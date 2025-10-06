"""Experimentally verified, machine-independent AAP protocol constants."""

from enum import Enum

AAP_PSM = 0x1001
HEART_RATE_SERVICE_ID = 0x13
HEART_RATE_MARKER = bytes.fromhex("3a 16 08 13 1a 12")
HEART_RATE_REPORT_SIZE = 18
HEART_RATE_REPORT_ID = 0x01


class HeartRateCommand(Enum):
    """Closed set of operationally named payloads proven by the HR PoC."""

    STOP_HEAD = bytes.fromhex(
        "04 00 04 00 17 00 00 00 10 00 10 00 "
        "08 86 01 42 0b 08 0e 10 02 1a 05 01 00 00 00 00"
    )
    CONNECT0 = bytes.fromhex(
        "00 00 00 00 01 00 03 00 00 00 00 00 00 00 00 00"
    )
    CAPS0 = bytes.fromhex("04 00 00 00 01 00 00")
    CONNECT4 = bytes.fromhex(
        "00 00 04 00 01 00 03 00 00 00 00 00 00 00 00 00"
    )
    CAPS4 = bytes.fromhex("04 00 04 00 01 00 00")
    HR_ON = bytes.fromhex("04 00 04 00 09 00 30 01 00 00 00")
    START_HR = bytes.fromhex(
        "04 00 04 00 17 00 00 00 10 00 10 00 "
        "08 e3 46 42 0b 08 13 10 02 1a 05 01 40 42 0f 00"
    )
    STOP_HR = bytes.fromhex(
        "04 00 04 00 17 00 00 00 10 00 10 00 "
        "08 e2 46 42 0b 08 13 10 02 1a 05 01 00 00 00 00"
    )
    HR_OFF = bytes.fromhex("04 00 04 00 09 00 30 00 00 00 00")

    @property
    def payload(self) -> bytes:
        return self.value
