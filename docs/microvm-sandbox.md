# microVM sandbox (`--sandbox microvm`)

Status: **experimental**. The backend exists and is selectable, but it counts as verified
only on a platform where the opt-in real-microVM tests (`AIOS_TEST_MICROVM=1`) have passed
and their evidence was kept. The Docker CI jobs say nothing about this backend.

## Threat model

`run_python` runs code that a model wrote, possibly steered by prompt injection. Assume the
code is hostile and tries to:

1. read or change files on the host (the workspace, keys, the AIOS database);
2. reach the network (exfiltration, or attacks on the LAN or cloud metadata endpoints);
3. escalate inside the sandbox and then attack the boundary itself;
4. exhaust host CPU, memory, disk or processes, or outlive its call;
5. forge or flood its own result to mislead the agent or the host.

Trusted: the host kernel and KVM, the Firecracker binary, the guest kernel image, the root
image and its `/aios-init`, and the AIOS process and user that start the VM.

Out of scope: KVM or Firecracker vulnerabilities, CPU side channels (Spectre-class,
cross-VM cache timing), a malicious host administrator, and denial of service against the
host through the VMM's own resource use beyond the limits listed below.

## Isolation boundary

Each call boots a new Firecracker microVM with its own guest kernel (hardware virtualization
via KVM). Compared with Docker, the code no longer shares the host kernel: a guest kernel
exploit gives root inside a disposable VM, not on the host. The VM gets:

| Resource | Setting |
|---|---|
| Network | none: no `network-interfaces`, no vsock, no MMDS in the config. Only `lo` exists. |
| Host files | none shared. Two block devices only: the root image (read-only) and a per-call job image (read-only) holding the code. |
| CPU / memory | 1 vCPU, 256 MiB (configurable), SMT off. Memory is a hard VM limit. |
| Disk | no writable disk. `/tmp` is a 16 MiB tmpfs (counts against VM memory); files are capped at 8 MiB. |
| Time | the guest kills the code's whole session at the timeout; the host kills the VMM if the VM has not finished by timeout + 15 s boot allowance. |
| Output | at most `max_out` bytes (10,000 by default) leave the guest; the host retains at most 4×max_out + 64 KiB of console output. |

Inside the guest, defence in depth: `/aios-init` (PID 1, root) runs the code as UID/GID
65534 with no supplementary groups, `no_new_privs`, an 8 MiB file-size limit and 64
processes. `/proc` is mounted `hidepid`, so the code cannot see the init. The job device
and `/run` (where the init keeps the code and output) are root-only, so the code never sees
the per-call nonce and cannot forge a result block on the console, which only root can
write.

## Lifecycle of one call

1. Make a private temp dir `aios-mvm-*`. Write `job.img` (header + code, padded to 512
   bytes) and `config.json`.
2. Start `firecracker --no-api --config-file config.json` in its own process group, stdin
   closed, console on a pipe.
3. The guest boots, `/aios-init` mounts proc/sys/dev/tmpfs, reads the job, runs
   `python3 -I -B -` as nobody, writes the base64 result between
   `AIOS-BEGIN-<nonce>` / `AIOS-END-<nonce> status=… rc=… truncated=…` and reboots, which
   ends Firecracker (`reboot=k`).
4. The host parses only the block carrying this call's nonce. No block means the sandbox
   failed: that is raised as an error, never reported as the code's output.
5. Always (success, timeout, error or cancellation): SIGKILL the process group, wait up to
   3 s to confirm exit (otherwise raise "cleanup unconfirmed"), delete the temp dir.

## Snapshots (on by default)

Booting costs about a second per call, so AIOS boots once and then restores:

1. The first call (per process, or whenever Firecracker, the kernel, the root image or the VM
   shape changes) boots a VM with `aios_wait=1` and an empty job drive of a fixed size. The init
   mounts proc/sys/dev/tmpfs, prints `AIOS-READY` and waits for a line on the console. No code
   has run in it yet. AIOS pauses it and saves a full snapshot to
   `$AIOS_MICROVM_DIR/snapshots/<key>/` (`vm.snap`, `vm.mem`, `job.img`, all read-only),
   removing snapshots made for older binaries or images. This VMM runs as the AIOS user even in
   jailed mode, since the guest runs nothing untrusted before the snapshot.
2. A probe call (`print('ok')`) restores it once. If making or probing the snapshot fails, for
   example with a root image built before snapshots, AIOS says so once and every call boots as
   before.
3. Each call starts a fresh VMM (jailed if configured), restores the snapshot into it with this
   call's job drive in the same place (paths in the snapshot are relative), and sends the line.
   The init drops the block cache for the job drive, reads the job, mixes the job's 32 fresh
   random bytes into the kernel's pool and forces a reseed (`RNDADDENTROPY`, `RNDRESEEDCRNG`;
   it refuses to run the code if that fails), sets the clock, then runs the code as before.

What this changes: every call still gets its own VM and VMM, so nothing a call does reaches the
next one; all calls start from the same memory image, so guest kernel and init addresses (KASLR)
are the same in every call, as they would be for anyone who can run code in one VM and read
them. Random numbers are not shared, because of the reseed. `AIOS_MICROVM_SNAPSHOT=0` turns
snapshots off. A snapshot holds the guest's memory (256 MiB by default) on disk.

