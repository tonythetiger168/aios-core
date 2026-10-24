# Docker sandbox: evidence and deployment limits

Review date: 2026-10-21 (Asia/Taipei). This is a Docker Linux-container backend,
not a microVM implementation or an escape-resistance certification.

## Baseline inspected

- PR #3 original head: `a007c955b88ea78080ba5d1ace52d8c3d6adc0d6`.
- Main/base at review: `c51c1eb3e2c60bd6d5280724b0d66ec63e6b3022`.
- [Original CI run](https://github.com/tonythetiger168/aios-core/actions/runs/39395189275):
  Ubuntu Python 3.11 and 3.12 each ran 240 tests with no skips. Four real-Docker
  tests passed in each Ubuntu job. Windows Python 3.11 and 3.12 each ran 240
  tests with nine skips, including all four real-Docker tests. No macOS job.
- PR author reports 239 passed / one skipped locally against Docker. This is
  author-reported evidence, not an independently reproduced deployment matrix.
- No submitted reviews or inline review threads existed at the time of review.

## What the original tests support

| Claim | Evidence | Limit |
|---|---|---|
| No implicit image pull | Command includes `--pull never`; selection tests reject unavailable Docker | Actual missing-image startup was not tested against Docker |
| No outbound network | `--network none`, one failed connection to 1.1.1.1:53 | Not an exhaustive test of network paths |
| No host directory mounts | Command has no volume/mount flags; empty `/tmp` | Empty scratch is not an escape test |
| UID 65534 | UID observed inside real container | Original test did not check GID |
| Read-only root | Flag plus failed `/pwned` write | That write can fail from ordinary UID permissions alone |
| Capabilities, no-new-privileges, 16 MiB tmpfs | Flags in implementation; only some asserted by original command test | Original suite did not inspect effective runtime state |
| Memory cap | A 512 MiB allocation under a requested 64 MiB limit exited nonzero | Original test did not identify OOM/cgroup cause |
| CPU, PID and swap limits | Requested in Docker arguments | Enforcement not behaviorally verified |
| Timeout cleanup | Infinite loop terminated and no sandbox-prefix container remained in Ubuntu CI | Original path ignored kill failure, cancellation and daemon hangs |

The process-backend network namespace warning in Ubuntu logs concerns the
separate process backend. Linux/macOS `auto` still selects that backend; installing
Docker does not automatically make their default execution containerized.

## Corrections in this revision

- Drain stdout/stderr in 8 KiB chunks and retain at most `max_out` UTF-8 bytes
  (10,000 by default), rather than buffering all output before truncating it.
- Create the named container before attaching/starting it. Timeout/cancellation
  invokes bounded `kill`, `rm --force`, then verifies absence for this exact name.
  A cleanup error cannot produce a successful `killed` result. Cancellation remains
  cancellation, with cleanup failures emitted to stderr.
- A create timeout/cancellation is explicitly an **unknown outcome**. Killing a
  Docker CLI cannot retract a daemon request already in flight. A delayed creation
  may leave a stopped container; check its reported name on that daemon. Code is
  not sent to a container until create has returned successfully.
- Timeout includes create and execution. Cleanup can add up to three control
  command deadlines, each with a bounded local CLI teardown deadline. The default
  cleanup command deadline is three seconds; the result is not an exact ten-second
  end-to-end latency guarantee.
- Tool descriptions distinguish process file access/network availability from
  Docker isolation. Both READMEs state the tested platform scope.
- New real-Docker assertions inspect UID/GID, root mount `ro`, capabilities,
  `NoNewPrivs`, tmpfs flags and capacity. Timeout checks target only this call.
  New assertions need their own CI evidence; the original 240-test run does not
  validate this revision.
- Linux CI records Docker client/server versions, cgroup/security configuration
  and the pulled image digest. A mutable image tag is still used; no image pinning
  or provenance guarantee is added here.

## Local validation for this revision

`python -m unittest discover -s tests -v`: 250 tests, 245 passed, five skipped
(all five real-Docker tests), Python 3.12.14, Linux workspace without Docker CLI.
Includes a bounded-memory 32 MiB output stream test and fake/mocked lifecycle
tests for failed/hung kill, unconfirmed cleanup, create timeout, command timeout,
backend descriptions, cancellation and repeated cancellation. These tests do not
validate Docker isolation. Real-Docker execution is delegated to Ubuntu CI.

## Deployment dependencies and follow-up

Trust the image, Docker executable, daemon, Docker context and administrators.
Isolation depends on the daemon's kernel, cgroup support, security configuration
and Docker version. Docker Desktop runs Linux containers in its own VM; AIOS does
not provision a per-call microVM. Neither these tests nor an empty mount list prove
resistance to kernel/Docker vulnerabilities or side channels.

Before claiming a deployment is verified, run the opt-in real-Docker tests there
and retain the commit, OS/architecture, Docker/daemon version, cgroup configuration
and image digest. Windows unit-test success is not Docker Desktop validation.

Follow-up acceptance criteria:

1. Real-Docker validation on supported Windows/macOS Docker Desktop versions,
   including cancellation, cleanup and unsupported configuration failures.
2. CPU, PID, swap and memory/OOM cause checks; daemon disconnect/restart and
   delayed-create reconciliation tests. Rootless/remote contexts need explicit scope.
3. ~~Image digest pinning/update policy and daemon/context threat model.~~ See
   "Image pinning and trust" below.
4. ~~A separate microVM backend~~: `--sandbox microvm`, see
   [microvm-sandbox.md](microvm-sandbox.md).

## Image pinning and trust

### Pinning

`--pull never` stops AIOS from fetching anything, but a tag such as
`python:3.12-slim` names whatever image was pulled last. To run exactly one image, pin it:

```sh
docker pull python:3.12-slim
docker image inspect --format '{{json .RepoDigests}}' python:3.12-slim
# ["python@sha256:<digest>"]
export AIOS_DOCKER_IMAGE=python@sha256:<digest>
export AIOS_DOCKER_REQUIRE_DIGEST=1   # optional: refuse to start with an unpinned tag
```

- With a pinned reference, the container is created from `NAME@sha256:<digest>`. The daemon
  stores images by content, so it runs that content or nothing. A missing or different image
  is reported as "not present", and nothing is pulled.
- Each time the docker sandbox starts, it prints the Docker executable it resolved, the daemon
  endpoint and whether the image is pinned. For an unpinned tag it also prints the exact
  `AIOS_DOCKER_IMAGE=` line to pin what is present now.
- A malformed `AIOS_DOCKER_IMAGE` (an `@` without `sha256:` and 64 hex digits) is a startup
  error.
- Multi-architecture images: `RepoDigests` records the digest of the multi-platform index you
  pulled, so the same pin works on amd64 and arm64 hosts. Each host runs the image for its own
  architecture from that index. If you need byte-identical images across hosts, pin the
  per-platform manifest digest instead, from `docker buildx imagetools inspect`.

### Updates and vulnerabilities

- Update on purpose: pull the tag, review what changed (for example `docker scout cves` or
  your scanner of choice), then move the pin. Record the old and new digests in your change log.
- When a vulnerability in the base image matters for code isolation (the Python
  interpreter, glibc, or anything reachable from `python -I` with no network and a read-only
  root), pull and re-pin promptly. Kernel and runtime (runc/containerd) vulnerabilities are
  fixed on the host, not in the image.
- AIOS does not verify signatures (cosign/Notary). Use a registry or admission policy if you
  need that.

### What you trust

- **The Docker executable**: resolved once from `PATH` when the sandbox starts and printed.
  Whoever controls your `PATH` controls what runs.
- **The daemon and its administrators**: anyone in the `docker` group, or with access to the
  socket, is root-equivalent on the host and can read code and output.
- **The Docker context**: `DOCKER_HOST` or the selected context decides which daemon runs the
  code. A non-local endpoint (`tcp://`, `ssh://`) is flagged at startup as remote, because code
  and output leave this machine.
- **The image**: pinning fixes which content runs; it does not make that content trustworthy.

### Platform dependencies

Enforcement comes from the daemon's kernel: cgroups (v2 recommended) for memory, CPU and PIDs,
namespaces for the network, seccomp and the default AppArmor/SELinux profile when enabled. Rootless
Docker and Docker Desktop change who enforces what. Rootless Docker needs cgroup v2 delegation for
the limits to apply, and Docker Desktop runs everything in its own Linux VM. These configurations
are not covered by CI; validate them with the opt-in real-Docker tests before relying on them.
