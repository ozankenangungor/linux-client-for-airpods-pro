"""Hardware-independent tests for the pre-authentication diagnostics."""

from __future__ import annotations


import importlib.metadata
import inspect
import unittest


from unittest.mock import patch

from bumble import hci
from bumble.device import Device
from bumble.host import Host


from airpods_hr.pre_auth_diagnostics import SUPPORTED_BUMBLE_VERSION, BumblePreAuthCompatibilityError, validate_bumble_pre_auth_api


class BumbleAPIContractTests(unittest.TestCase):
    def test_installed_bumble_0_0_234_contract_is_supported(self) -> None:
        self.assertEqual(
            importlib.metadata.version("bumble"), SUPPORTED_BUMBLE_VERSION
        )
        self.assertTrue(hasattr(Device, "send_command"))
        self.assertTrue(hasattr(Device, "send_async_command"))
        validate_bumble_pre_auth_api()


    def test_host_event_names_and_handler_signatures_match_reviewed_source(
        self,
    ) -> None:
        handlers = (
            (
                Host.on_hci_read_remote_supported_features_complete_event,
                ("classic_remote_features",),
            ),
            (
                Host.on_hci_read_remote_extended_features_complete_event,
                ("classic_remote_features",),
            ),
            (
                Host.on_hci_remote_name_request_complete_event,
                ("remote_name", "remote_name_failure"),
            ),
        )
        for handler, event_names in handlers:
            with self.subTest(handler=handler.__name__):
                self.assertEqual(
                    tuple(inspect.signature(handler).parameters),
                    ("self", "event"),
                )
                source = inspect.getsource(handler)
                for event_name in event_names:
                    self.assertIn(repr(event_name), source)

    def test_actual_host_emitters_use_reviewed_callback_argument_order(
        self,
    ) -> None:
        host = Host()
        features: list[tuple[object, ...]] = []
        names: list[tuple[object, ...]] = []
        failures: list[tuple[object, ...]] = []
        host.on("classic_remote_features", lambda *args: features.append(args))
        host.on("remote_name", lambda *args: names.append(args))
        host.on("remote_name_failure", lambda *args: failures.append(args))
        handle = 0x0123
        address = hci.Address(bytes(range(6)))

        host.on_hci_read_remote_supported_features_complete_event(
            hci.HCI_Read_Remote_Supported_Features_Complete_Event(
                status=hci.HCI_SUCCESS,
                connection_handle=handle,
                lmp_features=bytes.fromhex("0102030405060708"),
            )
        )
        host.on_hci_read_remote_extended_features_complete_event(
            hci.HCI_Read_Remote_Extended_Features_Complete_Event(
                status=hci.HCI_SUCCESS,
                connection_handle=handle,
                page_number=1,
                maximum_page_number=2,
                extended_lmp_features=bytes.fromhex("1020304050607080"),
            )
        )
        host.on_hci_remote_name_request_complete_event(
            hci.HCI_Remote_Name_Request_Complete_Event(
                status=hci.HCI_SUCCESS,
                bd_addr=address,
                remote_name=b"TEST\0",
            )
        )
        host.on_hci_remote_name_request_complete_event(
            hci.HCI_Remote_Name_Request_Complete_Event(
                status=hci.HCI_UNKNOWN_HCI_COMMAND_ERROR,
                bd_addr=address,
                remote_name=b"",
            )
        )

        self.assertEqual(
            features,
            [
                (handle, hci.HCI_SUCCESS, 0x0807060504030201, 0, 0),
                (handle, hci.HCI_SUCCESS, 0x8070605040302010, 1, 2),
            ],
        )
        self.assertEqual(names, [(address, b"TEST")])
        self.assertEqual(
            failures, [(address, hci.HCI_UNKNOWN_HCI_COMMAND_ERROR)]
        )

    def test_unreviewed_bumble_version_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            BumblePreAuthCompatibilityError, "unsupported Bumble version"
        ):
            validate_bumble_pre_auth_api(installed_version="0.0.235")

    def test_replaced_send_command_method_fails_closed(self) -> None:
        async def replacement(device, command, check_result=False):
            del device, command, check_result

        with patch.object(Device, "send_command", replacement):
            with self.assertRaisesRegex(
                BumblePreAuthCompatibilityError, "handlers were replaced"
            ):
                validate_bumble_pre_auth_api(
                    installed_version=SUPPORTED_BUMBLE_VERSION
                )


