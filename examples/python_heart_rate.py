"""Experimental Python SDK example for an already-running airpods-hubd."""

from __future__ import annotations

import asyncio

from airpods_client import AirPodsClient


async def main() -> None:
    async with await AirPodsClient.connect() as client:
        status = await client.status()
        print(f"daemon state: {status.state.value}")

        subscription = await client.subscribe_heart_rate()
        try:
            for _ in range(10):
                sample = await subscription.next()
                if sample is None:
                    break
                side = sample.source_side.value
                if sample.source_side_raw is not None:
                    side = f"unknown({sample.source_side_raw})"
                print(f"{sample.bpm} bpm ({side})")
        finally:
            await subscription.unsubscribe()


if __name__ == "__main__":
    asyncio.run(main())
