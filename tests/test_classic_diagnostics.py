"""Hardware-independent parity tests for the temporary Classic runtime."""

from __future__ import annotations

import inspect
import unittest
from dataclasses import fields
from types import SimpleNamespace

from bumble import hci, l2cap
from bumble.core import PhysicalTransport
from bumble.device import Device, DeviceConfiguration
from bumble.host import Host
from bumble.keys import PairingKeys

from airpods_hr.classic_diagnostics import (
    POWER_ON_CLASSIC_WRITES,
    POWER_ON_NOT_EXPLICITLY_WRITTEN,
    ClassicHostStateObserver,
    DeviceConfigurationAudit,
    RuntimeNameProfile,
    audit_device_configuration,
    local_name_matches_profile,
    runtime_name_for_profile,
)


class DeviceConfigurationParityTests(unittest.TestCase):
    def test_old_and_current_profiles_differ_only_in_name_and_config_keystore(
        self,
    ) -> None:
        legacy = DeviceConfiguration(
            name=runtime_name_for_profile(RuntimeNameProfile.LEGACY_POC),
            classic_enabled=True,
            le_enabled=False,
            keystore="JsonKeyStore",
        )
        current = DeviceConfiguration(
            name=runtime_name_for_profile(RuntimeNameProfile.PROJECT_DEFAULT),
            classic_enabled=True,
            le_enabled=False,
        )
        legacy_audit = audit_device_configuration(
            legacy,
            runtime_name_profile=RuntimeNameProfile.LEGACY_POC,
            keystore_available_before_power_on=True,
        )
        current_audit = audit_device_configuration(
            current,
            runtime_name_profile=RuntimeNameProfile.PROJECT_DEFAULT,
            keystore_available_before_power_on=True,
        )

        differences = {
            field.name
            for field in fields(DeviceConfigurationAudit)
            if getattr(legacy_audit, field.name) != getattr(current_audit, field.name)
        }
        self.assertEqual(
            differences, {"runtime_name_profile", "config_keystore_present"}
        )

    def test_parity_audit_covers_reviewed_classic_configuration_fields(self) -> None:
        names = {field.name for field in fields(DeviceConfigurationAudit)}
        self.assertTrue(
            {
                "classic_enabled",
                "le_enabled",
                "configured_address_is_default",
                "class_of_device",
                "classic_sc_enabled",
                "classic_ssp_enabled",
                "classic_smp_enabled",
                "classic_accept_any",
                "classic_interlaced_scan_enabled",
                "connectable",
                "discoverable",
                "io_capability",
                "gap_service_enabled",
                "gatt_service_enabled",
                "enhanced_retransmission_supported",
                "l2cap_extended_features",
                "config_keystore_present",
                "keystore_available_before_power_on",
            }.issubset(names)
        )

    def test_runtime_name_ab_profile_changes_only_name(self) -> None:
        current = DeviceConfiguration(
            name=runtime_name_for_profile(RuntimeNameProfile.PROJECT_DEFAULT),
            classic_enabled=True,
            le_enabled=False,
        )
        legacy = DeviceConfiguration(
            name=runtime_name_for_profile(RuntimeNameProfile.LEGACY_POC),
            classic_enabled=True,
            le_enabled=False,
        )
        self.assertNotEqual(current.name, legacy.name)
        current.name = legacy.name
        self.assertEqual(current, legacy)

    def test_power_on_does_not_inspect_sdp_or_service_records(self) -> None:
        power_on_source = inspect.getsource(Device.power_on)
        discoverable_source = inspect.getsource(Device.set_discoverable)
        inspected = power_on_source + discoverable_source
        self.assertNotIn("sdp_service_records", inspected)
        self.assertNotIn("sdp_server", inspected)
        self.assertIn("HCI_Write_Local_Name_Command", power_on_source)
        self.assertIn("CompleteLocalName(self.name)", discoverable_source)

    def test_power_on_audit_lists_written_and_unwritten_state(self) -> None:
        self.assertIn("local_name", POWER_ON_CLASSIC_WRITES)
        self.assertIn("scan_enable", POWER_ON_CLASSIC_WRITES)
        self.assertIn("page_timeout", POWER_ON_NOT_EXPLICITLY_WRITTEN)
        self.assertIn("default_link_policy", POWER_ON_NOT_EXPLICITLY_WRITTEN)
        self.assertFalse(
            set(POWER_ON_CLASSIC_WRITES)
            & set(POWER_ON_NOT_EXPLICITLY_WRITTEN)
        )
        inspected = (
            inspect.getsource(Host.reset)
            + inspect.getsource(Device.power_on)
            + inspect.getsource(Device.set_connectable)
            + inspect.getsource(Device.set_discoverable)
        )
        self.assertIn("HCI_Reset_Command", inspected)
        self.assertNotIn("HCI_Write_Page_Timeout_Command", inspected)
        self.assertNotIn("HCI_Write_Authentication_Enable_Command", inspected)
        self.assertNotIn("HCI_Write_Default_Link_Policy_Settings_Command", inspected)


class DynamicKeyStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_link_key_provider_reads_current_device_keystore(self) -> None:
        device = Device(
            config=DeviceConfiguration(classic_enabled=True, le_enabled=False),
            host=Host(),
        )
        peer = hci.Address.from_string_for_transport(
            ":".join(f"{value:02X}" for value in range(6)),
            PhysicalTransport.BR_EDR,
        )

        class Store:
            def __init__(self, value: bytes) -> None:
                self.value = value

            async def get(self, name: str) -> PairingKeys:
                self.request_name = name
                return PairingKeys(link_key=PairingKeys.Key(self.value))

        first = Store(bytes(range(16)))
        second = Store(bytes(reversed(range(16))))
        device.keystore = first
        self.assertEqual(await device.host.link_key_provider(peer), first.value)
        device.keystore = second
        self.assertEqual(await device.host.link_key_provider(peer), second.value)


class FakeHost:
    @staticmethod
    def supports_command(_op_code: int) -> bool:
        return True


class FakeReadableDevice:
    def __init__(self, local_name: object | None = None) -> None:
        self.config = SimpleNamespace(io_capability=3)
        self.classic_enabled = True
        self.le_enabled = False
        self.connectable = True
        self.discoverable = True
        self.classic_ssp_enabled = True
        self.classic_sc_enabled = True
        self.host = FakeHost()
        self.local_name = (
            runtime_name_for_profile(RuntimeNameProfile.PROJECT_DEFAULT)
            if local_name is None
            else local_name
        )

    async def send_sync_command(self, command: object) -> object:
        if isinstance(command, hci.HCI_Read_Local_Name_Command):
            return SimpleNamespace(local_name=self.local_name)
        if isinstance(command, hci.HCI_Read_Class_Of_Device_Command):
            return SimpleNamespace(class_of_device=0x010203)
        if isinstance(command, hci.HCI_Read_Authentication_Enable_Command):
            return SimpleNamespace(authentication_enable=1)
        if isinstance(command, hci.HCI_Read_Simple_Pairing_Mode_Command):
            return SimpleNamespace(simple_pairing_mode=1)
        if isinstance(command, hci.HCI_Read_Page_Scan_Type_Command):
            return SimpleNamespace(page_scan_type=1)
        if isinstance(command, hci.HCI_Read_Page_Scan_Activity_Command):
            return SimpleNamespace(page_scan_interval=0x0800, page_scan_window=0x0012)
        raise AssertionError("unexpected read command")


class ClassicHostStateObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_uses_only_allowlisted_read_commands(self) -> None:
        snapshot = await ClassicHostStateObserver().capture(
            FakeReadableDevice(), RuntimeNameProfile.PROJECT_DEFAULT
        )
        self.assertEqual(snapshot.observed_class_of_device, 0x010203)
        self.assertTrue(snapshot.observed_local_name_matches_profile)
        self.assertEqual(snapshot.observed_authentication_enable, 1)
        self.assertEqual(snapshot.observed_simple_pairing_mode, 1)
        self.assertEqual(snapshot.observed_page_scan_type, 1)
        self.assertEqual(snapshot.observed_page_scan_interval, 0x0800)
        self.assertEqual(snapshot.observed_page_scan_window, 0x0012)
        self.assertIn("scan_enable", snapshot.unavailable_fields)
        self.assertIn("page_timeout", snapshot.unavailable_fields)
        self.assertIn("default_link_policy", snapshot.unavailable_fields)

    async def test_snapshot_repr_has_no_address_key_or_raw_name(self) -> None:
        snapshot = await ClassicHostStateObserver().capture(
            FakeReadableDevice(), RuntimeNameProfile.PROJECT_DEFAULT
        )
        rendered = repr(snapshot).lower()
        self.assertNotRegex(rendered, r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")
        self.assertNotIn("linkkey", rendered)
        self.assertNotIn(
            runtime_name_for_profile(RuntimeNameProfile.PROJECT_DEFAULT),
            rendered,
        )

    async def test_malformed_local_name_is_unavailable(self) -> None:
        snapshot = await ClassicHostStateObserver().capture(
            FakeReadableDevice(b"\xff\x00" + bytes(246)),
            RuntimeNameProfile.PROJECT_DEFAULT,
        )

        self.assertIsNone(snapshot.observed_local_name_matches_profile)
        self.assertIn("local_name_matches_profile", snapshot.unavailable_fields)


class LocalNameNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = RuntimeNameProfile.PROJECT_DEFAULT
        self.expected = runtime_name_for_profile(self.profile)

    def test_exact_str_matches(self) -> None:
        self.assertTrue(local_name_matches_profile(self.expected, self.profile))

    def test_nul_padded_str_matches(self) -> None:
        self.assertTrue(
            local_name_matches_profile(self.expected + "\x00\x00", self.profile)
        )

    def test_exact_bytes_match(self) -> None:
        self.assertTrue(
            local_name_matches_profile(self.expected.encode(), self.profile)
        )

    def test_realistic_248_octet_nul_padded_bytes_match(self) -> None:
        encoded = self.expected.encode()
        local_name = encoded + bytes(248 - len(encoded))

        self.assertEqual(len(local_name), 248)
        self.assertTrue(local_name_matches_profile(local_name, self.profile))

    def test_malformed_bytes_are_unavailable(self) -> None:
        self.assertIsNone(
            local_name_matches_profile(b"\xff\x00" + bytes(246), self.profile)
        )

    def test_genuine_mismatch_is_false(self) -> None:
        self.assertFalse(local_name_matches_profile(b"different\x00", self.profile))


class BasicModeSendParityTests(unittest.TestCase):
    def test_write_and_send_pdu_reach_same_cid_with_same_sdu(self) -> None:
        manager = l2cap.ChannelManager()
        sent: list[tuple[object, int, bytes, bool]] = []
        manager.send_pdu = lambda connection, cid, pdu, with_fcs=False: sent.append(
            (connection, cid, bytes(pdu), with_fcs)
        )
        connection = SimpleNamespace(handle=1)
        channel = l2cap.ClassicChannel(
            manager=manager,
            connection=connection,
            signaling_cid=l2cap.L2CAP_SIGNALING_CID,
            psm=0x1001,
            source_cid=0x0040,
            spec=l2cap.ClassicChannelSpec(
                psm=0x1001, mode=l2cap.TransmissionMode.BASIC
            ),
        )
        channel.destination_cid = 0x0041
        channel.state = channel.State.OPEN
        payload = b"synthetic application SDU"

        channel.send_pdu(payload)
        direct = sent.pop()
        channel.write(payload)
        via_write = sent.pop()

        self.assertEqual(via_write, direct)
        self.assertEqual(via_write, (connection, 0x0041, payload, False))


if __name__ == "__main__":
    unittest.main()
