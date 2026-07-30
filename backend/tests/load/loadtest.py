"""Load test: concurrent HTTP against a running instance.

Not a benchmark for a press release — a check for the three failure modes
that only appear under concurrency:

1. **Latency collapse.** p95 that is fine at one request in flight and
   catastrophic at fifty usually means a lock, an N+1 query, or a per-request
   engine rebuild.
2. **Errors under load.** A 500 that never appears serially is a shared
   mutable something.
3. **Rate limiter behaviour.** 429s should appear in proportion to the
   configured rule and not as a collapse.

Run against a live server:

    python3 tests/load/loadtest.py --url http://127.0.0.1:8000 \
        --concurrency 25 --requests 600
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx

#: A realistic mix: cheap reads dominate, with a handful of expensive
#: aggregate endpoints. Weighting matters — measuring only /health tells you
#: nothing about the application.
ENDPOINTS: list[tuple[str, int]] = [
    ("/health", 2),
    ("/health/ready", 1),
    ("/metrics", 1),
    ("/api/v1/companies?page_size=25", 4),
    ("/api/v1/dashboard/overview", 3),
    ("/api/v1/admin/overview", 3),
    ("/api/v1/admin/entitlements", 3),
    ("/api/v1/admin/usage?days=30", 2),
    ("/api/v1/admin/audit?page_size=25", 3),
    ("/api/v1/admin/members", 2),
    ("/api/v1/platform/overview", 2),
    ("/api/v1/platform/tenants", 2),
    ("/api/v1/platform/queue", 2),
    ("/api/v1/platform/metrics", 1),
]


@dataclass
class Result:
    latencies: dict[str, list[float]] = field(default_factory=dict)
    statuses: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)

    def record(self, path: str, ms: float, status: int) -> None:
        self.latencies.setdefault(path, []).append(ms)
        self.statuses[status] += 1


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(p * len(ordered))) - 1))
    return round(ordered[index], 2)


async def _worker(
    client: httpx.AsyncClient, plan: list[str], result: Result, lock: asyncio.Lock,
) -> None:
    while True:
        async with lock:
            if not plan:
                return
            path = plan.pop()

        started = time.perf_counter()
        try:
            response = await client.get(path)
            elapsed = (time.perf_counter() - started) * 1000
            result.record(path, elapsed, response.status_code)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"{path}: {type(exc).__name__}: {exc}")


async def run(url: str, concurrency: int, total: int) -> Result:
    weighted: list[str] = []
    for path, weight in ENDPOINTS:
        weighted.extend([path] * weight)

    plan = [weighted[i % len(weighted)] for i in range(total)]
    result = Result()
    lock = asyncio.Lock()

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=url, timeout=30, limits=limits) as client:
        started = time.perf_counter()
        await asyncio.gather(*[
            _worker(client, plan, result, lock) for _ in range(concurrency)
        ])
        result.wall_seconds = time.perf_counter() - started  # type: ignore[attr-defined]
    return result


def report(result: Result, concurrency: int) -> int:
    everything = [ms for values in result.latencies.values() for ms in values]
    completed = len(everything)
    wall = getattr(result, "wall_seconds", 0.0) or 1.0

    print()
    print("=" * 78)
    print(f"LOAD TEST — concurrency {concurrency}, {completed} requests in {wall:.2f}s")
    print("=" * 78)
    print(f"  throughput      {completed / wall:8.1f} req/s")
    print(f"  mean            {statistics.mean(everything):8.2f} ms")
    print(f"  median          {statistics.median(everything):8.2f} ms")
    print(f"  p95             {_percentile(everything, 0.95):8.2f} ms")
    print(f"  p99             {_percentile(everything, 0.99):8.2f} ms")
    print(f"  max             {max(everything):8.2f} ms")
    print()
    print("  status codes:", dict(sorted(result.statuses.items())))
    if result.errors:
        print(f"  transport errors: {len(result.errors)}")
        for message in result.errors[:5]:
            print(f"    {message}")

    print()
    print(f"  {'endpoint':<48}{'n':>5}{'p50':>9}{'p95':>9}")
    print(f"  {'-' * 70}")
    for path, values in sorted(
        result.latencies.items(), key=lambda kv: -_percentile(kv[1], 0.95),
    ):
        print(
            f"  {path[:47]:<48}{len(values):>5}"
            f"{_percentile(values, 0.50):>9.2f}{_percentile(values, 0.95):>9.2f}"
        )

    # The gate. A 5xx under load is always a defect; 429 is the limiter
    # working and is reported rather than failed.
    server_errors = sum(count for code, count in result.statuses.items() if code >= 500)
    print()
    if server_errors:
        print(f"  FAIL — {server_errors} server error(s) under load")
        return 1
    if result.errors:
        print(f"  FAIL — {len(result.errors)} transport error(s)")
        return 1
    print("  PASS — no server errors under load")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--concurrency", type=int, default=25)
    parser.add_argument("--requests", type=int, default=600)
    args = parser.parse_args()

    result = asyncio.run(run(args.url, args.concurrency, args.requests))
    return report(result, args.concurrency)


if __name__ == "__main__":
    raise SystemExit(main())
