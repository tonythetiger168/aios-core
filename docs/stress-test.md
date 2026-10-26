# Swarm load test: how many agents at once?

`scripts/stress-swarm.py` runs N helper agents **at the same time** through `run_swarm`, the engine
behind the `swarm` tool and `aios swarm`, and multiplies N each level (1, 2, 4, ... by default) until
something breaks. Permission checks, delegation, the shared ledger, model slots and the hash-chained
audit log on a real SQLite file all run as in production; only the model is swapped:

| `--model-kind` | model | what it measures |
|---|---|---|
| `mock` | in-process, sleeps `--latency` s | AIOS alone (scheduler, kernel, audit) |
| `http-mock` | fake OpenAI-compatible server in another process | + the real `OpenAICompatProvider` (HTTP, threads, sockets) |
| `openai` | a real server (vLLM, Ollama, llama.cpp...) | the whole stack; the GPU is usually the limit |

Each level runs in its own process (peak memory is per level). Columns: `wall s` = all N done;
`p50/p99 s` = when the median / 99th-percentile agent finished, counted from the start; `model p95 s`
= time inside one model call; `loop lag s` = worst event-loop delay (above ~0.1 s the single Python
thread is the bottleneck).

```
python scripts/stress-swarm.py                            # 1, 2, 4, ... 131072
python scripts/stress-swarm.py --factor 10 --max 10000    # 1, 10, 100, 1000, 10000
python scripts/stress-swarm.py --model-kind openai --base-url http://localhost:8000/v1 --model Qwen/Qwen2.5-3B-Instruct
RUNPOD_API_KEY=... python scripts/runpod-stress.py        # one rented GPU, see below
```

## Results (4-vCPU cloud container, Python 3.11, fake model answering in 1 s)

### AIOS alone (`mock`)

| agents | ok | fail | wall s | tasks/s | in flight | p50 s | p99 s | model p95 s | CPU ms/task | loop lag s | RSS MB |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | 0.0 | 1.003 | 1.0 | 1 | 1.003 | 1.003 | 1.001 | 6.13 | 0.001 | 27.0 |
| 2 | 2 | 0.0 | 1.003 | 2.0 | 2 | 1.003 | 1.003 | 1.002 | 3.41 | 0.001 | 26.9 |
| 4 | 4 | 0.0 | 1.003 | 4.0 | 4 | 1.003 | 1.003 | 1.002 | 1.74 | 0.001 | 27.1 |
| 8 | 8 | 0.0 | 1.005 | 8.0 | 8 | 1.005 | 1.005 | 1.004 | 1.11 | 0.002 | 27.2 |
| 16 | 16 | 0.0 | 1.006 | 15.9 | 16 | 1.005 | 1.005 | 1.003 | 0.69 | 0.001 | 27.1 |
| 32 | 32 | 0.0 | 1.007 | 31.8 | 32 | 1.007 | 1.007 | 1.004 | 0.4 | 0.003 | 27.2 |
| 64 | 64 | 0.0 | 1.013 | 63.2 | 64 | 1.012 | 1.012 | 1.006 | 0.32 | 0.004 | 27.4 |
| 128 | 128 | 0.0 | 1.022 | 125.3 | 128 | 1.018 | 1.022 | 1.012 | 0.25 | 0.011 | 27.9 |
| 256 | 256 | 0.0 | 1.095 | 233.7 | 256 | 1.094 | 1.095 | 1.076 | 0.39 | 0.032 | 29.3 |
| 512 | 512 | 0.0 | 1.154 | 443.8 | 512 | 1.15 | 1.151 | 1.11 | 0.31 | 0.077 | 31.3 |
| 1024 | 1024 | 0.0 | 1.242 | 824.4 | 1024 | 1.231 | 1.235 | 1.117 | 0.27 | 0.084 | 35.6 |
| 2048 | 2048 | 0.0 | 1.639 | 1249.8 | 2048 | 1.61 | 1.625 | 1.387 | 0.3 | 0.39 | 43.9 |
| 4096 | 4096 | 0.0 | 1.853 | 2209.9 | 4096 | 1.834 | 1.841 | 1.479 | 0.22 | 0.348 | 61.3 |
| 8192 | 8192 | 0.0 | 3.525 | 2324.3 | 8192 | 3.393 | 3.447 | 2.486 | 0.3 | 1.325 | 94.7 |
| 16384 | 16384 | 0.0 | 6.205 | 2640.2 | 16384 | 5.139 | 6.07 | 3.445 | 0.34 | 2.451 | 164.9 |
| 32768 | 32768 | 0.0 | 13.387 | 2447.7 | 32768 | 12.402 | 13.288 | 7.03 | 0.37 | 5.986 | 301.8 |
| 65536 | 65536 | 0.0 | 30.824 | 2126.1 | 65536 | 30.316 | 30.517 | 14.476 | 0.41 | 14.706 | 576.9 |
| 131072 | 131072 | 0.0 | 57.262 | 2289.0 | 131072 | 56.067 | 56.828 | 26.654 | 0.39 | 29.137 | 1128.8 |

