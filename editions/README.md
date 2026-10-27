# AIOS editions

**中文：[README.zh-TW.md](README.zh-TW.md)** (the per-edition guides are in Traditional Chinese)

AIOS comes in five editions by hardware: four run on your own machine, one is hosted. The four local
editions are the same code with different settings: which model server, how many agents at once, which
sandbox, what each agent may do. Each is packaged as a zip that starts with `python start.py`.

| Edition | Hardware | Model | Agents (real-time / batch) | Plan | Guide |
|---|---|---|---|---|---|
| **Lite** | any 4-core CPU, 8 GB RAM, no GPU | cloud API, your own key | ~200 (API rate limits) | Core, free | [lite](lite/GUIDE.zh-TW.md) |
| **Personal** | 8C/16T, 32 GB RAM, 16 GB VRAM | Qwen2.5 3B (Ollama) | ~64 / 2,000 | Core, free | [personal](personal/GUIDE.zh-TW.md) |
| **Workstation** | 16C/32T, 64–128 GB RAM, 32 GB VRAM | Qwen2.5 7B (vLLM) | ~200 / 6,000 | Core; Team above 3 people | [workstation](workstation/GUIDE.zh-TW.md) |
| **Server** | 32C/64T, 256 GB RAM, 96 GB VRAM, Linux | Qwen2.5 7B (vLLM) | ~150–200 / 5,000 | Team or Enterprise | [server](server/GUIDE.zh-TW.md) |
| **Cloud** | none | your own key | 5,000+ | Cloud | [cloud](cloud/GUIDE.zh-TW.md) (**not launched**) |

Agent counts are scaled from a measured run (one 24 GB card, vLLM, Qwen2.5 3B: 4,096 agents at once
with no failures, timeouts past ~5,500; see [docs/stress-test.md](../docs/stress-test.md)); the 32 GB and
96 GB cards are not measured yet.

## Pricing (USD)

| Plan | Price | Status |
|---|---|---|
| **Core** | free (MIT), every safety feature included | available |
| **Team** (self-hosted) | US$15/seat/month, billed yearly, 5 seats minimum; free up to 3 users | in development |
| **Cloud** (hosted) | US$25/seat/month incl. 2,000 runs/seat/month (pooled); US$10 per 1,000 extra runs; bring your own model key | not launched |
| **Enterprise** | quote, from about US$20,000/year | on request |

Safety features are never paywalled. A run is one task end to end; a task over 20 model calls counts
once more per 20; runs refused by policy are free.

## Build and run

```bash
python scripts/build-editions.py                       # dist/editions/aios-<edition>-<version>.zip
python editions/start.py --edition personal chat       # or run an edition from the source tree
```

Inside a bundle: `python start.py install | check | chat | daemon | swarm ... | <any aios command>`.
Edition flags go first, so flags you add win.
