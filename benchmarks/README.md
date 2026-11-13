# CPU executor baseline (Issue #105)

Run from repository root with Python 3.11+:

```bash
python benchmarks/cpu_executor_baseline.py --jobs 32 --work 12000 --workers 1 2 4 8 --output cpu-baseline.json
```

This is an **opt-in microbenchmark**, not a change to the AIOS kernel.
It compares a serial loop, an asyncio loop deliberately running blocking
pure-Python CPU work, a Python thread pool and a Python process pool.
The same deterministic result is checked in all runs.

**Interpretation:** On standard GIL-enabled CPython, CPU-heavy Python
threads usually do not scale across cores. Processes can use multiple cores,
but startup and inter-process communication cost time. SMT/Hyper-Threading
does not double throughput; `os.cpu_count()` is generally a logical CPU
count, not the number of physical cores. Respect containers/cpusets and
record physical cores, CPU affinity and SMT status from OS tools separately.
The script intentionally does **not** set affinity or change runtime policy.

Metrics are total throughput and submit-to-completion percentiles, which
include queue time, startup overhead and execution; these are **not** isolated
queue-delay percentiles. Repeat 3–5 trials on a quiet host with the same
Python build, core topology, governor/power profile and workload. Compare
mean and variance before drawing conclusions.

## Limitations / next steps

- The benchmark is CPU-only; it does not measure real AIOS agent, model,
  MCP, sandbox or I/O task behavior and does not claim a C++ speedup.
- Record CPU utilization, peak RSS, physical/logical topology and cancellation
  behavior in subsequent instrumentation.
- Try free-threaded CPython separately (check its GIL state) and report
  wheel compatibility and whether the GIL was re-enabled by imported code.
- If production profiling identifies CPU-bound scheduler overhead, create
  an opt-in native executor proof of concept with strict security regression.
- No paid GPU resources, deployment or new scheduler defaults are required.
