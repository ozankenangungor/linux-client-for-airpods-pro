# airpods-client for Python

`airpods-client` 0.1 is the supported first Python SDK surface for consuming
heart-rate events from an already-running `airpods-hubd`. It uses only the
Python standard library and communicates through the local Unix JSONL socket.
It does not own Bluetooth, start the daemon, call systemd, or reconnect
automatically.

The standalone client supports Python 3.11 through 3.14. Its implementation is
standard-library-only, and its package, public-contract, protocol, lifecycle,
and typed-error tests run on every supported version. The production
`airpods-hr-linux` package remains a separate Python 3.14 distribution.

```python
import asyncio

from airpods_client import AirPodsClient


async def main() -> None:
    async with await AirPodsClient.connect() as client:
        status = await client.status()
        print(status.state.value)

        heart_rate = await client.subscribe_heart_rate()
        try:
            count = 0
            async for sample in heart_rate:
                print(sample.bpm, sample.source_side.value)
                count += 1
                if count == 10:
                    break
        finally:
            await heart_rate.unsubscribe()


asyncio.run(main())
```

The default socket is `$XDG_RUNTIME_DIR/airpods-hubd.sock`. Use
`AirPodsClient.connect_to(path)` for an explicit development or test socket.
The SDK returns typed errors when the runtime directory or daemon is absent;
it has no `/tmp`, TCP, autostart, or Bluetooth fallback.

The v0.1 surface is documented in `docs/sdk-v0.1-api.md` in the source
repository. Patch releases in the v0.1 line should not intentionally break
that surface. A future v0.2 may make deliberate breaking changes. The daemon
protocol remains experimental and is versioned in coordination with the SDKs.
This repository packaging candidate has not been published, and no external
package-name availability is claimed.