No failures up to 131,072 agents at once. Up to ~1,000 agents the whole swarm finishes in the
model's 1 s, so **adding agents makes the batch faster almost linearly** (10 agents: 10 tasks/s;
1,000: 824 tasks/s). Past ~4,000 the single Python thread saturates at **~2,300 tasks/s** (about
0.3 ms of CPU per agent: delegation, ledger, one audit row each); more agents then only make each
one wait longer (10,000: 3.4 s, 131,072: 57 s) while throughput stays flat. Memory is ~8 KB per
agent (111 MB at 10,000, 1.1 GB at 131,072).

### Through HTTP (`http-mock`), before and after the thread-pool fix

`OpenAICompatProvider` uses blocking `urllib` in a thread. It used Python's shared default pool,
which has `min(32, CPUs + 4)` threads: **8 on this machine, whatever `--model-slots` said**.

| agents | before: wall s | before: fail | after: wall s | after: fail | after: tasks/s |
|---|---|---|---|---|---|
| 10 | 2.0 | 0 | 1.03 | 0 | 9.7 |
| 100 | 13.1 | 0 | 1.13 | 0 | 88 |
| 1,000 | 120 (timeouts) | 4.8% | 2.6 | 0 | 388 |
| 10,000 | 122 (timeouts) | 90.5% | 20.7 | 0 | 484 |

The provider now has its own pool, sized to the kernel's model slots. The HTTP path costs ~2.3 ms
of CPU per call (thread + urllib + JSON under the GIL), so one process tops out near **~450 HTTP
model calls/s**; a real model server on one GPU is slower than that, so the GPU stays the limit.

### Real model on one rented GPU (2026-10-26)

`scripts/runpod-stress.py` on RunPod: **1x RTX 3090 24 GB** (community cloud), vLLM 0.6.6 serving
`Qwen/Qwen2.5-3B-Instruct` (`--max-num-seqs 256`), the ramp running inside the same pod. The pod
lived 699 s (about US$0.05). Each agent asks for a one-word answer.

| agents | ok | fail | wall s | tasks/s | p50 s | p99 s | RSS MB |
|---|---|---|---|---|---|---|---|
| 1 | 1 | 0 | 0.07 | 13.6 | 0.07 | 0.07 | 40 |
| 16 | 16 | 0 | 0.29 | 54.8 | 0.29 | 0.29 | 29 |
| 128 | 128 | 0 | 1.61 | 79.6 | 1.59 | 1.60 | 34 |
| 512 | 512 | 0 | 6.27 | 81.6 | 6.24 | 6.27 | 53 |
| 1,024 | 1,024 | 0 | 18.2 | 56.3 | 12.2 | 18.2 | 117 |
| 2,048 | 2,048 | 0 | 33.6 | 61.0 | 22.8 | 33.6 | 199 |
| 4,096 | 4,096 | 0 | 51.7 | 79.2 | 29.6 | 51.6 | 293 |
| 8,192 | 5,495 | 33% | 83.9 | 65.5 | 20.7 | 80.7 | 383 |
| 16,384 | 5,495 | 66% | 91.2 | 60.3 | 4.7 | 81.7 | 427 |

