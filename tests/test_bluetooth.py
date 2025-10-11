"""Hardware-independent controller handoff tests."""

from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager


from airpods_hr.bluetooth import AdapterNotFoundError, AdapterReappearanceTimeoutError, AdapterRestoreError, AdapterState, ControllerHandoff


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


class FakeBlueZ:
    def __init__(self, *, powered: bool, events: list[str]) -> None:
        self.powered = powered
        self.events = events
        self.missing_reads = 0
        self.always_missing = False
        self.fail_restore = False
        self.fail_power_down_after_change = False
        self.restore_failures_remaining = 0
        self.restore_raise_after_change = False

    async def ensure_available(self) -> None:
        self.events.append("bluez-ready")

    async def get_adapter(self, adapter_name: str) -> AdapterState:
        if self.always_missing or self.missing_reads > 0:
            if self.missing_reads > 0:
                self.missing_reads -= 1
            self.events.append("adapter-missing")
            raise AdapterNotFoundError(adapter_name)
        self.events.append(f"read:{self.powered}")
        return AdapterState(adapter_name, 0, self.powered)

    async def set_powered(self, adapter_name: str, powered: bool) -> None:
        del adapter_name
        self.events.append(f"set:{powered}")
        if powered and self.fail_restore:
            raise RuntimeError("synthetic restore failure")
        if powered and self.restore_failures_remaining > 0:
            self.restore_failures_remaining -= 1
            raise RuntimeError("synthetic adapter-not-ready failure")
        if powered and self.restore_raise_after_change:
            self.restore_raise_after_change = False
            self.powered = True
            raise RuntimeError("synthetic post-apply D-Bus failure")
        self.powered = powered
        if not powered and self.fail_power_down_after_change:
            raise RuntimeError("synthetic power-down failure")

    def close(self) -> None:
        self.events.append("close")


class FakeTransport:
    def __init__(
        self,
        *,
        events: list[str],
        on_release=None,
        acquisition_error: BaseException | None = None,
    ) -> None:
        self.events = events
        self.on_release = on_release
        self.acquisition_error = acquisition_error

    async def ensure_available(self) -> None:
        self.events.append("transport-ready")

    @asynccontextmanager
    async def acquire(self, adapter_index: int):
        self.events.append(f"acquire:{adapter_index}")
        if self.acquisition_error is not None:
            raise self.acquisition_error
        try:
            yield object()
        finally:
            self.events.append("release")
            if self.on_release is not None:
                self.on_release()


def make_handoff(bluez: FakeBlueZ, transport: FakeTransport, clock: FakeClock):
    return ControllerHandoff(
        bluez,
        transport,
        state_timeout=1.0,
        restore_timeout=1.0,
        poll_interval=0.25,
        sleep=clock.sleep,
        clock=clock.monotonic,
    )


class ControllerHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_originally_powered_on_off_acquire_release_on(self) -> None:
        events: list[str] = []
        clock = FakeClock()
        bluez = FakeBlueZ(powered=True, events=events)
        handoff = make_handoff(bluez, FakeTransport(events=events), clock)

        async with handoff.handoff("hci0"):
            events.append("held")

        actions = [event for event in events if not event.startswith("read:")]
        self.assertEqual(
            actions,
            ["set:False", "acquire:0", "held", "release", "set:True"],
        )
        self.assertTrue(bluez.powered)

    async def test_originally_powered_off_remains_off(self) -> None:
        events: list[str] = []
        clock = FakeClock()
        bluez = FakeBlueZ(powered=False, events=events)
        handoff = make_handoff(bluez, FakeTransport(events=events), clock)

        async with handoff.handoff("hci0"):
            pass

        self.assertFalse(bluez.powered)
        self.assertNotIn("set:True", events)
        self.assertNotIn("set:False", events)

    async def test_acquisition_failure_restores_original_state(self) -> None:
        events: list[str] = []
        clock = FakeClock()
        bluez = FakeBlueZ(powered=True, events=events)
        transport = FakeTransport(
            events=events,
            acquisition_error=RuntimeError("synthetic acquisition failure"),
        )
        handoff = make_handoff(bluez, transport, clock)

        with self.assertRaisesRegex(RuntimeError, "acquisition failure"):
            async with handoff.handoff("hci0"):
                self.fail("handoff body must not run")

        self.assertTrue(bluez.powered)
        self.assertIn("set:True", events)

    async def test_power_down_failure_after_change_restores_state(self) -> None:
        events: list[str] = []
        clock = FakeClock()
        bluez = FakeBlueZ(powered=True, events=events)
        bluez.fail_power_down_after_change = True
        handoff = make_handoff(bluez, FakeTransport(events=events), clock)

        with self.assertRaisesRegex(RuntimeError, "power-down failure"):
            async with handoff.handoff("hci0"):
                self.fail("handoff body must not run")

        self.assertEqual(events.count("set:False"), 1)
        self.assertEqual(events.count("set:True"), 1)
        self.assertTrue(bluez.powered)

    async def test_held_exception_releases_and_restores(self) -> None:
        events: list[str] = []
        clock = FakeClock()
        bluez = FakeBlueZ(powered=True, events=events)
        handoff = make_handoff(bluez, FakeTransport(events=events), clock)

        with self.assertRaisesRegex(RuntimeError, "held failure"):
            async with handoff.handoff("hci0"):
                raise RuntimeError("synthetic held failure")

        self.assertLess(events.index("release"), events.index("set:True"))
        self.assertTrue(bluez.powered)

    async def test_cancellation_attempts_release_and_restoration(self) -> None:
        events: list[str] = []
        clock = FakeClock()
        bluez = FakeBlueZ(powered=True, events=events)
        entered = asyncio.Event()
        wait_forever = asyncio.Event()
        handoff = make_handoff(bluez, FakeTransport(events=events), clock)

        async def worker() -> None:
            async with handoff.handoff("hci0"):
                entered.set()
                await wait_forever.wait()

        task = asyncio.create_task(worker())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertIn("release", events)
        self.assertIn("set:True", events)
        self.assertTrue(bluez.powered)

    async def test_temporarily_missing_adapter_is_retried(self) -> None:
        events: list[str] = []
        clock = FakeClock()
        bluez = FakeBlueZ(powered=True, events=events)

        def disappear_temporarily() -> None:
            bluez.missing_reads = 2

        transport = FakeTransport(
            events=events,
            on_release=disappear_temporarily,
        )
        handoff = make_handoff(bluez, transport, clock)

        async with handoff.handoff("hci0"):
            pass

        self.assertEqual(events.count("adapter-missing"), 2)
        self.assertTrue(bluez.powered)
        self.assertEqual(clock.now, 0.5)

    async def test_restore_set_error_but_observed_state_is_success(self) -> None:
        events: list[str] = []
        clock = FakeClock()
        bluez = FakeBlueZ(powered=True, events=events)
        bluez.restore_raise_after_change = True
        handoff = make_handoff(bluez, FakeTransport(events=events), clock)

        async with handoff.handoff("hci0"):
            pass

        self.assertTrue(bluez.powered)
        self.assertEqual(events.count("set:True"), 1)
        self.assertIn("read:True", events[events.index("set:True") + 1 :])

    async def test_transient_restore_set_failures_eventually_succeed(self) -> None:
        events: list[str] = []
        clock = FakeClock()
        bluez = FakeBlueZ(powered=True, events=events)
        bluez.restore_failures_remaining = 3
        handoff = make_handoff(bluez, FakeTransport(events=events), clock)

        async with handoff.handoff("hci0"):
            pass

        self.assertTrue(bluez.powered)
        self.assertEqual(events.count("set:True"), 4)
        self.assertEqual(clock.now, 0.75)

    async def test_reappeared_adapter_retries_until_ready_for_set(self) -> None:
        events: list[str] = []
        clock = FakeClock()
        bluez = FakeBlueZ(powered=True, events=events)
        bluez.restore_failures_remaining = 2

        def reappear_before_ready() -> None:
            bluez.missing_reads = 1

        handoff = make_handoff(
            bluez,
            FakeTransport(events=events, on_release=reappear_before_ready),
            clock,
        )

        async with handoff.handoff("hci0"):
            pass

        self.assertTrue(bluez.powered)
        self.assertEqual(events.count("adapter-missing"), 1)
        self.assertEqual(events.count("set:True"), 3)

    async def test_adapter_never_reappears_has_bounded_timeout(self) -> None:
        events: list[str] = []
        clock = FakeClock()
        bluez = FakeBlueZ(powered=True, events=events)

        def disappear() -> None:
            bluez.always_missing = True

        handoff = make_handoff(
            bluez,
            FakeTransport(events=events, on_release=disappear),
            clock,
        )

        with self.assertRaises(AdapterReappearanceTimeoutError):
            async with handoff.handoff("hci0"):
                pass

        self.assertEqual(clock.now, 1.0)

    async def test_restore_setter_failure_has_dedicated_error(self) -> None:
        events: list[str] = []
        clock = FakeClock()
        bluez = FakeBlueZ(powered=True, events=events)
        bluez.fail_restore = True
        handoff = make_handoff(bluez, FakeTransport(events=events), clock)

        with self.assertRaises(AdapterRestoreError) as caught:
            async with handoff.handoff("hci0"):
                pass

        self.assertIn("Powered=True", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)


