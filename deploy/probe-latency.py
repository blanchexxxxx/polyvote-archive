#!/usr/bin/env python3
"""Measure warm CLOB RTT separately over IPv4 and IPv6."""
from __future__ import annotations

import asyncio
import json
import math
import socket
import statistics
import time

import aiohttp


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * p) - 1)]


async def measure(family: int, samples: int = 30) -> dict:
    label = "ipv4" if family == socket.AF_INET else "ipv6"
    timeout = aiohttp.ClientTimeout(total=5, connect=2)
    connector = aiohttp.TCPConnector(
        family=family,
        limit=2,
        ttl_dns_cache=300,
        keepalive_timeout=60,
    )
    values: list[float] = []
    errors: list[str] = []
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        for index in range(samples + 1):
            began = time.perf_counter()
            try:
                async with session.get("https://clob.polymarket.com/time") as response:
                    await response.read()
                    response.raise_for_status()
                elapsed = (time.perf_counter() - began) * 1000
                if index:
                    values.append(elapsed)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(0.1)
    return {
        "family": label,
        "samples": len(values),
        "p50_ms": round(statistics.median(values), 2) if values else None,
        "p95_ms": round(percentile(values, 0.95), 2) if values else None,
        "p99_ms": round(percentile(values, 0.99), 2) if values else None,
        "max_ms": round(max(values), 2) if values else None,
        "errors": errors[:3],
    }


async def main() -> None:
    results = []
    for family in (socket.AF_INET, socket.AF_INET6):
        results.append(await measure(family))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
