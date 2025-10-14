"""Hardware-independent parity tests for the temporary Classic runtime."""

from __future__ import annotations

import inspect
import unittest
from dataclasses import fields


from bumble.device import Device, DeviceConfiguration
from bumble.host import Host


from airpods_hr.classic_diagnostics import POWER_ON_CLASSIC_WRITES, POWER_ON_NOT_EXPLICITLY_WRITTEN, DeviceConfigurationAudit, RuntimeNameProfile, audit_device_configuration, local_name_matches_profile, runtime_name_for_profile


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


