"""Validate the frozen client API from a fresh installed environment."""
import asyncio
import importlib.metadata as metadata
import os
import sys
import tempfile
from pathlib import Path
import airpods_client as client

distribution = metadata.distribution("airpods-client")
assert distribution.version == "0.1.1" and not distribution.requires
assert "PYTHONPATH" not in os.environ
installed = Path(distribution.locate_file("")).resolve()
assert "site-packages" in installed.parts and installed.is_relative_to(Path(sys.prefix).resolve())
assert Path(client.__file__).resolve().is_relative_to(installed)
expected = {
    "AirPodsClient", "AirPodsClientError", "ConnectionClosed", "ConnectionFailed",
    "DaemonError", "DaemonState", "EventBufferFull", "FrameTooLarge", "HeartRateSample",
    "HeartRateSubscription", "Hello", "InvalidMessage", "ProtocolVersionError", "SourceSide",
    "Status", "SubscriptionActive", "XdgRuntimeDirMissing",
}
assert set(client.__all__) == expected
assert all(hasattr(client, name) for name in expected)
assert all(hasattr(client.AirPodsClient, name) for name in
           ("connect", "connect_to", "hello", "ping", "status", "subscribe_heart_rate", "close"))
assert "airpods_hr" not in sys.modules and "bumble" not in sys.modules

async def typed_errors():
    os.environ.pop("XDG_RUNTIME_DIR", None)
    try:
        await client.AirPodsClient.connect()
    except client.XdgRuntimeDirMissing:
        pass
    else:
        raise AssertionError("missing XDG not typed")
    with tempfile.TemporaryDirectory() as directory:
        try:
            await client.AirPodsClient.connect_to(Path(directory) / "missing.sock")
        except client.ConnectionFailed:
            pass
        else:
            raise AssertionError("missing daemon not typed")

asyncio.run(typed_errors())
print("installed Python client consumer PASS")
