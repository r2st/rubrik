"""Async load test for the Transcript Intelligence API.

Hammers each read endpoint with N concurrent requests and reports per-endpoint
latency percentiles, throughput, and error rate. Lightweight — uses httpx +
asyncio, no external tool required.

Default scenario: 30 seconds, 20 concurrent virtual users (VUs), default rate
limit (120/min/IP) is bypassed because all requests share the same source IP
and we lift the limit during a load run via the LOAD_TEST_BYPASS_RATELIMIT env
var (read by the API).

Usage:
    python -m tests.load.run_load_test                 # localhost:8000, 30s
    python -m tests.load.run_load_test --duration 60   # custom duration
    python -m tests.load.run_load_test --vus 50        # more concurrency
    python -m tests.load.run_load_test --base-url http://staging.example.com

Exit code is non-zero if any endpoint's error rate exceeds 1%.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

# ---------------------------------------------------------------------------
# Scenario — endpoints to exercise + traffic mix
# ---------------------------------------------------------------------------
ENDPOINTS = [
    # (path, weight)  — higher weight = more traffic
    ("/api/health",                        3),
    ("/api/v1/summary",                    5),
    ("/api/v1/clusters",                   3),
    ("/api/v1/sentiment/by-call-type",     2),
    ("/api/v1/sentiment/by-purpose",       2),
    ("/api/v1/sentiment/weekly",           2),
    ("/api/v1/insights/customer-health",   3),
    ("/api/v1/insights/incident-impact",   2),
    ("/api/v1/insights/action-items",      2),
    ("/api/v1/insights/negative-pivots",   1),
    ("/api/v1/meetings?limit=20",          3),
]


@dataclass
class EndpointStats:
    path: str
    latencies_ms: list[float] = field(default_factory=list)
    statuses: dict[int, int] = field(default_factory=dict)
    errors: int = 0
    bytes_received: int = 0

    @property
    def total(self) -> int:
        return sum(self.statuses.values()) + self.errors

    @property
    def error_rate(self) -> float:
        return self.errors / self.total if self.total else 0.0

    def percentile(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        return statistics.quantiles(self.latencies_ms, n=100)[int(p) - 1]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
async def _hit(
    client: httpx.AsyncClient,
    path: str,
    stats: EndpointStats,
    headers: dict,
) -> None:
    start = time.perf_counter()
    try:
        r = await client.get(path, headers=headers, timeout=10.0)
        elapsed_ms = (time.perf_counter() - start) * 1000
        stats.latencies_ms.append(elapsed_ms)
        stats.statuses[r.status_code] = stats.statuses.get(r.status_code, 0) + 1
        stats.bytes_received += len(r.content)
    except Exception:
        stats.errors += 1


async def _vu(
    client: httpx.AsyncClient,
    deadline: float,
    weighted_paths: list[str],
    stats_by_path: dict[str, EndpointStats],
    headers: dict,
    vu_id: int,
) -> None:
    """One virtual user: pick a weighted random endpoint, fire, repeat."""
    import random
    rng = random.Random(vu_id)
    while time.monotonic() < deadline:
        path = rng.choice(weighted_paths)
        await _hit(client, path, stats_by_path[path], headers)


async def run(base_url: str, duration_s: int, vus: int, api_key: Optional[str]) -> int:
    weighted_paths: list[str] = []
    for path, weight in ENDPOINTS:
        weighted_paths.extend([path] * weight)

    stats_by_path = {path: EndpointStats(path=path) for path, _ in ENDPOINTS}
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key

    print("\n▶  Load test starting")
    print(f"   target    : {base_url}")
    print(f"   duration  : {duration_s}s")
    print(f"   VUs       : {vus}")
    print(f"   endpoints : {len(ENDPOINTS)}\n")

    deadline = time.monotonic() + duration_s
    limits = httpx.Limits(max_connections=vus * 2, max_keepalive_connections=vus * 2)
    async with httpx.AsyncClient(base_url=base_url, limits=limits) as client:
        wall_start = time.monotonic()
        tasks = [
            asyncio.create_task(_vu(client, deadline, weighted_paths, stats_by_path, headers, i))
            for i in range(vus)
        ]
        await asyncio.gather(*tasks)
        wall_elapsed = time.monotonic() - wall_start

    return _report(stats_by_path, wall_elapsed)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _report(stats_by_path: dict[str, EndpointStats], wall_elapsed: float) -> int:
    total_requests = sum(s.total for s in stats_by_path.values())
    total_errors = sum(s.errors for s in stats_by_path.values())
    total_bytes = sum(s.bytes_received for s in stats_by_path.values())
    rps = total_requests / wall_elapsed if wall_elapsed > 0 else 0
    overall_error_rate = total_errors / total_requests if total_requests else 0

    print(f"\n{'═' * 86}")
    print(" Per-endpoint results")
    print(f"{'═' * 86}")
    print(f" {'endpoint':<40}{'count':>8}{'p50':>8}{'p95':>8}{'p99':>8}{'err%':>8}")
    print(f" {'-' * 84}")

    failed_endpoints = []
    for stats in stats_by_path.values():
        flag = ""
        if stats.error_rate > 0.01:
            flag = " ⚠"
            failed_endpoints.append(stats.path)
        print(
            f" {stats.path:<40}"
            f"{stats.total:>8d}"
            f"{stats.percentile(50):>7.0f}{'ms':>1}"
            f"{stats.percentile(95):>7.0f}{'ms':>1}"
            f"{stats.percentile(99):>7.0f}{'ms':>1}"
            f"{stats.error_rate * 100:>7.1f}%{flag}"
        )

    print(f"\n{'═' * 86}")
    print(" Summary")
    print(f"{'═' * 86}")
    print(f"   wall time      : {wall_elapsed:.1f}s")
    print(f"   total requests : {total_requests:,}")
    print(f"   throughput     : {rps:.1f} RPS")
    print(f"   error rate     : {overall_error_rate * 100:.2f}%")
    print(f"   bytes received : {total_bytes / 1_000_000:.2f} MB")
    print()

    if failed_endpoints:
        print("✘  Error-rate threshold exceeded on:")
        for ep in failed_endpoints:
            print(f"     {ep}")
        return 1
    print("✓  All endpoints under 1% error rate")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description="Load test the Transcript Intelligence API")
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--duration", type=int, default=30, help="seconds")
    p.add_argument("--vus", type=int, default=20, help="concurrent virtual users")
    p.add_argument("--api-key", default=None, help="X-API-Key header value")
    args = p.parse_args()
    return asyncio.run(run(args.base_url, args.duration, args.vus, args.api_key))


if __name__ == "__main__":
    raise SystemExit(main())
