"""Network allowlist for a confined MCP server (`--mcp-egress NAME=HOSTS`). Standard library only.

The server runs with no network of its own (a fresh network namespace on Linux, sandbox-exec on
macOS). The only way out is an HTTPS proxy that AIOS runs, which opens a tunnel (HTTP CONNECT) only
to the hosts you listed:

* `api.github.com` allows that host on port 443, `*.example.com` its subdomains (not example.com
  itself), `host:8443` another port. Plain-http requests are refused: HTTPS only.
* A name must resolve only to public addresses, and the tunnel goes to the address that was checked,
  so DNS cannot point an allowed name at your router or the cloud metadata service. A literal address
  or `localhost` in the list is allowed as written.
* When AIOS itself has an HTTPS proxy (HTTPS_PROXY), tunnels go through it and it resolves names.
* Every tunnel, opened or refused, is written to the audit log.

On Linux the server's namespace has only its own loopback: BRIDGE runs there first, brings loopback
up, points the proxy variables at a port on it and forwards each connection to the proxy's Unix
socket outside, then runs the server (which still gets Landlock file confinement).
"""
import asyncio
import ipaddress
import re
import urllib.request

import aios_fetch

PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
MAX_TUNNELS = 32
HEAD_TIMEOUT = 10
CONNECT_TIMEOUT = 15
_PATTERN = re.compile(r"(\*\.)?([a-z0-9_-]+(?:\.[a-z0-9_-]+)*|\[[0-9a-f:.]+\])(?::(\d{1,5}))?")


def parse_patterns(text: str) -> list[tuple[str, int]]:
    """'api.github.com,*.example.com:8443' -> [('api.github.com', 443), ('*.example.com', 8443)]"""
    out = []
    for raw in text.split(","):
        item = raw.strip().lower().rstrip(".")
        m = _PATTERN.fullmatch(item)
        port = int(m.group(3)) if m and m.group(3) else 443
        if not m or not 0 < port < 65536 or (m.group(1) and m.group(2).startswith("[")):
            raise ValueError(f"bad host '{raw.strip()}': use host, *.domain or host:port")
        out.append(((m.group(1) or "") + m.group(2).strip("[]"), port))
    if not out:
        raise ValueError("no hosts given")
    return out


def proxy_env(url: str) -> dict:
    """What a server needs to send everything through the proxy at url (Node 24+ reads the
    variables only with NODE_USE_ENV_PROXY=1)."""
    return {**{k: url for k in PROXY_VARS}, "NO_PROXY": "", "no_proxy": "", "NODE_USE_ENV_PROXY": "1"}


def _literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return host == "localhost"


