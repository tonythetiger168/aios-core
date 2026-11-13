#!/usr/bin/env python3
"""Opt-in, dependency-free CPU executor baseline. Not a production scheduler.

Usage:
    python benchmarks/cpu_executor_baseline.py --jobs 32 --work 12000 --workers 1 2 4 8

This deliberately uses pure-Python CPU work. Threads therefore normally remain
GIL-limited on standard CPython. Results are NOT a benchmark of LLM inference,
I/O-bound agents, C++ native code or AIOS's production scheduling.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import json
import os
import platform
import statistics
import sys
import time


def count_primes(limit: int) -> int:
    """Deterministic, CPU-heavy Python workload with no external dependencies."""
    found = 0
    for value in range(2, limit):
        prime = True
        divisor = 2
        while divisor * divisor <= value:
            if value % divisor == 0:
                prime = False
                break
            divisor += 1
        if prime:
            found += 1
    return found


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    rank = fraction * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def run_serial(jobs: int, work: int) -> tuple[list[int], list[float]]:
    start = time.perf_counter()
    results, elapsed = [], []
    for _ in range(jobs):
        results.append(count_primes(work))
        elapsed.append(time.perf_counter() - start)
    return results, elapsed


async def run_asyncio(jobs: int, work: int) -> tuple[list[int], list[float]]:
    start = time.perf_counter()
    async def task() -> tuple[int, float]:
        result = count_primes(work)  # intentionally blocks event loop (CPU-bound)
        return result, time.perf_counter() - start
    completed = await asyncio.gather(*(task() for _ in range(jobs)))
    return [value for value, _ in completed], [elapsed for _, elapsed in completed]


def run_pool(kind: str, workers: int, jobs: int, work: int) -> tuple[list[int], list[float]]:
    executor_class = ThreadPoolExecutor if kind == "threads" else ProcessPoolExecutor
    start = time.perf_counter()
    with executor_class(max_workers=workers) as pool:
        futures = [pool.submit(count_primes, work) for _ in range(jobs)]
        results, elapsed = [], []
        for future in as_completed(futures):
            results.append(future.result())
            elapsed.append(time.perf_counter() - start)
    return results, elapsed


def measure(kind: str, workers: int, jobs: int, work: int, expected: int) -> dict:
    start = time.perf_counter()
    if kind == "serial":
        results, latencies = run_serial(jobs, work)
    elif kind == "asyncio":
        results, latencies = asyncio.run(run_asyncio(jobs, work))
    else:
        results, latencies = run_pool(kind, workers, jobs, work)
    wall = time.perf_counter() - start
    if len(results) != jobs or any(value != expected for value in results):
        raise RuntimeError(f"incorrect result from {kind} with {workers} workers")
    return {
        "executor": kind,
        "workers": workers,
        "jobs": jobs,
        "work_upper_bound": work,
        "seconds": round(wall, 6),
        "jobs_per_second": round(jobs / wall, 3),
        "completion_p50_ms": round(percentile(latencies, 0.50) * 1000, 3),
        "completion_p95_ms": round(percentile(latencies, 0.95) * 1000, 3),
        "completion_p99_ms": round(percentile(latencies, 0.99) * 1000, 3),
        "checksum": expected,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=32)
    parser.add_argument("--work", type=int, default=12000)
    parser.add_argument("--workers", nargs="+", type=int, default=[1, 2, 4, 8])
    parser.add_argument("--output", help="Optional JSON output path")
    args = parser.parse_args()
    if args.jobs < 1 or args.work < 3 or not args.workers or min(args.workers) < 1:
        parser.error("jobs and workers must be >= 1, work must be >= 3")
    if args.jobs > 10000 or args.work > 1000000 or max(args.workers) > 64:
        parser.error("safety cap: jobs <= 10000, work <= 1000000, workers <= 64")
    expected = count_primes(args.work)
    allowed = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    metadata = {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "logical_cpus_os": os.cpu_count(),
        "affinity_eligible_logical_cpus": allowed,
        "physical_cores": None,
        "smt_status": "not detected (record externally)",
        "note": "Completion times are submit-to-finish, not queue-only latency; processes include startup/IPC. Run repeated trials on a stable machine.",
    }
    rows = [measure("serial", 1, args.jobs, args.work, expected),
            measure("asyncio", 1, args.jobs, args.work, expected)]
    for workers in args.workers:
        for kind in ("threads", "processes"):
            rows.append(measure(kind, workers, args.jobs, args.work, expected))
    output = {"metadata": metadata, "results": rows}
    rendered = json.dumps(output, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as file:
            file.write(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
