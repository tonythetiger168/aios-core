"""aios_admit: sandboxes start only while their memory limits fit in the host's free memory."""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios  # noqa: E402
import aios_admit  # noqa: E402


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_while_full_and_goes_in_arrival_order(self):
        free = [1000]  # MiB the fake host has free
        g = aios_admit.MemoryGate(reserve_mb=200, max_running=None, wait_s=5, probe=lambda: free[0])
        order, release = [], asyncio.Event()

        async def call(name, mb):
            async with g.admit(mb):
                order.append(name)
                await release.wait()

        tasks = [asyncio.create_task(call(n, 300)) for n in "abc"]
        await asyncio.sleep(0.1)
        # a: the first one always starts; b: 1000 - 300 >= 300 + 200; c: 1000 - 600 < 500 -> waits
        self.assertEqual(order, ["a", "b"])
        self.assertEqual((g.running, g.reserved_mb, g.waited), (2, 600, 1))
        free[0] = 1300  # memory freed elsewhere on the host
        await asyncio.sleep(aios_admit.POLL_S * 2)
        self.assertEqual(order, ["a", "b", "c"])
        release.set()
        await asyncio.gather(*tasks)
        self.assertEqual((g.running, g.reserved_mb), (0, 0))

    async def test_the_only_call_always_starts_and_a_timeout_fails_cleanly(self):
        g = aios_admit.MemoryGate(reserve_mb=0, max_running=None, wait_s=0.3, probe=lambda: 10)
        async with g.admit(256):  # far more than is free, but nothing else runs
            with self.assertRaises(aios_admit.AdmissionError) as cm:
                async with g.admit(256):
                    pass
        self.assertIn("no room for another 256 MiB sandbox", str(cm.exception))
        self.assertEqual(g.running, 0)

    async def test_count_cap_and_unknown_memory(self):
        g = aios_admit.MemoryGate(reserve_mb=0, max_running=2, wait_s=0.3, probe=lambda: None)
        self.assertEqual(g.capacity(256), 2)
        async with g.admit(256), g.admit(256):
            with self.assertRaises(aios_admit.AdmissionError):
                async with g.admit(256):
                    pass
        self.assertIsNone(aios_admit.MemoryGate(max_running=None, probe=lambda: None).capacity(256))
        self.assertEqual(aios_admit.MemoryGate(reserve_mb=512, max_running=None,
                                               probe=lambda: 4608).capacity(256), 16)

    def test_settings_come_from_the_environment(self):
        with mock.patch.dict(os.environ, {"AIOS_SANDBOX_RESERVE_MB": "64", "AIOS_SANDBOX_MAX": "3",
                                          "AIOS_SANDBOX_WAIT": "9"}):
            g = aios_admit.MemoryGate()
        self.assertEqual((g.reserve_mb, g.max_running, g.wait_s), (64, 3, 9.0))
        with mock.patch.dict(os.environ, {"AIOS_SANDBOX_MAX": "lots"}), self.assertRaises(SystemExit):
            aios_admit.MemoryGate()

    @unittest.skipUnless(sys.platform.startswith("linux") or sys.platform == "win32", "Linux/Windows only")
    def test_free_memory_is_read_here(self):
        self.assertGreater(aios_admit.available_mb(), 0)


class RunPythonUsesTheGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_container_and_vm_calls_go_through_the_gate(self):
        class FakeBox:
            name, timeout, mem_mb = "docker", 10, 256

            async def run_python(self, code):
                return f"ran with {gate.running} running, {gate.reserved_mb} MiB reserved"

        gate = aios_admit.MemoryGate(reserve_mb=0, max_running=None, probe=lambda: 10_000)
        k = aios.Kernel(store=aios.Store(":memory:"))
        self.addCleanup(k.store.db.close)
        with mock.patch.object(aios, "make_sandbox", lambda *_: FakeBox()), \
                mock.patch.object(aios_admit, "GATE", gate):
            aios.install_builtin_tools(k)
            out = await k.tools.call("run_python", aios.Agent("t", {"run_python"}), code="1")
        self.assertEqual(out, "ran with 1 running, 256 MiB reserved")
        self.assertEqual(gate.running, 0)


if __name__ == "__main__":
    unittest.main()
