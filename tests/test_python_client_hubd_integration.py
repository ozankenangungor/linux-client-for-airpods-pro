"""Python SDK integration against the real hub daemon with a fake sensor."""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from tests.client_sdk_test_support import PYTHON_CLIENT_SRC as _CLIENT_SRC

from airpods_client import (
    AirPodsClient,
    ConnectionClosed,
    DaemonState as ClientDaemonState,
    SourceSide,
)
from airpods_hr._hubd.server import DaemonState as HubDaemonState
from tests.test_rust_client_hubd_integration import (
    BarrierHubDaemon,
    FakeSensorSession,
    FakeSessionFactory,
    TEST_TIMEOUT,
    report,
)


class PythonClientHubdIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.temp.name) / "airpods-hubd.sock"
        self.session = FakeSensorSession()
        self.factory = FakeSessionFactory(self.session)
        self.daemon = BarrierHubDaemon(self.factory, self.socket_path)
        self.clients: list[AirPodsClient] = []
        await self.daemon.start()
        self.assertEqual(self.daemon.state, HubDaemonState.READY)

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.close()
        await self.daemon.shutdown()
        self.temp.cleanup()

    async def connect(self) -> AirPodsClient:
        client = await AirPodsClient.connect_to(self.socket_path)
        self.clients.append(client)
        return client

    async def assert_single_open_session(self) -> None:
        self.assertEqual(self.factory.calls, 1)
        self.assertEqual(self.session.open_calls, 1)
        self.assertEqual(self.session.close_calls, 0)
        await self.daemon.shutdown()
        self.assertEqual(self.session.close_calls, 1)
        self.assertEqual(self.daemon.state, HubDaemonState.STOPPED)

    async def test_real_daemon_protocol_events_and_interleaving(self) -> None:
        client = await self.connect()
        hello = await client.hello()
        self.assertEqual(hello.service, "airpods-hubd")
        self.assertTrue(hello.experimental)
        await client.ping()
        ready = await client.status()
        self.assertEqual(ready.state, ClientDaemonState.READY)
        self.assertEqual(ready.subscriber_count, 0)

        subscription = await client.subscribe_heart_rate()
        self.assertEqual(self.session.start_calls, 1)
        self.assertEqual(self.daemon.subscriber_count, 1)

        self.daemon.arm_status_interleave()
        status_task = asyncio.create_task(client.status())
        await asyncio.wait_for(self.daemon.status_entered.wait(), TEST_TIMEOUT)
        self.session.inject(report(169, field_5=1, sequence=1))
        await asyncio.wait_for(
            self.daemon.heart_rate_enqueued.wait(), TEST_TIMEOUT
        )
        self.daemon.status_release.set()
        first, streaming = await asyncio.gather(
            subscription.__anext__(), status_task
        )
        self.assertEqual((first.bpm, first.source_side), (169, SourceSide.LEFT))
        self.assertEqual(streaming.state, ClientDaemonState.STREAMING)
        self.assertEqual(streaming.subscriber_count, 1)

        self.session.inject(report(88, field_5=2, sequence=2))
        self.session.inject(report(88, field_5=2, sequence=3))
        self.session.inject(report(73, field_5=37, sequence=4))
        remaining = [await subscription.__anext__() for _ in range(3)]
        self.assertEqual([sample.bpm for sample in remaining], [88, 88, 73])
        self.assertEqual(
            [sample.source_side for sample in remaining],
            [SourceSide.RIGHT, SourceSide.RIGHT, SourceSide.UNKNOWN],
        )
        self.assertEqual(remaining[-1].source_side_raw, 37)

        await subscription.unsubscribe()
        ready = await client.status()
        self.assertEqual(ready.state, ClientDaemonState.READY)
        self.assertEqual(ready.subscriber_count, 0)
        self.assertEqual(self.session.stop_calls, 1)
        self.assertEqual(self.session.reports_returned, 4)
        await self.assert_single_open_session()

    async def test_two_python_clients_share_one_sensor_start(self) -> None:
        client_a = await self.connect()
        client_b = await self.connect()
        subscription_a = await client_a.subscribe_heart_rate()
        subscription_b = await client_b.subscribe_heart_rate()
        self.assertEqual(self.session.start_calls, 1)
        self.assertEqual(self.daemon.subscriber_count, 2)

        self.session.inject(report(101, field_5=1, sequence=1))
        self.session.inject(report(102, field_5=2, sequence=2))
        samples_a = [await subscription_a.__anext__() for _ in range(2)]
        samples_b = [await subscription_b.__anext__() for _ in range(2)]
        self.assertEqual([sample.bpm for sample in samples_a], [101, 102])
        self.assertEqual(samples_a, samples_b)

        await subscription_a.unsubscribe()
        self.assertEqual(self.daemon.subscriber_count, 1)
        self.assertEqual(self.session.stop_calls, 0)
        self.session.inject(report(83, field_5=37, sequence=3))
        remaining = await subscription_b.__anext__()
        self.assertEqual((remaining.bpm, remaining.source_side_raw), (83, 37))
        await subscription_b.unsubscribe()
        self.assertEqual(self.daemon.subscriber_count, 0)
        self.assertEqual(self.session.stop_calls, 1)
        self.assertEqual(self.daemon.state, HubDaemonState.READY)
        await self.assert_single_open_session()

    async def test_active_client_disconnect_cleans_real_daemon_state(self) -> None:
        client = await self.connect()
        subscription = await client.subscribe_heart_rate()
        self.assertEqual(self.session.start_calls, 1)
        await client.close()
        await asyncio.wait_for(self.session.stop_observed.wait(), TEST_TIMEOUT)
        self.assertEqual(self.session.stop_calls, 1)
        self.assertEqual(self.session.close_calls, 0)
        self.assertEqual(self.daemon.subscriber_count, 0)
        self.assertEqual(self.daemon.state, HubDaemonState.READY)

        with self.assertRaises(ConnectionClosed):
            await subscription.__anext__()
        replacement = await self.connect()
        replacement_subscription = await replacement.subscribe_heart_rate()
        self.assertEqual(self.session.start_calls, 2)
        await replacement_subscription.unsubscribe()
        self.assertEqual(self.session.stop_calls, 2)
        await self.assert_single_open_session()


if __name__ == "__main__":
    unittest.main()
