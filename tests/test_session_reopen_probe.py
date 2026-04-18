"""Hardware-independent session lifecycle characterization tests."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from airpods_hr.aap import (
    AAPDescriptorObservationTimeoutError,
    AAPHandshakeTimeoutError,
    DescriptorEvidence,
    HandshakeObservation,
)
from airpods_hr.production_session import (
    ProductionSessionCategory,
    ProductionSessionCounters,
    ProductionSessionError,
)
from airpods_hr.session_reopen import (
    BlueZReopenCheckpointObserver,
    BlueZReopenCheckpoint,
    ReopenSessionBundle,
    Session1Mode,
    SessionReopenResultCategory,
    _ObservedHandshakeSession,
    create_reopen_session_bundle,
)
from tools.probe_session_reopen import build_parser, main, run_probe


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SESSION_SHA256 = (
    "f79e1cae91650459be0c016e53dea0aa10322e3159b17eb0c6857e3919266c4f"
)
FROZEN_SHA256 = {
    "src/airpods_hr/protocol.py": (
        "b4d1daea0582841e48ba9efc3a8a7d4d74bba9b69cdbf54d3767b8bb45afecca"
    ),
    "src/airpods_hr/heartrate.py": (
        "df0ddb9824146c7ab23eb30c2548aaa9ec7e8dc26461d76aaf92f2c19c3dc045"
    ),
    "src/airpods_hr/bluez_coexistence.py": (
        "4fd967c6350a90b511b51064a5718c68b284517682eb4f15770a05c50351b2d2"
    ),
    "src/airpods_hr/monitor_cli.py": (
        "41332f411af2e89374b42047a2e009aef035ca2e8bce74440d0d9d891d7aded4"
    ),
    "src/airpods_hr/hr_semantics.py": (
        "f6004987032f02c5b2a7c59590a4e3e2edb5ef85788f8259f2e0b9499f5bc4ba"
    ),
}


class FakeTransport:
    def __init__(self, identity: int, events: list[str]) -> None:
        self.identity = identity
        self.events = events
        self.close_calls = 0
        self.local_rx_observation = SimpleNamespace(
            before_imtu=672,
            after_imtu=2048,
            verified=True,
        )


class FakeHandshake:
    def __init__(
        self,
        identity: int,
        *,
        observation: HandshakeObservation | None = None,
        error: BaseException | None = None,
        completed: int = 1,
    ) -> None:
        self.identity = identity
        self.attempts = 1
        self.completed = completed
        self.observation = observation or HandshakeObservation(
            True,
            DescriptorEvidence(True, True, True, True),
            post_ack_frame_count=29,
        )
        self.error = error


class FakeSession:
    def __init__(
        self,
        identity: int,
        transport: FakeTransport,
        handshake: FakeHandshake,
        events: list[str],
        *,
        open_error: BaseException | None = None,
        report_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.identity = identity
        self.transport = transport
        self.handshake = handshake
        self.events = events
        self.open_error = open_error
        self.report_error = report_error
        self.close_error = close_error
        self.state = "closed"
        self.opens = 0
        self.activations = 0
        self.stops = 0
        self.reports = 0

    @property
    def counters(self) -> ProductionSessionCounters:
        return ProductionSessionCounters(
            transport_opens=self.opens,
            descriptor_handshakes=self.handshake.completed,
            hr_activations=self.activations,
            hr_stops=self.stops,
            reports_received=self.reports,
        )

    async def open(self) -> None:
        self.events.append(f"session_{self.identity}_open")
        self.opens += 1
        if self.open_error is not None:
            raise self.open_error
        self.state = "ready"
        self.events.append(f"session_{self.identity}_ready")

    async def start(self) -> None:
        self.events.append(f"session_{self.identity}_start")
        self.activations += 1
        self.state = "streaming"

    async def receive_report(self, timeout: float):
        del timeout
        if self.report_error is not None:
            raise self.report_error
        self.reports += 1
        return SimpleNamespace(
            bpm=169,
            sequence=self.reports - 1,
            field_5=1,
            flags=0x1000,
        )

    async def stop(self) -> None:
        self.events.append(f"session_{self.identity}_stop")
        self.events.append(f"session_{self.identity}_hr_off")
        self.stops += 1
        self.state = "ready"

    async def close(self) -> None:
        self.events.append(f"session_{self.identity}_close_from_{self.state}")
        self.events.append(f"session_{self.identity}_close")
        self.transport.close_calls += 1
        self.state = "closed"
        if self.close_error is not None:
            raise self.close_error


class FakeObserver:
    def __init__(
        self,
        events: list[str],
        *,
        states: list[tuple[bool, bool]] | None = None,
    ) -> None:
        self.events = events
        self.states = iter(states or [(True, True)] * 3)
        self.close_calls = 0
        self.labels: list[str] = []

    def _make(self, label: str) -> BlueZReopenCheckpoint:
        powered, connected = next(self.states)
        self.labels.append(label)
        return BlueZReopenCheckpoint(label, True, powered, connected)

    async def open(self) -> BlueZReopenCheckpoint:
        self.events.append("observer_open")
        return self._make("before_session_1")

    async def checkpoint(self, label: str) -> BlueZReopenCheckpoint:
        self.events.append(label)
        return self._make(label)

    def close(self) -> None:
        self.events.append("observer_close")
        self.close_calls += 1


class BundleFactory:
    def __init__(
        self,
        events: list[str],
        second_error: BaseException | None = None,
        second_observation: HandshakeObservation | None = None,
        second_handshake_error: BaseException | None = None,
    ) -> None:
        self.events = events
        self.second_error = second_error
        self.second_observation = second_observation
        self.second_handshake_error = second_handshake_error
        self.bundles: list[ReopenSessionBundle] = []

    def __call__(self, **kwargs) -> ReopenSessionBundle:
        del kwargs
        identity = len(self.bundles) + 1
        transport = FakeTransport(identity, self.events)
        if identity == 2:
            handshake = FakeHandshake(
                identity,
                observation=self.second_observation,
                error=self.second_handshake_error,
                completed=0 if self.second_error else 1,
            )
            open_error = self.second_error
        else:
            handshake = FakeHandshake(identity)
            open_error = None
        session = FakeSession(
            identity,
            transport,
            handshake,
            self.events,
            open_error=open_error,
        )
        bundle = ReopenSessionBundle(session, transport, handshake)
        self.bundles.append(bundle)
        return bundle


class SessionReopenProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.events: list[str] = []
        self.observer = FakeObserver(self.events)

    async def run_with(self, factory: BundleFactory, **kwargs):
        output: list[str] = []
        status, result = await run_probe(
            execute=True,
            samples_per_session=2,
            reopen_delay=5,
            output=output.append,
            bundle_factory=factory,
            observer_factory=lambda: self.observer,
            sleep=AsyncMock(),
            **kwargs,
        )
        assert result is not None
        return status, result, output

    def test_defaults_and_bounds(self) -> None:
        args = build_parser().parse_args([])
        self.assertFalse(args.execute)
        self.assertEqual(args.session_1_mode, Session1Mode.HR_CYCLE.value)
        self.assertEqual(args.samples_per_session, 5)
        self.assertEqual(args.reopen_delay, 5)
        self.assertEqual(args.descriptor_timeout, 30)
        for option, value in (
            ("--samples-per-session", "0"),
            ("--reopen-delay", "301"),
            ("--descriptor-timeout", "0"),
        ):
            with self.subTest(option=option), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    build_parser().parse_args([option, value])

    async def test_dry_run_constructs_no_hardware_objects(self) -> None:
        factory = Mock()
        observer_factory = Mock()
        output: list[str] = []
        status, result = await run_probe(
            execute=False,
            output=output.append,
            bundle_factory=factory,
            observer_factory=observer_factory,
        )
        self.assertEqual(status, 0)
        self.assertIsNone(result)
        factory.assert_not_called()
        observer_factory.assert_not_called()
        rendered = "\n".join(output)
        self.assertIn("sessions=2; AAP channels=2", rendered)
        self.assertIn("descriptor state shared=no", rendered)
        self.assertIn("automatic BlueZ reconnect=no", rendered)
        self.assertIn("session_1_mode=hr-cycle", rendered)

    def test_descriptor_only_mode_is_parsed(self) -> None:
        args = build_parser().parse_args(
            ["--session-1-mode", "descriptor-only", "--reopen-delay", "60"]
        )
        self.assertEqual(args.session_1_mode, "descriptor-only")
        self.assertEqual(args.reopen_delay, 60)

    async def test_two_fresh_sessions_pass_with_expected_counters(self) -> None:
        factory = BundleFactory(self.events)
        status, result, _ = await self.run_with(factory)
        self.assertEqual(status, 0)
        self.assertIs(
            result.category, SessionReopenResultCategory.BOTH_SESSIONS_PASS
        )
        self.assertEqual(result.counters.session_objects_created, 2)
        self.assertIs(result.counters.session_1_mode, Session1Mode.HR_CYCLE)
        self.assertEqual(result.counters.transport_opens, 2)
        self.assertEqual(result.counters.transport_closes, 2)
        self.assertEqual(result.counters.descriptor_handshakes_attempted, 2)
        self.assertEqual(result.counters.descriptor_handshakes_completed, 2)
        self.assertEqual(result.counters.exact_aap_acks, 2)
        self.assertEqual(result.counters.hr_activations, 2)
        self.assertEqual(result.counters.hr_stops, 2)
        self.assertEqual(result.counters.hr_activations_session_1, 1)
        self.assertEqual(result.counters.hr_stops_session_1, 1)
        self.assertEqual(result.counters.reports_received_session_1, 2)
        self.assertEqual(result.counters.reports_received_session_2, 2)
        self.assertIn("session_1_start", self.events)
        self.assertIn("session_1_stop", self.events)
        self.assertIn("session_1_hr_off", self.events)

    async def test_descriptor_only_first_session_never_activates_hr(self) -> None:
        factory = BundleFactory(self.events)
        output: list[str] = []
        status, result = await run_probe(
            execute=True,
            session_1_mode=Session1Mode.DESCRIPTOR_ONLY,
            samples_per_session=2,
            reopen_delay=5,
            output=output.append,
            bundle_factory=factory,
            observer_factory=lambda: self.observer,
            sleep=AsyncMock(),
        )
        assert result is not None
        self.assertEqual(status, 0)
        self.assertIs(
            result.counters.session_1_mode, Session1Mode.DESCRIPTOR_ONLY
        )
        self.assertIn("session_1_ready", self.events)
        self.assertNotIn("session_1_start", self.events)
        self.assertNotIn("session_1_stop", self.events)
        self.assertNotIn("session_1_hr_off", self.events)
        self.assertIn("session_1_close_from_ready", self.events)
        self.assertEqual(result.counters.hr_activations_session_1, 0)
        self.assertEqual(result.counters.hr_stops_session_1, 0)
        self.assertEqual(result.counters.reports_received_session_1, 0)
        self.assertEqual(result.counters.reports_received_session_2, 2)
        self.assertIn("session_1_mode=descriptor-only", "\n".join(output))

    async def test_descriptor_only_timeout_preserves_classification(self) -> None:
        observation = HandshakeObservation(
            True,
            DescriptorEvidence(),
            post_ack_frame_count=27,
            receive_frames_dropped=0,
        )
        factory = BundleFactory(
            self.events,
            second_error=ProductionSessionError(
                ProductionSessionCategory.DESCRIPTOR_HANDSHAKE_FAILED,
                "descriptor_handshake",
            ),
            second_observation=observation,
            second_handshake_error=AAPDescriptorObservationTimeoutError(
                observation
            ),
        )
        _, result = await run_probe(
            execute=True,
            session_1_mode="descriptor-only",
            samples_per_session=2,
            output=lambda message: None,
            bundle_factory=factory,
            observer_factory=lambda: self.observer,
            sleep=AsyncMock(),
        )
        assert result is not None
        self.assertIs(
            result.category,
            SessionReopenResultCategory.SESSION_2_EXACT_ACK_DESCRIPTOR_TIMEOUT,
        )
        self.assertEqual(result.counters.session_objects_created, 2)
        self.assertEqual(result.counters.hr_activations_session_1, 0)
        self.assertEqual(result.counters.hr_stops_session_1, 0)
        self.assertEqual(result.counters.reports_received_session_1, 0)
        self.assertNotIn("session_3_open", self.events)

    async def test_objects_transports_and_handshakes_are_distinct(self) -> None:
        factory = BundleFactory(self.events)
        await self.run_with(factory)
        first, second = factory.bundles
        self.assertIsNot(first.session, second.session)
        self.assertIsNot(first.transport, second.transport)
        self.assertIsNot(first.handshake, second.handshake)
        self.assertIsNot(
            first.handshake.observation, second.handshake.observation
        )

    async def test_first_is_closed_before_second_is_created_and_opened(self) -> None:
        factory = BundleFactory(self.events)
        await self.run_with(factory)
        self.assertLess(
            self.events.index("session_1_close"),
            self.events.index("before_session_2_open"),
        )
        self.assertLess(
            self.events.index("before_session_2_open"),
            self.events.index("session_2_open"),
        )

    async def test_required_bluez_checkpoints_are_recorded(self) -> None:
        factory = BundleFactory(self.events)
        _, result, output = await self.run_with(factory)
        self.assertEqual(
            [item.label for item in result.checkpoints],
            [
                "before_session_1",
                "after_session_1_close",
                "before_session_2_open",
            ],
        )
        self.assertTrue(all(item.invariant_holds for item in result.checkpoints))
        self.assertIn("Device1.Connected=yes", "\n".join(output))

    async def test_bluez_state_change_prevents_second_session(self) -> None:
        self.observer = FakeObserver(
            self.events, states=[(True, True), (True, False)]
        )
        factory = BundleFactory(self.events)
        status, result, _ = await self.run_with(factory)
        self.assertEqual(status, 1)
        self.assertIs(
            result.category, SessionReopenResultCategory.BLUEZ_STATE_CHANGED
        )
        self.assertEqual(len(factory.bundles), 1)
        self.assertNotIn("session_2_open", self.events)

    async def test_exact_ack_descriptor_timeout_has_dedicated_category(self) -> None:
        observation = HandshakeObservation(
            True,
            DescriptorEvidence(sensor_framework=True),
            post_ack_frame_count=30,
            receive_frames_dropped=0,
        )
        timeout = AAPDescriptorObservationTimeoutError(observation)
        factory = BundleFactory(
            self.events,
            second_error=ProductionSessionError(
                ProductionSessionCategory.DESCRIPTOR_HANDSHAKE_FAILED,
                "descriptor_handshake",
            ),
            second_observation=observation,
            second_handshake_error=timeout,
        )
        status, result, output = await self.run_with(factory, verbose=True)
        self.assertEqual(status, 1)
        self.assertIs(
            result.category,
            SessionReopenResultCategory.SESSION_2_EXACT_ACK_DESCRIPTOR_TIMEOUT,
        )
        self.assertEqual(result.counters.exact_aap_acks, 2)
        self.assertEqual(result.counters.descriptor_handshakes_completed, 1)
        self.assertEqual(result.counters.reports_received_session_1, 2)
        self.assertEqual(result.counters.reports_received_session_2, 0)
        rendered = "\n".join(output)
        self.assertIn("exact_ack_observed=yes", rendered)
        self.assertIn("post_ack_frames=30", rendered)
        self.assertIn("heart_rate_service=no", rendered)
        self.assertNotIn("raw", rendered.lower())

    async def test_missing_ack_has_distinct_category(self) -> None:
        observation = HandshakeObservation(False, DescriptorEvidence())
        timeout = AAPHandshakeTimeoutError("missing ACK", observation)
        factory = BundleFactory(
            self.events,
            second_error=ProductionSessionError(
                ProductionSessionCategory.DESCRIPTOR_HANDSHAKE_FAILED,
                "descriptor_handshake",
            ),
            second_observation=observation,
            second_handshake_error=timeout,
        )
        _, result, _ = await self.run_with(factory)
        self.assertIs(
            result.category, SessionReopenResultCategory.SESSION_2_AAP_ACK_FAILURE
        )

    async def test_transport_failure_has_distinct_category(self) -> None:
        factory = BundleFactory(
            self.events,
            second_error=ProductionSessionError(
                ProductionSessionCategory.TRANSPORT_FAILED, "transport_open"
            ),
        )
        _, result, _ = await self.run_with(factory)
        self.assertIs(
            result.category,
            SessionReopenResultCategory.SESSION_2_TRANSPORT_FAILURE,
        )

    async def test_session_2_preflight_change_has_bluez_category(self) -> None:
        factory = BundleFactory(
            self.events,
            second_error=ProductionSessionError(
                ProductionSessionCategory.PREFLIGHT_FAILED, "preflight"
            ),
        )
        _, result, _ = await self.run_with(factory)
        self.assertIs(
            result.category, SessionReopenResultCategory.BLUEZ_STATE_CHANGED
        )

    async def test_generic_handshake_failure_is_other_failure(self) -> None:
        factory = BundleFactory(
            self.events,
            second_error=ProductionSessionError(
                ProductionSessionCategory.DESCRIPTOR_HANDSHAKE_FAILED,
                "descriptor_handshake",
            ),
            second_handshake_error=RuntimeError("generic handshake failure"),
        )
        factory.second_observation = None
        _, result, _ = await self.run_with(factory)
        self.assertIs(
            result.category, SessionReopenResultCategory.OTHER_FAILURE
        )

    async def test_second_failure_is_cleaned_without_third_session(self) -> None:
        observation = HandshakeObservation(True, DescriptorEvidence())
        factory = BundleFactory(
            self.events,
            second_error=ProductionSessionError(
                ProductionSessionCategory.DESCRIPTOR_HANDSHAKE_FAILED,
                "descriptor_handshake",
            ),
            second_observation=observation,
            second_handshake_error=AAPDescriptorObservationTimeoutError(
                observation
            ),
        )
        _, result, _ = await self.run_with(factory)
        self.assertEqual(len(factory.bundles), 2)
        self.assertEqual(factory.bundles[1].transport.close_calls, 1)
        self.assertEqual(result.counters.transport_closes, 2)
        self.assertNotIn("session_3_open", self.events)

    async def test_descriptor_timeout_never_reaches_hr_or_reconnect(self) -> None:
        observation = HandshakeObservation(True, DescriptorEvidence())
        factory = BundleFactory(
            self.events,
            second_error=ProductionSessionError(
                ProductionSessionCategory.DESCRIPTOR_HANDSHAKE_FAILED,
                "descriptor_handshake",
            ),
            second_observation=observation,
            second_handshake_error=AAPDescriptorObservationTimeoutError(
                observation
            ),
        )
        await self.run_with(factory)
        self.assertNotIn("session_2_start", self.events)
        self.assertFalse(
            {"device_connect", "device_disconnect", "ack_only"}
            & set(self.events)
        )

    async def test_reopen_delay_is_honored(self) -> None:
        factory = BundleFactory(self.events)
        sleep = AsyncMock()
        await run_probe(
            execute=True,
            samples_per_session=1,
            reopen_delay=7.5,
            output=lambda message: None,
            bundle_factory=factory,
            observer_factory=lambda: self.observer,
            sleep=sleep,
        )
        sleep.assert_awaited_once_with(7.5)

    async def test_cancellation_closes_current_session_and_observer(self) -> None:
        class CancelSecondFactory(BundleFactory):
            def __call__(self, **kwargs):
                bundle = super().__call__(**kwargs)
                if len(self.bundles) == 2:
                    bundle.session.open_error = asyncio.CancelledError()
                return bundle

        factory = CancelSecondFactory(self.events)
        with self.assertRaises(asyncio.CancelledError):
            await self.run_with(factory)
        self.assertEqual(factory.bundles[1].transport.close_calls, 1)
        self.assertEqual(self.observer.close_calls, 1)

    def test_concrete_factory_builds_fresh_dependency_graphs(self) -> None:
        first = create_reopen_session_bundle(output=lambda message: None)
        second = create_reopen_session_bundle(output=lambda message: None)
        self.assertIsNot(first.session, second.session)
        self.assertIsNot(first.transport, second.transport)
        self.assertIsNot(first.handshake, second.handshake)
        self.assertIsNot(
            first.session._client, second.session._client  # type: ignore[attr-defined]
        )
        self.assertIsNot(
            first.session._registration,  # type: ignore[attr-defined]
            second.session._registration,  # type: ignore[attr-defined]
        )

    def test_main_dry_run_smoke(self) -> None:
        stream = StringIO()
        self.assertEqual(main([], stream=stream), 0)
        self.assertIn("DRY RUN", stream.getvalue())


class SessionReopenStaticSafetyTests(unittest.TestCase):
    def test_production_core_is_frozen(self) -> None:
        data = (ROOT / "src/airpods_hr/production_session.py").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), PRODUCTION_SESSION_SHA256)

    def test_protocol_parser_transport_monitor_and_semantics_are_frozen(self) -> None:
        for relative, expected in FROZEN_SHA256.items():
            with self.subTest(path=relative):
                data = (ROOT / relative).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), expected)

    def test_private_modules_are_not_publicly_exported(self) -> None:
        package_init = (ROOT / "src/airpods_hr/__init__.py").read_text()
        self.assertNotIn("session_reopen", package_init)
        self.assertNotIn("InternalProductionSession", package_init)

    def test_no_forbidden_backend_or_credential_dependency(self) -> None:
        paths = [
            ROOT / "src/airpods_hr/session_reopen.py",
            ROOT / "tools/probe_session_reopen.py",
        ]
        forbidden_imports = {
            "airpods_hr.bumble_keys",
            "airpods_hr.controller_handoff",
            "airpods_hr.handoff",
        }
        for path in paths:
            tree = ast.parse(path.read_text())
            imported = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }
            self.assertTrue(forbidden_imports.isdisjoint(imported), path)
            source = path.read_text()
            self.assertNotIn("/var/lib/bluetooth", source)
            self.assertNotIn("HCI_CHANNEL_USER", source)
            self.assertNotIn("ControllerHandoff", source)

    def test_probe_has_no_connect_disconnect_or_third_session_strategy(self) -> None:
        source = (ROOT / "tools/probe_session_reopen.py").read_text()
        self.assertNotIn("ConnectProfile", source)
        self.assertNotIn("Disconnect", source)
        self.assertNotIn("range(3)", source)


class SessionReopenDiagnosticBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_observer_is_read_only(self) -> None:
        candidate = object()
        state = SimpleNamespace(
            candidate=candidate,
            adapter_powered=True,
            device_connected=True,
        )

        class FakeBlueZClient:
            def __init__(self) -> None:
                self.calls: list[object] = []

            async def connect(self) -> None:
                self.calls.append("connect")

            async def preflight(self, *, require_connected: bool):
                self.calls.append(("preflight", require_connected))
                return state

            async def snapshot(self, selected):
                self.calls.append(("snapshot", selected))
                return state

            def close(self) -> None:
                self.calls.append("close")

        client = FakeBlueZClient()
        observer = BlueZReopenCheckpointObserver(client, timeout=1)
        initial = await observer.open()
        later = await observer.checkpoint("after_session_1_close")
        observer.close()
        self.assertTrue(initial.invariant_holds)
        self.assertTrue(later.invariant_holds)
        self.assertEqual(
            client.calls,
            [
                "connect",
                ("preflight", True),
                ("snapshot", candidate),
                "close",
            ],
        )

    async def test_handshake_observer_retains_canonical_safe_timeout(self) -> None:
        observation = HandshakeObservation(
            True,
            DescriptorEvidence(sensor_framework=True),
            post_ack_frame_count=26,
            receive_frames_dropped=0,
        )

        class Delegate:
            async def run_collected(self, transport):
                del transport
                raise AAPDescriptorObservationTimeoutError(observation)

        wrapper = _ObservedHandshakeSession(Delegate())
        with self.assertRaises(AAPDescriptorObservationTimeoutError):
            await wrapper.run_collected(object())
        self.assertIs(wrapper.observation, observation)
        self.assertEqual(wrapper.attempts, 1)
        self.assertEqual(wrapper.completed, 0)
        self.assertIsInstance(
            wrapper.error, AAPDescriptorObservationTimeoutError
        )
