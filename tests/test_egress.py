"""--mcp-egress: the allowlist proxy, and a confined server whose only way out is that proxy."""
import asyncio
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import aios  # noqa: E402
import aios_egress  # noqa: E402

NO_PROXY_ENV = {k: "" for k in aios_egress.PROXY_VARS}


async def echo_server():
    async def handle(r, w):
        while data := await r.read(1024):
            w.write(data)
            await w.drain()
        w.close()
    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


async def connect_via(port: int, target: str, method="CONNECT"):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"{method} {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    await w.drain()
    head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 10)
    return int(head.split(b" ")[1]), r, w


class PatternTests(unittest.TestCase):
    def test_hosts_ports_and_wildcards(self):
        self.assertEqual(aios_egress.parse_patterns("API.github.com, *.example.com:8443,127.0.0.1:9,[::1]:80"),
                         [("api.github.com", 443), ("*.example.com", 8443), ("127.0.0.1", 9), ("::1", 80)])
        p = aios_egress.EgressProxy(aios_egress.parse_patterns("api.github.com,*.example.com"))
        self.assertEqual(p.match("API.GitHub.com.", 443), "api.github.com")
        self.assertEqual(p.match("a.b.example.com", 443), "*.example.com")
        for host, port in (("example.com", 443), ("api.github.com", 80), ("api.github.com.evil.com", 443),
                           ("evilexample.com", 443), ("xapi.github.com", 443)):
            self.assertIsNone(p.match(host, port), (host, port))

    def test_bad_patterns_are_refused(self):
        for bad in ("", "*", "*.", "https://api.github.com", "api.github.com/x", "a*b.com", "host:0",
                    "host:70000", "*.[::1]", "user@host", "host,"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                aios_egress.parse_patterns(bad)


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env = mock.patch.dict(os.environ, NO_PROXY_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.echo, self.echo_port = await echo_server()
        self.events = []
        self.proxy = aios_egress.EgressProxy(aios_egress.parse_patterns(f"127.0.0.1:{self.echo_port},evil.example"),
                                             lambda *e: self.events.append(e))
        self.port = await self.proxy.start_tcp()

    async def asyncTearDown(self):
        await self.proxy.close()
        self.echo.close()
        await self.echo.wait_closed()

    async def test_an_allowed_host_gets_a_tunnel(self):
        code, r, w = await connect_via(self.port, f"127.0.0.1:{self.echo_port}")
        self.assertEqual(code, 200)
        w.write(b"ping")
        self.assertEqual(await asyncio.wait_for(r.readexactly(4), 10), b"ping")
        w.close()
        self.assertEqual(self.events, [("127.0.0.1", self.echo_port, "ok", f"allowed by 127.0.0.1")])

    async def test_other_hosts_ports_and_plain_http_are_refused(self):
        for target, method, want in ((f"127.0.0.2:{self.echo_port}", "CONNECT", 403),
                                     ("127.0.0.1:1", "CONNECT", 403),
                                     (f"http://127.0.0.1:{self.echo_port}/", "GET", 405),
                                     ("nonsense", "CONNECT", 400)):
            code, _, w = await connect_via(self.port, target, method)
            w.close()
            self.assertEqual(code, want, target)
        self.assertEqual([e[2] for e in self.events], ["denied"] * 4)

    async def test_an_allowed_name_that_resolves_to_a_private_address_is_refused(self):
        real = socket.getaddrinfo
        fake = lambda host, *a, **k: real("127.0.0.1", *a, **k) if host == "evil.example" else real(host, *a, **k)
        with mock.patch.object(socket, "getaddrinfo", fake):
            code, _, w = await connect_via(self.port, "evil.example:443")
        w.close()
        self.assertEqual(code, 502)
        self.assertEqual(self.events[0][2], "error")
        self.assertIn("not a public address", self.events[0][3])

    async def test_tunnels_go_through_the_users_own_proxy(self):
        upstream_events = []
        upstream = aios_egress.EgressProxy([("127.0.0.1", self.echo_port)], lambda *e: upstream_events.append(e), "")
        up_port = await upstream.start_tcp()
        try:
            with mock.patch.dict(os.environ, {"https_proxy": f"http://127.0.0.1:{up_port}", "HTTPS_PROXY": "",
                                              "no_proxy": "", "NO_PROXY": ""}):
                code, r, w = await connect_via(self.port, f"127.0.0.1:{self.echo_port}")
                self.assertEqual(code, 200, self.events)
                w.write(b"pong")
                self.assertEqual(await asyncio.wait_for(r.readexactly(4), 10), b"pong")
                w.close()
        finally:
            await upstream.close()
        self.assertEqual([e[2] for e in upstream_events], ["ok"], self.events)


@unittest.skipUnless(aios.egress_ok(), "needs a network namespace or sandbox-exec")
class ConfinedServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_proxy_is_the_only_way_out(self):
        env = mock.patch.dict(os.environ, NO_PROXY_ENV)
        env.start()
        self.addCleanup(env.stop)
        echo, echo_port = await echo_server()
        events = []
        proxy = aios_egress.EgressProxy([("127.0.0.1", echo_port)], lambda *e: events.append(e))
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as state, \
                tempfile.TemporaryDirectory() as prog, tempfile.TemporaryDirectory() as sockdir:
            if sys.platform == "darwin":
                via = await proxy.start_tcp()
                child_env = {**os.environ, **aios_egress.proxy_env(f"http://127.0.0.1:{via}")}
            else:
                via = await proxy.start_unix(os.path.join(sockdir, "p.sock"))
                child_env = dict(os.environ)
            Path(outside, "secret.txt").write_text("key", encoding="utf-8")
            server = Path(prog, "server.py")
            server.write_text("\n".join([
                "import os, socket, urllib.parse",
                "def try_(f):",
                "    try:",
                "        return f()",
                "    except OSError as e:",
                "        return type(e).__name__",
                "def via(target):",
                "    u = urllib.parse.urlsplit(os.environ['HTTPS_PROXY'])",
                "    s = socket.create_connection((u.hostname, u.port), 10)",
                "    s.sendall(f'CONNECT {target} HTTP/1.1\\r\\n\\r\\n'.encode())",
                "    head = b''",
                "    while b'\\r\\n\\r\\n' not in head:",
                "        head += s.recv(1)",
                "    if b' 200 ' not in head.split(b'\\r\\n')[0]:",
                "        return head.split(b' ')[1].decode()",
                "    s.sendall(b'ping')",
                "    return s.recv(4).decode()",
                f"print(try_(lambda: open({str(Path(outside, 'secret.txt'))!r}).read()))",
                f"print(try_(lambda: socket.create_connection(('127.0.0.1', {echo_port}), 3) and 'direct'))",
                f"print(try_(lambda: via('127.0.0.1:{echo_port}')))",
                "print(try_(lambda: via('127.0.0.1:1')))",
                "print(os.environ.get('NODE_USE_ENV_PROXY'))",
            ]), encoding="utf-8")
            argv = aios.confine_command(sys.executable, [str(server)], state, egress=via)
            p = await asyncio.create_subprocess_exec(*argv, env=child_env, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(p.communicate(), 60)
        await proxy.close()
        echo.close()
        lines = out.decode().splitlines()
        self.assertEqual(p.returncode, 0, err.decode())
        self.assertEqual(lines[0], "PermissionError", err.decode())
        self.assertNotEqual(lines[1], "direct", err.decode())
        self.assertEqual(lines[2:], ["ping", "403", "1"], err.decode())
        self.assertEqual([e[2] for e in events], ["ok", "denied"])

    def test_needs_full_network_isolation(self):
        with mock.patch.object(aios, "offline_level", return_value="tcp"), \
                self.assertRaisesRegex(RuntimeError, "--mcp-egress needs"):
            aios.confine_command(sys.executable, [], tempfile.gettempdir(), egress="x")


if __name__ == "__main__":
    unittest.main()