## Selection: no silent fallback

`microvm` is chosen only by `--sandbox microvm` or `AIOS_SANDBOX=microvm`; `auto` never
picks it. If anything is missing (not Linux, no `/dev/kvm` access, no firecracker, kernel or
root image) AIOS refuses to start with the reason. It never downloads or builds anything.

## Setup (Linux x86_64 with KVM)

Files live in `$AIOS_MICROVM_DIR` (default `~/.aios/microvm`):

```sh
d=~/.aios/microvm; mkdir -p $d
# Firecracker release binary (CI pins v1.13.1, tarball sha256 59450b91…7b4faf)
curl -fsSL https://github.com/firecracker-microvm/firecracker/releases/download/v1.13.1/firecracker-v1.13.1-x86_64.tgz | tar -xz
install -m 0755 release-v1.13.1-x86_64/firecracker-v1.13.1-x86_64 $d/firecracker
# A guest kernel; CI uses Firecracker's own CI build (sha256 b36a4a1b…7e8724)
curl -fsSL -o $d/vmlinux https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.13/x86_64/vmlinux-6.1.141
# The root image, from a local Docker image (nothing is pulled implicitly)
docker pull python:3.12-slim
sudo scripts/build-microvm-rootfs.sh $d
python aios.py chat --sandbox microvm
```

Your user needs read/write access to `/dev/kvm` (usually the `kvm` group).

## Jailed VMMs (AIOS running as root)

When AIOS runs as root, for example as a system service, set `AIOS_MICROVM_JAILER=1` and put
the `jailer` binary from the same Firecracker release next to `firecracker`:

```sh
install -m 0755 release-v1.13.1-x86_64/jailer-v1.13.1-x86_64 $d/jailer
AIOS_MICROVM_JAILER=1 python aios.py daemon --sandbox microvm ...
```

Each call then gets its own jail. The kernel and root image are hard-linked (copied if they live
on another filesystem) into a fresh chroot under `$AIOS_MICROVM_DIR/jails/firecracker/<id>/root`,
owned by uid/gid 65534 and mode 0700, next to the job drive and config. The jailer starts
Firecracker in a new mount namespace, chrooted there, as uid/gid 65534 with no capabilities. On
cgroup v2 it runs in its own cgroup capped at the guest memory plus 128 MiB. After the call, the
jail directory and the cgroup are removed. The kernel and root image must be readable by uid
65534. AIOS refuses to start in this mode when it is not root or the jailer is missing.
The microVM CI job runs the real tests both ways. When jailed, it also checks from the host that
the running VMM's uid, gid, capabilities, chroot and cgroup are as described, and that both the
jail and the cgroup are gone afterwards.

## Known limits

- By default Firecracker runs as the AIOS user without the `jailer` (no chroot, cgroups or
  separate UID for the VMM). Firecracker's built-in seccomp filters are on. See "Jailed VMMs"
  below for deployments that treat the VMM itself as an attack surface.
- Without snapshots (`AIOS_MICROVM_SNAPSHOT=0`, or a root image that predates them), boot costs roughly a second per call. There is no warm pool of running VMs.
- The root image comes from a mutable Docker tag; record its digest (the build script
  prints it). No image signing.
- Linux x86_64 only in CI. aarch64 should work with an aarch64 kernel and binary but is
  untested. macOS and Windows: use `--sandbox docker`.
- Guest kernel console noise is kept low (`quiet loglevel=1`) but not filtered; only the
  nonce block is trusted.

## Verification

- Unit tests (`tests/test_microvm.py`, every platform): job encoding and parsing, config
  contents (no network devices, read-only drives), result parsing, output bounds, guest
  and host timeouts, cleanup of the temp dir and VMM after timeout and cancellation, VMM
  failure raising instead of returning, a block with the wrong nonce being ignored; for
  snapshots (Linux), one boot then a restore per call, a fresh seed and fixed-size job drive
  per call, a new snapshot when an image changes, falling back to booting when the init
  predates snapshots or a restore fails, jailed restores, cancellation. These use a fake
  `firecracker` and do not prove isolation.
- Real tests (`RealMicroVMTests`, `AIOS_TEST_MICROVM=1`), run by the `microvm` CI job on
  an Ubuntu runner with KVM: guest kernel differs from the host's, UID/GID 65534, no groups,
  no capabilities, `NoNewPrivs`, only `lo`, no outbound connection, read-only root, empty
  `/tmp` of at most 16 MiB, only `vda`/`vdb`, job drive and `/run` unreadable, init hidden,
  MemTotal below 256 MiB, a 512 MiB allocation failing, timeout plus no leftover VMM or
  temp dir, a fork bomb and output flood contained; restored calls start with an empty `/tmp`,
  different random numbers and the right time, and are faster than a booted call (the timings
  are in the evidence). The job runs these tests with snapshots, again with
  `AIOS_MICROVM_SNAPSHOT=0`, and jailed.
- Evidence: the CI job uploads `microvm-evidence` (runner kernel, KVM node, Firecracker
  version, sha256 of binary, kernel and root image, image digest, full test output).
