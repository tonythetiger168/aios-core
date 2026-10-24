# aios-core

> Secure-by-default, local-first runtime for AI agents: scheduler, permissions, persistent memory, sandboxed tools and MCP. Python.

**繁體中文：[README.zh-TW.md](README.zh-TW.md)**

**Status: early alpha.** This is a working prototype, not a hardened product. Read [Security status](#security-status) before giving it access to anything you care about.

## Why

Agents need an operating-system-like layer: who may call which tool, what is remembered, what runs in isolation, what gets logged. Big vendors are building this into their own OS or enterprise platform. The popular open-source personal agents proved the demand but shipped with weak security (see [Landscape](#landscape)). `aios-core` aims at the gap: a **local-first, cross-platform, model-agnostic runtime for individuals and small teams, secure by default**.

"Model-agnostic" is the goal; today only Claude is wired in (see roadmap P0).

## What works today

- **Kernel**: async task queue with a worker pool; each agent is a "process" with its own permissions, history and memory.
- **Tool registry** ("syscalls") with JSON-schema tools; an agent only sees tools it is permitted to use (exact name or `prefix*` wildcard).
- **Persistent memory** (SQLite): conversation history survives restarts; long-term facts via `remember` / `recall` / `list_facts` and are injected into the system prompt.
- **Workspace file tools** (`read_file`, `write_file`, `list_files`), path-escape protected, UTF-8.
- **Sandboxed code execution** (`run_python`), three backends chosen with `--sandbox` (or `AIOS_SANDBOX`):
  - `process` (default on Linux/macOS): separate process, CPU/memory/file-size limits, no network (Linux with unprivileged user namespaces).
  - `docker`: a throwaway container per call with no network, **nothing from your machine mounted**, a read-only root, an unprivileged user, all capabilities dropped and memory/CPU/process caps. Designed for Linux containers, including Docker Desktop on macOS/Windows; actual enforcement depends on the daemon and platform, and AIOS refuses a daemon that reports it cannot enforce the limits (for example rootless Docker without cgroup delegation, which would silently ignore them; see [rootless Docker](docs/docker-sandbox-validation.md#rootless-docker)). Real-Docker CI covers Ubuntu, rootful and rootless. The image is never pulled for you: run `docker pull python:3.12-slim` once. Pin it with `AIOS_DOCKER_IMAGE=python@sha256:<digest>` so a changed tag cannot swap what runs (the startup message prints the line to use; see [pinning](docs/docker-sandbox-validation.md#image-pinning-and-trust)). On Windows `auto` picks it when it is ready; otherwise `run_python` is off there.
  - `microvm` (experimental, Linux with KVM): a fresh [Firecracker](https://firecracker-microvm.github.io/) microVM per call with its own kernel, no network device, nothing from your machine except the code, a read-only root and an unprivileged user. Only chosen explicitly, never by `auto`, and AIOS refuses to start if KVM, Firecracker or the images are missing. When AIOS runs as root, `AIOS_MICROVM_JAILER=1` also confines the VMM itself with Firecracker's jailer (chroot, unprivileged uid, memory-capped cgroup). Setup and threat model: [docs/microvm-sandbox.md](docs/microvm-sandbox.md).
- **MCP client**: attach stdio MCP servers; their tools are registered as `<server>__<tool>` and are *not* granted to agents unless you allow them.
- **Model providers**: Anthropic (default) or any OpenAI-compatible server (Ollama, LM Studio, vLLM, llama.cpp) via `--provider openai --model <name>`. The openai provider uses the standard library only; the `anthropic` package is imported only when you use Anthropic.
- **Budgets**: per-run limits on model calls (`--max-steps`), tokens (`--max-tokens`) and cost (`--max-cost` with `--price-in/--price-out`). A run that hits a limit stops *before* running more tools and says why. Each run's usage is written to the audit log.
- **Human approval**: `write_file`, `run_python` and every MCP tool ask you `[y/N]` before running. With no approver configured (e.g. `demo`, library use) they are refused. `--auto-approve` turns the prompt off; unsafe.
- **Untrusted-content tagging + taint policy**: output of `read_file`, `list_files`, `run_python` and every MCP tool is wrapped in `<untrusted>` tags (fake closing tags inside are defused) and the system prompt tells the model never to obey it. While such content is in the context, the run is *tainted*: `--auto-approve` stops applying, every risky call needs a person at the prompt (shown with a warning), and `remember` needs approval too, so injected text cannot silently poison memory.
- **Capability scopes**: beyond tool names, limit a tool to workspace paths (`paths`), skip the prompt for safe paths in clean runs (`auto_paths`) and cap calls per run (`max_calls`). Argument rules (`args`) allow or deny values of any argument with glob patterns and a length cap, so MCP tools are no longer all-or-nothing (for example a fetch tool limited to `https://docs.python.org/*`), and `auto_args` skips the prompt for pre-approved values the same way `auto_paths` does for paths. Set them in code (`Agent(..., scopes=...)`) or in a TOML file: `--policy policy.example.toml` (Python 3.11+). Typos in a policy fail loudly instead of silently allowing.
- **Memory provenance**: every fact records who wrote it (`user`, `model`, or `tainted` = saved while the run had read untrusted content) and when. Tainted facts are shown to the model as *unverified*; facts you set yourself with `/remember` cannot be overwritten by the model; newlines in a fact cannot forge extra lines in the system prompt. Older `aios.db` files are upgraded automatically.
- **Scheduled jobs**: `aios jobs ...` and `aios daemon` run tasks unattended with caps and the same policy, with a heartbeat and a rule-based watchdog (`aios status`, `aios alerts`; see below).
- **Agents working together**: every agent in the policy file can be handed a sub-task through a tool named `agent__<name>`, granted like any tool (`permissions = ["agent__notes"]`). The helper works with its **own** permissions, scopes and memory, starts each sub-task fresh (not saved), and runs under the caller's budget. Delegation cannot be used to launder an injection: if the caller has read untrusted content, the helper starts tainted (no auto-approval, no `auto_paths`), and the helper's answer comes back tagged untrusted. Loops are refused and depth is capped at 2. Works in `chat` and in `daemon` jobs.
- **Agent swarms**: hand hundreds of sub-tasks to helper agents at once with the `swarm` tool or `aios swarm`, under a worker pool, a process-wide limit on model calls in flight, a shared token/cost budget and per-task timeouts, optionally combined by a reducer agent. Same permissions, approvals, taint and audit as one delegation (see below).
- **Agents on other machines (A2A)**: serve an agent to other A2A agents with `aios a2a-serve`, or call remote ones as `a2a__<name>` tools (see below). Strangers' requests always run tainted, with only the tools the policy grants (read-only by default) and a budget cap.
- **Skills with digest pinning and a scanner**, in the open Agent Skills `SKILL.md` format: `aios skills scan|trust|list|untrust|sign|publish|search|install`, optional publisher signatures, a static skill registry, `chat --skill <folder>` (see below).
- **MCP server mode**: `python aios.py serve` offers the governed tools to any MCP client (see below). `python aios.py audit` prints the audit log from the shell.
- **Local web console** (`python aios.py console`): a read-only browser view of the audit log (filter by agent, tool, status), refusals, jobs and their runs, each agent's notes with who wrote them, and trusted skills. It listens on 127.0.0.1 only, needs the one-time link it prints, opens the database read-only and runs no JavaScript, so text an attacker planted in the log cannot act in your browser. With `--approve` it also answers approval requests from the daemon and `serve` (see Scheduled jobs).
- **Memory search**: the `search_memory` tool and `/search` find notes by keywords (BM25, pure Python, works for Chinese too). With `--embed-model nomic-embed-text` (any OpenAI-compatible `/embeddings` server, e.g. Ollama) it also matches by meaning; the two rankings are fused. With more than 30 notes the prompt shows only the ones relevant to the task, and the model uses `search_memory` for the rest.
- **Prompt-injection test suite**: `python tests/test_injection.py --report` prints which attacks get through under each defense (below).
- **Audit log** (SQLite, append-only API): every tool call is recorded with agent, tool, arguments, result and status (`ok` / `error` / `denied`). View it with `/audit`.
- **Tests**: `python -m unittest discover -s tests -v` (standard library only, no API key needed); CI runs on Linux and Windows.
- **CLI**: interactive chat with `/facts`, `/search <words>`, `/remember <key> <value>`, `/forget <key>`, `/audit`, `/reset`, `/quit`.

## Quick start

Requires Python 3.10+ and an [Anthropic API key](https://platform.claude.com/settings/keys).

```bash
pip install anthropic            # optional: pip install mcp
export ANTHROPIC_API_KEY="sk-ant-..."      # PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."

python aios.py chat                         # interactive agent named "main"
python aios.py chat --agent work            # separate history + memory per name
python aios.py chat --mcp "demo=python demo_mcp_server.py"   # attach an MCP server
python aios.py chat --provider openai --model llama3.1   # local model via Ollama (http://localhost:11434/v1)
python aios.py chat --max-tokens 20000 --max-steps 6     # hard limits per run
python aios.py chat --policy policy.example.toml --agent notes   # per-agent permissions and path scopes
python aios.py chat --policy policy.example.toml --agent lead    # an agent that hands writing to 'notes'
python aios.py chat --sandbox docker          # run_python in a throwaway container (after `docker pull python:3.12-slim`)
python aios.py chat --skill examples/skills/meeting-notes        # load a skill you pinned (see Skills)
python aios.py chat --embed-model nomic-embed-text               # also search notes by meaning (needs `ollama pull nomic-embed-text`)
python aios.py chat --auto-approve          # skip the [y/N] prompts for clean runs (unsafe)
python aios.py demo                         # scripted demo (risky tools are refused)
```

State is stored in `./aios.db` and `./workspace/` (both git-ignored).

## Scheduled jobs

Run a task on a schedule, without you present:

```bash
python aios.py jobs add digest "Summarise inbox.txt into reports/digest.txt" --schedule "daily 09:00" --agent reporter
python aios.py jobs list                  # also: enable | disable | run <name> | remove <name> | history [name]
python aios.py daemon --policy policy.example.toml --provider openai --model llama3.1   # leave running
python aios.py daemon --once ...          # run what is due, then exit: for cron or Windows Task Scheduler
```

Schedules: `every 30m`, `every 2h`, `every 1d`, `daily 09:00`, `weekly mon 09:00`, `once 2026-12-24 18:30`, `on change inbox/*`, `on webhook` (both below; local time, at most once a minute). With cron: `* * * * * cd ~/aios && python aios.py daemon --once --provider openai --model llama3.1`.

**Keep it running.** `aios service` prints a service definition that starts the daemon at login and restarts it when it crashes, plus the commands to install it. aios installs nothing itself, so you can read exactly what will run:

```bash
python aios.py service systemd --home ~/aios-data -- --policy policy.toml --provider openai --model llama3.1   # Linux
python aios.py service launchd --home ~/aios-data -- ...                                                   # macOS
python aios.py service windows --home C:\aios-data -- ...                                                  # Task Scheduler (PowerShell)
```

Flags after `--` go to `aios daemon` unchanged (`--once` is refused: a service keeps running). API keys never go into the service file: systemd and launchd read `KEY=value` lines from `<home>/aios.env` (make it readable by you only); on Windows, set them as user environment variables.

- **Nobody can approve anything** (unless you turn on browser approvals, below). Same policy pipeline as `chat`, but risky tools are refused unless the policy pre-allows the path with `auto_paths`. A job with no policy section is read-only (`now`, `read_file`, `list_files`, `recall`, `list_facts`, `search_memory`).
- A job that reads a file is *tainted*, so even `auto_paths` stop applying, unless the scope says `auto_when_tainted = true`: your explicit statement that injected text in what the job read may write into that one folder, and nothing else.
- Each job is its own agent (`job-<name>`: own history and memory) and starts every run with a clean history. Runs always have a cap (default 50,000 tokens, 8 steps, 300 s), and a job runs at most 200 times a day (`--max-runs-per-day`), so a typo like `every 1m` cannot burn your budget unnoticed.
- A job missed while the daemon was off runs **once** when it comes back, not once per missed slot. Jobs run one at a time. Results: `aios jobs history`; every tool call is in `aios audit`.

**Approve from the browser.** Instead of refusing, the daemon (or `serve`) can ask you:

```bash
python aios.py daemon --approvals console --approve-timeout 240 ...   # risky calls wait for a person
python aios.py console --approve                                       # answer them on the Approvals page
```

- Each risky call that no scope pre-allows becomes a request showing the agent, the tool and the full arguments, with a warning when the run has read untrusted content. You press Approve or Deny. No answer before the timeout (default 240 s, kept below the job's `--timeout`) means no, and a request closes when its run ends, so it cannot be approved later.
- Only a console started with `--approve` can answer; without it the page is read-only. The only write it can make is that one answer. A submission needs the console's cookie, a form token made at each start, and a same-origin `Origin` header, so another web page cannot press the button for you.
- One request at a time: a job waits while it is open.

**File-change triggers.** `--schedule "on change inbox/*"` runs the job when files matching the pattern (relative to the workspace, subfolders included) are added, edited or removed:

```bash
python aios.py jobs add mail "Summarise the new files in inbox/ into reports/mail.txt" --schedule "on change inbox/*" --agent reporter
```

- Files already there when you add the job are the baseline. The daemon checks every `--tick` (or every `--once` run) and acts on a change only once it has held still for one check, so a file still being copied is not read half-way.
- File names are never put into the task (anyone who can drop a file could choose one); the task only says that the pattern matched. The job reads the files itself, which makes the run *tainted* like any other.
- What the run writes into the watched folder does not trigger it again; changes made by others *during* a run are folded into that run. At most 5,000 matching files are fingerprinted, and the daily run cap applies.

**Webhook triggers.** `--schedule "on webhook"` runs the job when another program on this machine (a git hook, a script, a home-automation bridge) sends it a request:

```bash
python aios.py jobs add triage "File the reported bug into notes/bugs.md" --schedule "on webhook" --agent reporter   # prints the job's secret once
python aios.py daemon --webhook-port 8766 ...
curl -X POST http://127.0.0.1:8766/hook/triage -H "Authorization: Bearer <secret>" --data-binary @event.json
```

- The listener binds 127.0.0.1 only and accepts `POST /hook/<job>` with the job's own secret (only its hash is stored; `aios jobs secret <job>` makes a new one). A wrong secret and an unknown job get the same 401, so names cannot be probed. Bodies are capped at 64 KB, and at most 20 requests wait per job (then 429).
- The listener runs nothing itself: each request becomes one run on the next tick, under the job's policy, caps and daily limit. The body reaches the model **tagged as untrusted data** and the run is *tainted* from the start, so `auto_paths` stop applying (unless `auto_when_tainted`). An empty body is just a ping and does not taint. The body is deleted after the run.
- To receive webhooks from the internet, put your own HTTPS reverse proxy or tunnel in front and keep the secret private; aios does not do TLS.

**Watchdog and status.** Nobody watches an unattended run live, so the daemon watches its own log. On every tick it writes a heartbeat and checks the audit log and job runs against fixed rules; no model is involved, because a model reading the log would read attacker-written text.

```bash
python aios.py status --home ~/aios-data     # daemon alive or down, open alerts, approvals waiting; exit 1 = look
python aios.py alerts --home ~/aios-data     # open alerts; --ack <id>|all, --all, --check (run the rules now)
```

| Rule | Severity | Raised when (last 15 minutes) |
|---|---|---|
| `tainted-refused` | critical | a risky call was refused because the run had read untrusted content: what a prompt injection trying to act looks like |
| `unattended-refused` | warning | a job tried a risky call its policy does not pre-allow |
| `refusal-spike` | warning | one agent was refused 10 or more times |
| `budget-stop` | info | a run stopped on its token or cost budget |
| `job-failing` | warning | a job's last 3 runs ended in error or timeout |

- Alerts go to the daemon's stderr, `aios status`, `aios alerts` and the console's overview. The same rule for the same agent is not repeated within an hour. Alerts only report; they never stop, block or change anything.
- "Down" means no heartbeat for longer than 3 ticks plus one job timeout (at least 2 minutes), so a long job does not look like a dead daemon. Put `aios status` in cron or a monitoring check to be told when the daemon dies.

## Looking after memory

Notes saved while a run had read untrusted content are marked *unverified*. Check them now and then:

```bash
python aios.py memory review                # unverified notes, oldest first
python aios.py memory confirm main owner    # you checked it: it becomes your note
python aios.py memory tidy --dry-run        # what tidy would do
python aios.py memory tidy                  # archive unverified notes older than 30 days (--days)
python aios.py memory archived              # nothing is lost: memory restore <agent> <key>
```

- Tidy moves old unverified notes to an archive instead of deleting them, and never touches your notes or the model's. It also drops cached vectors whose note changed or is gone (they are rebuilt on demand), and lists notes with the same text under several keys without changing them.
- Restore puts the note back as unverified, and refuses if a newer note with that key exists. No model is involved: deciding what an agent keeps believing is what an injected note would like to influence.

## Skills

A skill is a folder in the open [Agent Skills](https://agentskills.io) format: `SKILL.md` with `name` and `description` front matter, followed by the instructions for the agent, so skills written for Claude, Codex, Gemini CLI or Copilot (for example [anthropics/skills](https://github.com/anthropics/skills)) load as they are. `skill.toml` (name, version, description) is optional; when present it wins, and a different `name` in the front matter is refused. Only the text after the front matter goes into the prompt. aios **never runs code inside a skill**; it only reads `SKILL.md`. Because those instructions go into the system prompt, a skill is a prompt-injection channel, so you review it and **pin its digest** first:

```bash
python aios.py skills scan examples/skills/meeting-notes     # tripwire scan + digest
python aios.py skills trust examples/skills/meeting-notes    # you type the name to confirm
python aios.py chat --skill examples/skills/meeting-notes    # loads only if the digest still matches
python aios.py skills list                                   # what you trust
python aios.py skills untrust meeting-notes
```

- The digest covers **every file in the folder** (path and content; CRLF is normalised). Edit, add, remove or rename anything and the skill is `changed` and refused until you re-trust it. A different folder with the same name does not inherit the trust.
- The scan flags: prompt-injection phrases (English and Chinese), "don't tell the user", running things without asking, disabling approval/audit/sandbox, sending secrets, `curl | sh`, hidden Unicode, secret-file paths, plus network/process/obfuscation hints in code files. High findings block `trust` unless you pass `--force`.
- A skill cannot grant permissions or change approval rules. It is only text; the tools and scopes come from your policy.
- **Publisher signatures** (optional) tell you *who* published exactly this content. They use the same OpenSSH signing that git uses (`ssh-keygen -Y`, OpenSSH 8.1+), with no new key format or library:

  ```bash
  # publisher: writes SKILL.sig (signs name, version and the digest of every other file)
  python aios.py skills sign my-skill --key ~/.ssh/id_ed25519
  # you: list the publishers you accept, one per line, in <home>/allowed_signers
  echo 'alice@example.com namespaces="aios-skill" ssh-ed25519 AAAA...' >> allowed_signers
  python aios.py skills trust my-skill --require-signed   # or AIOS_SKILLS_REQUIRE_SIGNED=1
  ```

  `scan` and `trust` report the signature as valid (and who signed), unsigned, unknown signer, not checked (no `ssh-keygen` or no signers file), or **invalid**. An invalid signature always blocks `trust`. With `--require-signed`, anything other than a valid signature from an allowed signer is refused. `skills list` shows the signer recorded at trust time. A signature is about origin, not safety: you still review and confirm.
- **Registry** (optional): a registry is just a folder with `index.json` and one `.tar.gz` per published version, so any static host (GitHub Pages, a bucket, a file share) can serve it. No server to run:

  ```bash
  # publisher: add a (signed) skill to a registry folder, then upload the folder
  python aios.py skills publish my-skill --registry ./registry
  # you: find and install; same scan, signature check and typed confirmation as `trust`
  python aios.py skills search notes --registry https://example.com/registry   # or AIOS_SKILL_REGISTRY
  python aios.py skills install meeting-notes --require-signed                  # or meeting-notes@1.0.0
  python aios.py chat --skill <home>/skills/meeting-notes
  ```

  `install` downloads to a temporary folder, checks the archive's SHA-256 and the skill's name, version and digest against the index, then reviews it exactly like `trust`; only if you confirm does it replace `<home>/skills/<name>` and the pin. A published name and version cannot be republished with other content. Archives may hold only plain files with plain relative paths, within the skill size limits. Remote registries must use `https://` (plain `http://` only for localhost). The index is not signed: it helps you find skills and catches corrupted or swapped downloads, but who published a skill is answered only by its `SKILL.sig` and your allowed signers, so use `--require-signed` for registries you do not control.
- **Limits**: pinning proves the content is what *you reviewed*; a signature proves who published it, but only for keys you chose to allow. Key distribution is up to you (there is no key server); a registry entry is not an endorsement. A clean scan is a tripwire, not proof. A skill you trust can still mislead the model: read it.

## Use it as an MCP server

Other tools (Claude Desktop and any MCP client) can use aios's governed tools instead of ungoverned ones:

```bash
pip install mcp
python aios.py serve --home ~/aios-data --agent gateway --policy policy.example.toml
python aios.py audit --home ~/aios-data      # see what the client did
python aios.py console --home ~/aios-data    # or browse it: prints a local link with an access token
```

MCP client config (shape used by Claude Desktop's `claude_desktop_config.json`; use absolute paths):

```json
{
  "mcpServers": {
    "aios": {
      "command": "python",
      "args": ["/path/to/aios.py", "serve", "--home", "/path/to/aios-data"]
    }
  }
}
```

- The client is treated like the model: **not trusted**. Same permissions, scopes, audit log and `<untrusted>` tagging as `chat`.
- Without a policy only read-only tools are offered (`now`, `read_file`, `list_files`, `recall`, `list_facts`).
- Nobody can answer a `[y/N]` prompt over stdio, so risky tools (`write_file`, `run_python`, `remember` after tainting) are **refused** unless the policy pre-allows a path with `auto_paths`. Once the session has read a file, even those are refused until the server restarts.
- `max_calls` counts per server session. All data lives under `--home` (`aios.db`, `workspace/`).
- **Status**: works with the official `mcp` SDK 1.x and 2.x. CI drives `aios serve` with the SDK's own stdio client (initialize, list, call, refusals, audit) on both versions, and connects aios to a real SDK server. Not yet tried with Claude Desktop or other GUI clients: if yours does not connect or lists no tools, please open an issue with the stderr output.

## Talk to other agents (A2A)

[A2A](https://a2a-protocol.org) lets agents from different vendors call each other. aios speaks both versions in use, 1.0 and 0.3 (JSON-RPC), and was checked against the official `a2a-sdk` in both directions.

```bash
# offer one of your agents to others (prints a token; or set AIOS_A2A_TOKEN)
python aios.py a2a-serve --agent researcher --policy policy.example.toml --port 8770 --description "Finds facts in my notes"

# call a remote agent as the tool a2a__research (its token, if it needs one, from $AIOS_A2A_TOKEN_RESEARCH)
AIOS_A2A_TOKEN_RESEARCH=<token> python aios.py chat --a2a research=http://127.0.0.1:8770
```

- **Serving**: listens on 127.0.0.1; every call needs the token (the agent card at `/.well-known/agent-card.json` is public and only names the agent). The caller is **not trusted**: each conversation is a fresh instance of that policy agent with its own permissions and a capped budget per request (50,000 tokens by default). It starts *tainted* so `auto_paths`/`auto_args` never apply, and every request is audited. A message that carries a `contextId` continues that conversation: its history stays in memory only (at most 20 messages per conversation and 20 conversations, forgotten after 30 idle minutes) and is never saved. A message without one starts over. Nobody can approve risky calls, so a served agent should be read-only or tightly scoped.
- **Calling**: `--a2a NAME=URL` (in `chat` and `daemon`) makes the tool `a2a__NAME`. Grant it per agent like any tool. It sends your text to another machine, so every call needs approval (in `daemon`: refused unless a scope pre-allows it with `auto_args`, or you answer it with `--approvals console`), and the answer comes back tagged untrusted. A token is never sent over plain `http` to anything but this machine, and calls always go to the URL you gave, not to a URL the remote card names. Each answer ends with `[conversation: <id>]`; the model passes that id back as `conversation` to continue, including when the remote agent asks for more input.
- **Streaming**: `SendStreamingMessage` / `message/stream` answer as server-sent events (submitted, working, the answer, final status), so streaming-only clients work too. Updates are not incremental yet: the answer arrives in one piece. A caller whose stream broke can subscribe to the unfinished task again (`SubscribeToTask` / `tasks/resubscribe`, checked with the official a2a-sdk client) and gets the task as it is, then the answer and final status; a finished task is read with `GetTask`.
- **Tasks that run on**: a caller that does not want to wait sends `returnImmediately` (1.0) or `blocking: false` (0.3) and gets a working task back, then polls it with `GetTask`/`tasks/get`, lists tasks with `ListTasks` (1.0; filter by `contextId` or `status`) and stops one with `CancelTask`/`tasks/cancel`. Requests still run one at a time; at most 8 tasks may be unfinished at once, and more are rejected. Cancelling stops the agent at its next step and forgets that conversation's history, because it may end halfway through a tool call. When `a2a__NAME` gives up waiting on a remote task, it asks the remote agent to cancel it.
- Not supported yet: push notifications and files.

## Agent swarms

A swarm hands many sub-tasks to helper agents **at the same time** and collects the answers: one lead fans work out to many workers, optionally one more agent combines what they found. It is built on delegation, so every worker is exactly the call the lead could make itself, one at a time: its permission, scope, approval, taint and audit all apply.

```toml
[agents.lead]
permissions = ["read_file", "swarm", "agent__worker", "agent__editor"]
[agents.worker]
permissions = ["read_file"]
[agents.editor]
permissions = []
```

- **From an agent**: grant the tool `swarm`. The model passes `tasks` (a list of `{helper, task}`), or one `helper` + `task` + `items` (the task runs once per item, `{item}` replaced), and optionally `reduce = {helper, task}`. A swarm reaches only helpers the agent may already call (`agent__<name>`, `a2a__<name>`); asking for any other refuses the whole swarm before anything runs. Up to 1,000 tasks per call.
- **From the command line** (batch, no lead model): `aios swarm` streams a task file of any length through the workers and writes one JSON line per result as each finishes.

```bash
python aios.py swarm --policy policy.example.toml --agent worker --tasks tasks.txt --out results.jsonl \
    --parallel 64 --model-slots 32 --swarm-max-tokens 2000000 --reduce editor
# tasks.txt: one task per line, or {"agent": "...", "task": "..."} per line; - reads stdin
```

**High concurrency, with hard limits.**
- *Worker pool and backpressure*: a fixed pool of `--parallel` workers (default 8) pulls from the task list, so 100,000 tasks never mean 100,000 runs in memory, and a task file is read only when a worker is free.
- *Rate limit*: every model call of every agent in the process waits for one of `--model-slots` slots (default 16). Nested swarms and parallel jobs share them, so the provider never sees more requests at once than that, whatever the fan-out.
- *Shared budget*: all workers of one swarm spend from one ledger, `--swarm-max-tokens` (default 1,000,000) and `--swarm-max-cost` (with prices). When it runs out, no new task starts (the rest are reported `skipped`) and running workers stop before their next model call. A swarm inside a swarm also charges its parent's ledger. Each worker still has its own per-run budget.
- *Isolation of failures*: each task has a timeout (`--timeout`, default 600 s); an error or timeout in one never stops the others, and cancelling the swarm cancels every worker.
- *Combining large results*: `reduce` packs answers into bounded groups and has the reducer combine them in rounds until one answer is left, so a thousand answers never land in one prompt. Without `reduce`, the lead gets each answer clipped (1,500 chars, 20,000 in total).
- *Database*: the SQLite store now uses WAL, so hundreds of workers can write audit entries without stalling, and the console reads while they do. Tested with 3,000 tasks, 400 workers and 150 model slots.

**Security.** Workers inherit the lead's taint, as in delegation: if the lead has read untrusted content, no worker gets `auto_paths`/`auto_args`. The reducer always runs tainted, because the answers it reads may carry text the workers read. The swarm's answer is tagged untrusted. `max_calls` on `agent__<name>` counts every swarm task. Approvals are asked one at a time, so a swarm that needs many is slow by design; for unattended swarms pre-allow what workers may do with scopes. Every task is audited under the caller's name with a shared swarm id. `aios swarm` tasks are the operator's own, so they run untainted.

## Architecture

```
 you ──> CLI ──> Kernel ──┬─> Claude API (tool-use loop, per-agent permissions)
                          ├─> ToolRegistry ──> built-in tools / MCP servers
                          ├─> Store (SQLite): history, long-term facts
                          └─> Sandbox: subprocess + rlimits + no network, or a Docker container
```

## Built-in tools

| Tool | What it does |
|---|---|
| `now` | Current local date and time |
| `read_file` / `write_file` / `list_files` | Workspace-only file access |
| `remember` / `recall` / `list_facts` | Per-agent long-term memory |
| `run_python` | Sandboxed Python (`--sandbox process`, `docker` or `microvm`) |

## Security status

Honest list of what this does **not** do yet:

- **Prompt-injection defense is partial.** Tagging only asks the model to behave; the real protection is the taint policy plus scopes, which stop a *hijacked* model from acting without a person. It does **not** stop a person who types `y` without reading the prompt, content that merely misleads the answer (no tool involved), or a malicious tool that lies. Our suite models the worst case (a model that obeys every injected instruction); real models vary and we have not measured them yet.
- Argument rules are glob patterns on the text of top-level arguments, not a parser: `*` also matches `/`, so write `https://host/*` rather than `https://host*` (which matches `https://host.evil.com`). They check what the tool is asked to do, not what it really does (a fetch can still follow a redirect). `allow` is case-sensitive, `deny` ignores case. There is still no network-level egress control.
- Scheduled jobs are the riskiest way to run an agent: unattended, so nobody notices odd behavior live. Keep job permissions read-only where you can, scope writes to one output folder, and read `aios audit` now and then. `auto_when_tainted` trades safety for usefulness on purpose. Times are local wall-clock time (a daily job can shift by an hour once at a daylight-saving change). The daemon is one process: if it dies, jobs stop until you restart it; `aios status` tells you it is down, but nothing restarts it for you yet.
- Memory search covers the saved notes only (not chat history). Vector search sends each note's text to the embedding server you configure: use a local one for private notes. CI checks it against a real Ollama server with `nomic-embed-text`: queries that share no word with a note find it, and an unrelated query finds nothing. A note counts as a meaning match only when it scores clearly above how the query scores against a few unrelated sentences, because real models rate any two texts as somewhat similar. Other models and OpenAI endpoints are untested. Keyword search needs no setup.
- Skills are trusted by **content you reviewed**, not by author; an optional signature only records who published it. The scanner catches obvious tricks and nothing subtle, and a skill you trust can still steer the model. Treat `--force` as "I read every line".
- `serve` cannot ask over stdio (it is the protocol channel). Use `--approvals console`, pre-allow paths with `auto_paths` (or argument values with `auto_args`) or leave risky tools off. The session taint lasts until the server restarts.
- The audit log records actions but is not tamper-proof: anyone with write access to `aios.db` can edit it.
- The console link is a password while the console runs: anyone who has it (or the browser cookie it sets) can read your log and notes, and with `--approve` answer approval requests. A new one is made each start. It does not protect against other software running as your own user, which can read `aios.db` directly anyway.
- **The `process` sandbox is not a full security boundary.** It requests resource limits and, where available, a Linux network namespace, but code can still read files its user can read, and process count is unlimited. For untrusted code use `--sandbox docker`. A container shares the Docker host kernel: a kernel or Docker escape bug can break isolation. Docker Desktop adds its own VM, but this backend does not implement microVM isolation; for that use `--sandbox microvm` on Linux with KVM. Each call creates then attaches to a container, with no extra Python packages installed by AIOS. Treat the image, Docker CLI, daemon and selected Docker context as trusted.
- Docker output is drained in bounded chunks; at most 10,000 UTF-8 bytes are retained by default. Timeout/cancellation triggers bounded container kill, force removal and an absence check. Cleanup failure is reported, not treated as a confirmed kill. A create timeout has an unknown outcome: a delayed daemon operation may still create a container; check the named container on that daemon. See [validation scope](docs/docker-sandbox-validation.md).
- **Windows**: without Docker, `run_python` is disabled (timeout-only isolation is not safe). Use `--sandbox docker`, or set `AIOS_ALLOW_WEAK_SANDBOX=1` to enable the weak one at your own risk.
- Treat MCP servers as untrusted code and untrusted data sources; grant them per agent explicitly.
- Budgets are checked after each model call, so one call can overshoot a limit. Cost limits need prices you supply (they change, so there are no built-in defaults). Budgets apply per run, not per day.
- Local models are much weaker at tool use and at resisting prompt injection than frontier models. Keep approval on.

### Injection test results

Worst-case model, `--auto-approve` on. `X` = the attack worked.

| Attack | no defenses | taint only | scopes only | taint + scopes |
|---|:-:|:-:|:-:|:-:|
| write file anywhere | X | | | |
| write into `config/` | X | | | |
| write inside `notes/` (in scope) | X | | X | |
| path traversal | | | | |
| memory poisoning | X | | X | |
| overwrite a fact you set | | | | |
| run code | X | | X | |
| call an outside tool | X | | X | |

Reading it: scopes alone cannot stop a malicious call that stays inside the allowed path; the taint policy is what stops it. Path traversal is stopped by the workspace jail in every column.

## Roadmap

Months are relative estimates (month 1 = project start) and will move.

| Phase | When | Goal | Exit criterion |
|---|---|---|---|
| **P0 Foundations** | month 1 | Automated tests · audit log · human approval for risky tools · cost/step budgets · model-provider abstraction (incl. local models) | Every action is traceable; budget overruns stop the run |
| **P1 Secure core** | months 1–2 | ✅ capability scopes (paths, call caps, argument rules) · ✅ untrusted-content tagging + taint policy · ✅ prompt-injection test suite · ✅ memory provenance and timestamps · ✅ skill pinning, scanning and OpenSSH publisher signatures · ✅ memory search (BM25 keywords, optional vectors) · ✅ run as an MCP server (`aios serve`; checked with the official SDK client, 1.x and 2.x) | Passes our own injection test suite (done for the worst-case model); P1 is feature-complete, real-world testing is still open |
| **P2 Expansion** | months 3–4 | ✅ multi-agent delegation (`agent__<name>`) · ✅ agent swarms (`swarm`, `aios swarm`: high concurrency under shared limits) · ✅ A2A (serve and call agents, protocol 1.0 and 0.3, multi-turn, streaming and resubscribe, background tasks and cancel) · ✅ schedules (`aios jobs`/`daemon`) · ✅ file-change and webhook triggers · ✅ local web console (with browser approvals) · pluggable sandbox backends (✅ Docker · 🧪 Firecracker microVM, verified only in its own CI job) | Multi-agent in one command; real isolation on Windows |
| **P3 Ecosystem** | months 6–9 | ✅ signed skill registry (publisher signatures, static `index.json` registry, `publish`/`search`/`install`; no public registry hosted yet) · optional hosted version | External contributors ship skills |
| **P4 OS layer** | month 12+ | WSL image / Linux distro · system service · mobile companion | Decide based on earlier results |

Explicit non-goal for now: writing an actual OS kernel.

Detailed tracking (gap analysis, per-item status, Gantt chart): [`docs/landscape-and-roadmap.xlsx`](docs/landscape-and-roadmap.xlsx).

## Product line (planned)

| Product | What | Phase | Model |
|---|---|---|---|
| **AIOS Core** | This repo: scheduler, permissions, memory, MCP | P0–P1 | Open source |
| **AIOS Shell** | CLI (exists) + local web console with approvals and audit view | P2 | Open source |
| **AIOS Guard** | Policy engine, skill scanner, prompt-injection test suite | P1 | TBD (likely the paid differentiator) |
| **AIOS Memory** | Standalone memory service with an MCP interface | P1 | TBD |
| **AIOS Sandbox** | Multi-backend isolated execution | P2 | TBD |
| **AIOS Hub** | Signed skills / agents registry | P3 | TBD |
| **AIOS Team / Cloud** | Hosted, RBAC, audit export | P3 | Paid |

Business-model rows are hypotheses, not commitments.

## Landscape

A snapshot as of October 2026. Much of it is vendor-published or press coverage; Apple items are reports, not announcements.

- **Security is the open wound.** Gartner analysts called OpenClaw's risk "unacceptable" ([Security Boulevard](https://securityboulevard.com/2026/03/latest-openclaw-flaw-can-let-malicious-websites-hijack-local-ai-agents/)); researchers report that poisoning any one of an agent's capability, identity or knowledge raises attack success from 24.6% to 64–74% ([ICML 2026](https://icml.cc/virtual/2026/69297)).
- **OS vendors are building it in**: Windows has an on-device MCP registry, agent workspace and agent identities ([TechSpective](https://techspective.net/2025/11/18/windows-becoming-operating-system-for-ai-agents/)); Android has AppFunctions and Gemini Intelligence ([MacRumors](https://macrumors.com/2026/05/12/google-previews-android-17/)).
- **Standards settled**: MCP and A2A both sit under the Linux Foundation's Agentic AI Foundation ([source](https://algeriatech.news/?p=35142)).
- Full list with sources: [`docs/landscape-and-roadmap.xlsx`](docs/landscape-and-roadmap.xlsx).

## Contributing

Issues and PRs welcome, especially: tests, sandbox backends, prompt-injection test cases, and provider adapters.

## License

MIT, see [LICENSE](LICENSE).
