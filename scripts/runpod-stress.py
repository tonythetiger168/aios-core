#!/usr/bin/env python3
"""Run scripts/stress-swarm.py against a real model on ONE rented RunPod GPU, then delete the pod.

The pod runs everything itself: vLLM serves the model on localhost, the stress ramp (1, 2, 4, ...
agents) talks to it there, and the results are served on port 8080 for this script to download.
Spending is bounded three ways: the pod deletes itself after --max-minutes even if this script
dies, this script deletes it when the run ends (or on Ctrl-C / any error), and it asks before
creating the pod with the worst-case cost. Put only a small credit on the RunPod account
(RunPod is prepaid) for a hard ceiling.

  RUNPOD_API_KEY=... python scripts/runpod-stress.py               # asks before renting
  RUNPOD_API_KEY=... python scripts/runpod-stress.py --max 16384 --max-minutes 45 --yes

Standard library only. Needs network access to rest.runpod.io and *.proxy.runpod.net.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://rest.runpod.io/v1"
# cheapest 24-48 GB cards first; USD/hour on-demand (runpod.io/gpu-cloud/pricing, 2026-10): only
# used for the cost estimate, the real price is whatever RunPod charges for the card it picks.
GPUS = {"NVIDIA RTX A5000": 0.27, "NVIDIA GeForce RTX 3090": 0.50, "NVIDIA A40": 0.59}
IMAGE = "vllm/vllm-openai:v0.6.6"
REPO = "https://github.com/tonythetiger168/aios-core"

POD_SCRIPT = r"""
set -u
mkdir -p /results && cd /results
exec > >(tee -a /results/pod.log) 2>&1
# safety net: delete this pod after the deadline even if nobody is watching
( sleep $((MAX_MINUTES*60)); curl -s -X DELETE -H "Authorization: Bearer $RUNPOD_API_KEY" \
    "https://rest.runpod.io/v1/pods/$RUNPOD_POD_ID" ) &
python3 -m http.server 8080 --directory /results >/dev/null 2>&1 &
vllm serve "$MODEL" --port 8000 --max-model-len 4096 --max-num-seqs 256 --disable-log-requests \
    > /results/vllm.log 2>&1 &
until curl -sf localhost:8000/v1/models >/dev/null; do sleep 5; done
echo "vllm ready"; nvidia-smi --query-gpu=name,memory.total --format=csv > /results/gpu.csv
git clone --depth 1 "$REPO" /aios
python3 /aios/scripts/stress-swarm.py --model-kind openai --base-url http://localhost:8000/v1 \
    --model "$MODEL" --start "$START" --factor "$FACTOR" --max "$MAX_AGENTS" --timeout 600 \
    --out /results/ramp.jsonl > /results/ramp.md 2> /results/stress.log
echo $? > /results/DONE
sleep infinity
"""


def call(method, path, key, body=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def fetch(pod, name):
    try:
        with urllib.request.urlopen(f"https://{pod}-8080.proxy.runpod.net/{name}", timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="Qwen/Qwen2.5-3B-Instruct", help="Hugging Face model vLLM serves")
    ap.add_argument("--gpu", action="append", help=f"GPU type id, in order of preference (default {list(GPUS)})")
    ap.add_argument("--cloud", choices=["COMMUNITY", "SECURE"], default="COMMUNITY")
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--factor", type=float, default=2.0)
    ap.add_argument("--max", type=int, default=16384, help="most agents at once (ramp stops earlier on failures)")
    ap.add_argument("--max-minutes", type=int, default=45, help="the pod deletes itself after this")
    ap.add_argument("--out", default="runpod-results", help="folder for the downloaded results")
    ap.add_argument("--yes", action="store_true", help="do not ask before renting")
    a = ap.parse_args()
    key = os.environ.get("RUNPOD_API_KEY")
    if not key:
        sys.exit("set RUNPOD_API_KEY (runpod.io > Settings > API Keys)")
    gpus = a.gpu or list(GPUS)
    worst = max(GPUS.get(g, 1.0) for g in gpus) * a.max_minutes / 60 + 0.05  # + disk
    print(f"1x {' / '.join(gpus)} ({a.cloud}), at most {a.max_minutes} min: worst case about US${worst:.2f}")
    if not a.yes and input("rent it? [y/N] ").strip().lower() != "y":
        return
    env = {"MODEL": a.model, "START": str(a.start), "FACTOR": str(a.factor), "MAX_AGENTS": str(a.max),
           "MAX_MINUTES": str(a.max_minutes), "REPO": REPO, "RUNPOD_API_KEY": key}
    pod = call("POST", "/pods", key, {
        "name": "aios-stress", "imageName": IMAGE, "gpuTypeIds": gpus, "gpuCount": 1, "cloudType": a.cloud,
        "containerDiskInGb": 40, "volumeInGb": 0, "ports": ["8080/http"], "env": env,
        "dockerEntrypoint": ["bash", "-c"], "dockerStartCmd": [POD_SCRIPT]})["id"]
    print(f"pod {pod} created; results at https://{pod}-8080.proxy.runpod.net/")
    t0, last = time.time(), ""
    try:
        while time.time() - t0 < a.max_minutes * 60:
            time.sleep(20)
            done = fetch(pod, "DONE")
            log = fetch(pod, "stress.log") or fetch(pod, "pod.log") or ""
            line = log.strip().splitlines()[-1][:200] if log.strip() else "starting..."
            if line != last:
                print(f"[{int(time.time() - t0)}s] {line}", flush=True)
                last = line
            if done is not None:
                break
        else:
            print("deadline reached; keeping what finished")
        os.makedirs(a.out, exist_ok=True)
        for name in ("ramp.jsonl", "ramp.md", "stress.log", "gpu.csv", "vllm.log", "pod.log"):
            body = fetch(pod, name)
            if body is not None:
                with open(os.path.join(a.out, name), "w", encoding="utf-8") as f:
                    f.write(body)
        print(fetch(pod, "ramp.md") or "(no table: see the logs in " + a.out + ")")
    finally:
        try:
            call("DELETE", f"/pods/{pod}", key)
            print(f"pod {pod} deleted after {int(time.time() - t0)}s")
        except (urllib.error.URLError, OSError) as e:
            print(f"!! could not delete pod {pod} ({e}): delete it at runpod.io/console/pods", file=sys.stderr)


if __name__ == "__main__":
    main()
