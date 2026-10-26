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
# Cards that fit a 3B model (16-48 GB), cheapest first, with a rough upper bound of their on-demand
# USD/hour per cloud (runpod.io/gpu-cloud/pricing, 2026-10). Only used to pick cards under
# --max-price and for the estimate; RunPod charges its live price for the card it assigns.
GPUS = {  # id: (COMMUNITY, SECURE)
    "NVIDIA RTX A4000": (0.20, 0.25), "NVIDIA RTX 4000 Ada Generation": (0.25, 0.30),
    "NVIDIA RTX A4500": (0.25, 0.30), "NVIDIA RTX A5000": (0.27, 0.30),
    "NVIDIA GeForce RTX 3090": (0.30, 0.50), "NVIDIA GeForce RTX 3090 Ti": (0.35, 0.55),
    "NVIDIA GeForce RTX 4090": (0.40, 0.89), "NVIDIA L4": (0.45, 0.59), "NVIDIA A40": (0.45, 0.59),
}
CLOUDS = ("COMMUNITY", "SECURE")
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
git init -q /aios && git -C /aios fetch -q --depth 1 "$REPO" "$REF" && git -C /aios checkout -q FETCH_HEAD
python3 /aios/scripts/stress-swarm.py --model-kind openai --base-url http://localhost:8000/v1 \
    --model "$MODEL" --start "$START" --factor "$FACTOR" --max "$MAX_AGENTS" --timeout 600 \
    --out /results/ramp.jsonl > /results/ramp.md 2> /results/stress.log
echo $? > /results/DONE
sleep infinity
"""


# RunPod sits behind Cloudflare, which answers 403 to urllib's default "Python-urllib/3.x" agent
UA = {"User-Agent": "aios-runpod-stress/1.0 (+https://github.com/tonythetiger168/aios-core)"}


class ApiError(RuntimeError):
    pass


def call(method, path, key, body=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={**UA, "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise ApiError(f"RunPod {method} {path}: HTTP {e.code}: {detail or e.reason}") from None
    return json.loads(raw) if raw else {}


def fetch(pod, name):
    try:
        req = urllib.request.Request(f"https://{pod}-8080.proxy.runpod.net/{name}", headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", default="main", help="branch, tag or commit of aios-core the pod tests")
    ap.add_argument("--model", default="Qwen/Qwen2.5-3B-Instruct", help="Hugging Face model vLLM serves")
    ap.add_argument("--gpu", action="append", help="GPU type id, in order of preference (default: every card "
                    "in GPUS under --max-price)")
    ap.add_argument("--cloud", choices=["COMMUNITY", "SECURE", "ANY"], default="ANY",
                    help="ANY: community first, secure if community has no card free")
    ap.add_argument("--max-price", type=float, default=0.60, help="most USD/hour a card may cost")
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
    try:  # free check first: a bad key or a blocked client fails here, before anything is rented
        call("GET", "/pods", key)
    except (ApiError, urllib.error.URLError) as e:
        sys.exit(f"{e}\n401/403: check the key is a Read/Write (or All) API key and the account has credit")
    plan = []  # (cloud, cards that cloud may use)
    for i, cloud in enumerate(CLOUDS):
        if a.cloud in (cloud, "ANY"):
            cards = [g for g in (a.gpu or GPUS) if GPUS.get(g, (9, 9))[i] <= a.max_price]
            if cards:
                plan.append((cloud, cards))
    if not plan:
        sys.exit(f"no card in the list costs at most US${a.max_price}/h")
    top = max(GPUS.get(g, (9, 9))[CLOUDS.index(c)] for c, cards in plan for g in cards)
    worst = top * a.max_minutes / 60 + 0.05  # + disk
    for cloud, cards in plan:
        print(f"{cloud}: {', '.join(g.replace('NVIDIA ', '') for g in cards)}")
    print(f"1 card (the first free one above, at most ~US${top:.2f}/h), at most {a.max_minutes} min: "
          f"worst case about US${worst:.2f}")
    if not a.yes and input("rent it? [y/N] ").strip().lower() != "y":
        return
    env = {"MODEL": a.model, "START": str(a.start), "FACTOR": str(a.factor), "MAX_AGENTS": str(a.max),
           "MAX_MINUTES": str(a.max_minutes), "REPO": REPO, "REF": a.ref, "RUNPOD_API_KEY": key}
    pod = None
    for cloud, cards in plan:
        try:
            pod = create(key, cloud, cards, env)
            break
        except ApiError as e:
            if "no instances" not in str(e).lower():
                sys.exit(f"{e}\nnothing was rented")
            print(f"{cloud}: no card free right now")
    if pod is None:
        sys.exit("no card free in any cloud right now; nothing was rented. Try again in a while.")
    print(f"pod {pod} created; results at https://{pod}-8080.proxy.runpod.net/")
    watch(pod, key, a)


def create(key, cloud, gpus, env):
    return call("POST", "/pods", key, {
        "name": "aios-stress", "imageName": IMAGE, "gpuTypeIds": gpus, "gpuCount": 1, "cloudType": cloud,
        "containerDiskInGb": 40, "volumeInGb": 0, "ports": ["8080/http"], "env": env,
        "dockerEntrypoint": ["bash", "-c"], "dockerStartCmd": [POD_SCRIPT]})["id"]


def watch(pod, key, a):
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
        except (ApiError, urllib.error.URLError, OSError) as e:
            print(f"!! could not delete pod {pod} ({e}): delete it at runpod.io/console/pods", file=sys.stderr)


if __name__ == "__main__":
    main()
