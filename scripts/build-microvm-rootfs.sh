#!/bin/sh
# Build the read-only guest root image for `--sandbox microvm` from a local Docker image.
#   sudo scripts/build-microvm-rootfs.sh [OUT_DIR]     (default: $AIOS_MICROVM_DIR or ~/.aios/microvm)
# Needs: docker (the image already pulled; nothing is pulled here), tar, mkfs.ext4 with -d.
# Run as root so file ownership inside the image is preserved.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
out=${1:-${AIOS_MICROVM_DIR:-$HOME/.aios/microvm}}
image=${AIOS_MICROVM_IMAGE:-python:3.12-slim}
[ "$(id -u)" = 0 ] || { echo "run as root (sudo) so ownership is kept" >&2; exit 1; }
mkdir -p "$out"
tmp=$(mktemp -d)
cid=""
cleanup() { [ -n "$cid" ] && docker rm -f "$cid" >/dev/null 2>&1; rm -rf "$tmp"; }
trap cleanup EXIT
cid=$(docker create --pull never "$image")
mkdir "$tmp/root"
docker export "$cid" | tar -C "$tmp/root" -xpf -
install -m 0755 -o 0 -g 0 "$here/aios_microvm_init.py" "$tmp/root/aios-init"
rm -f "$tmp/root/.dockerenv"
for d in proc sys dev tmp run; do mkdir -p "$tmp/root/$d"; done
size=$(( $(du -sm "$tmp/root" | cut -f1) + 64 ))
rm -f "$out/rootfs.ext4"
mkfs.ext4 -q -F -L aios-root -d "$tmp/root" "$out/rootfs.ext4" "${size}M"
chmod 0644 "$out/rootfs.ext4"
echo "built $out/rootfs.ext4 (${size} MiB) from $image $(docker image inspect --format '{{json .RepoDigests}}' "$image")"