class EgressProxy:
    """HTTP CONNECT proxy that opens tunnels only to the allowed hosts. log(host, port, status,
    detail) is called for every request; status is 'ok', 'denied' or 'error'. upstream: the HTTP
    proxy to go through, "" for none; by default the one in HTTPS_PROXY, if any."""

    def __init__(self, patterns: list[tuple[str, int]], log=None, upstream: str | None = None):
        self.patterns, self.log, self.upstream = patterns, log or (lambda *a: None), upstream
        self._server = None
        self._slots = asyncio.Semaphore(MAX_TUNNELS)
        self._tasks: set = set()

    def match(self, host: str, port: int):
        """The pattern that allows host:port, or None."""
        host = host.lower().rstrip(".")
        for pat, pport in self.patterns:
            if pport == port and (host == pat or pat.startswith("*.") and host.endswith(pat[1:])):
                return pat
        return None

    async def start_unix(self, path: str) -> str:
        self._server = await asyncio.start_unix_server(self._handle, path)
        return path

    async def start_tcp(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def close(self):
        if self._server:
            self._server.close()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server:
            await self._server.wait_closed()

    async def _handle(self, r, w):
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            if self._slots.locked():
                return await self._reply(w, 503, "too many tunnels")
            async with self._slots:
                await self._serve(r, w)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            pass
        finally:
            self._tasks.discard(task)
            w.close()

    async def _reply(self, w, code: int, reason: str):
        w.write(f"HTTP/1.1 {code} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        await w.drain()

    async def _serve(self, r, w):
        head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        method, _, rest = head.split(b"\r\n", 1)[0].decode("latin-1").partition(" ")
        target = rest.partition(" ")[0]
        if method != "CONNECT":
            self.log(target[:200], 0, "denied", "only HTTPS (CONNECT) is allowed")
            return await self._reply(w, 405, "only HTTPS through CONNECT")
        host, _, port = target.rpartition(":")
        host = host.strip("[]")
        if not host or not port.isdigit():
            self.log(target[:200], 0, "denied", "bad CONNECT target")
            return await self._reply(w, 400, "bad target")
        port = int(port)
        pat = self.match(host, port)
        if pat is None:
            self.log(host, port, "denied", "not in the allowlist")
            return await self._reply(w, 403, "host not allowed")
        try:
            ur, uw = await asyncio.wait_for(self._open(host, port, _literal(pat)), CONNECT_TIMEOUT)
        except (OSError, ValueError, asyncio.TimeoutError) as e:
            self.log(host, port, "error", str(e) or type(e).__name__)
            return await self._reply(w, 502, "cannot connect")
        self.log(host, port, "ok", f"allowed by {pat}")
        await self._reply(w, 200, "Connection Established")
        try:
            await asyncio.gather(_pipe(r, uw), _pipe(ur, w))
        finally:
            uw.close()

    async def _open(self, host: str, port: int, allow_private: bool):
        upstream = urllib.request.getproxies().get("https") if self.upstream is None else self.upstream
        if upstream and not urllib.request.proxy_bypass(host):
            if not allow_private and aios_fetch._literal_ip(host) and not aios_fetch._public(host):
                raise ValueError(f"{host} is not a public address")
            return await _through(upstream, host, port)
        ip = await asyncio.to_thread(aios_fetch._resolve, host, port, allow_private)
        return await asyncio.open_connection(ip, port)


async def _through(proxy: str, host: str, port: int):
    """A tunnel to host:port through the HTTP proxy at proxy."""
    u = aios_fetch.check_url(proxy if "://" in proxy else "http://" + proxy)
    r, w = await asyncio.open_connection(u.hostname, u.port or 80)
    target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    w.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    await w.drain()
    head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
    line = head.split(b"\r\n", 1)[0].decode("latin-1")
    if line.split(" ", 2)[1:2] != ["200"]:
        w.close()
        raise OSError(f"upstream proxy refused: {line[:100]}")
    return r, w


async def _pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
        if w.can_write_eof():
            w.write_eof()
    except OSError:
        pass


# Runs inside the server's network namespace: python -I -c BRIDGE '{"sock": ..., "exec": [...]}'
BRIDGE = r"""
import asyncio, ctypes, fcntl, json, os, signal, socket, struct, sys
cfg = json.loads(sys.argv[1])
s = socket.socket()
fcntl.ioctl(s, 0x8914, struct.pack("16sH22x", b"lo", 0x1 | 0x8 | 0x40))  # SIOCSIFFLAGS lo UP|LOOPBACK|RUNNING
s.close()

async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
        if w.can_write_eof():
            w.write_eof()
    except OSError:
        pass

async def handle(r, w):
    try:
        ur, uw = await asyncio.open_unix_connection(cfg["sock"])
    except OSError:
        w.close()
        return
    try:
        await asyncio.gather(pipe(r, uw), pipe(ur, w))
    finally:
        uw.close()
        w.close()

async def main():
    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    env = {**os.environ, **{k: v.replace("PORT", str(srv.sockets[0].getsockname()[1]))
                            for k, v in cfg["env"].items()}}
    libc = ctypes.CDLL(None)
    child = await asyncio.create_subprocess_exec(*cfg["exec"], env=env,
                                                 preexec_fn=lambda: libc.prctl(1, signal.SIGKILL))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(sig, child.send_signal, sig)
    code = await child.wait()
    return code if code >= 0 else 128 - code

sys.exit(asyncio.run(main()))
"""