- Up to 4,096 agents at once: no failures. Throughput rises from 14 to ~55 agents/s by 16 agents,
  then stays at **55-80 agents/s**: from there every extra agent only waits longer (p99 grows with
  the queue: 1.6 s at 128, 52 s at 4,096). The limit is the model server on that pod, not AIOS.
- 8,192 and up failed with `RuntimeError: can't start new thread` after ~5,500 threads: the HTTP
  provider kept one thread per model slot and the container caps threads. Fixed: the provider now
  uses at most `MODEL_THREADS_MAX` (512) threads and further calls queue for one. With the fake HTTP
  server, 10,000 agents still finish in 21 s with no failures (and the event loop lags 3.5 s, not 17 s).
- Rerun with the fix (same card type, `--start 1250 --max 10000`, about US$0.03):

| agents | ok | fail | wall s | tasks/s | p50 s | p99 s | RSS MB |
|---|---|---|---|---|---|---|---|
| 1,250 | 1,250 | 0 | 15.5 | 80.8 | 9.6 | 15.3 | 63 |
| 2,500 | 2,500 | 0 | 30.7 | 81.4 | 15.8 | 30.7 | 72 |
| 5,000 | 5,000 | 0 | 65.3 | 76.5 | 36.4 | 65.3 | 115 |
| 10,000 | 10,000 | 0 | 128.0 | 78.1 | 67.9 | 128.0 | 169 |

  **10,000 real agents at once, no failures, all done in 128 s** (~78 agents/s, the same GPU ceiling).
  To finish faster, add GPUs (one vLLM per card, or tensor/data parallel) or a faster card; AIOS is
  not the limit.

## What it takes to run 10,000 real agents at once

AIOS itself holds 10,000 concurrent agents in one process (~110-190 MB). With a real model, the
number of agents *in flight* is easy; the number *finishing per second* is what the GPU can decode:

- **One shared inference server**, not one model per agent. vLLM batches concurrent requests
  (`--max-num-seqs`, 256 by default); the rest wait in its queue. 10,000 agents = 10,000 queued
  requests, served ~256 at a time.
- **`--model-slots`** at least the server's batch size (e.g. 256-512). Higher only moves the queue
  from AIOS into vLLM.
- **Short tasks**: time to finish all 10,000 is roughly 10,000 x tokens per task / GPU tokens per
  second. A 3B model on one 24 GB card decodes a few thousand tokens/s batched, so 10,000 short
  answers take minutes, not seconds.
- Above ~450 model calls/s, run several AIOS processes (e.g. split the task file) against the same
  server.

## Real GPU on RunPod (`scripts/runpod-stress.py`)

Rents **one** GPU (the first free 16-48 GB card under `--max-price`, US$0.60/h by default: A4000, A4500, A5000, 3090, 4090, L4, A40...; community cloud first, then secure), runs vLLM
with `Qwen/Qwen2.5-3B-Instruct` and the ramp up to 16,384 agents *inside* the pod (localhost, no
proxy in between), downloads the results, and deletes the pod. Spending is capped by: a confirmation
with the worst-case cost before renting, the pod deleting itself after `--max-minutes` (default 45)
even if the launcher dies, the launcher deleting it on exit / Ctrl-C, and RunPod being prepaid
(load only a few dollars of credit). Worst case at the defaults: 45 min x US$0.59/h ~ **US$0.50**.

| GPU (RunPod, on-demand, 2026-10) | VRAM | US$/h | fits |
|---|---|---|---|
| RTX A5000 | 24 GB | 0.27 | 3B-7B (AWQ) - cheapest start |
| RTX 3090 | 24 GB | 0.50 | 3B-7B |
| A40 | 48 GB | 0.59 | 7B-14B, bigger batch |
| L40S | 48 GB | 1.09 | 7B-14B, ~2x faster |
| A100 80 GB | 80 GB | 1.79 | 32B, or 7B with a very large batch |
| H100 SXM | 80 GB | 3.99 | fastest single card |
