"""Deterministic smoke tests for the opt-in CPU executor benchmark (#105).

Run: python -m unittest tests.test_cpu_executor_baseline -v
"""
import unittest

from benchmarks import cpu_executor_baseline as bench


class CpuExecutorBaselineTests(unittest.TestCase):
    def test_prime_workload(self):
        self.assertEqual(bench.count_primes(3), 1)
        self.assertEqual(bench.count_primes(10), 4)
        self.assertEqual(bench.count_primes(100), 25)

    def test_percentiles(self):
        self.assertEqual(bench.percentile([1.0], 0.95), 1.0)
        self.assertEqual(bench.percentile([4.0, 1.0, 3.0, 2.0], 0), 1.0)
        self.assertEqual(bench.percentile([4.0, 1.0, 3.0, 2.0], 1), 4.0)
        self.assertAlmostEqual(bench.percentile([1.0, 3.0], 0.5), 2.0)

    def test_serial_asyncio_and_threads_match(self):
        for kind, workers in (("serial", 1), ("asyncio", 1), ("threads", 2)):
            with self.subTest(kind=kind):
                row = bench.measure(kind, workers, jobs=4, work=100, expected=25)
                self.assertEqual(row["checksum"], 25)
                self.assertEqual(row["jobs"], 4)
                self.assertGreater(row["seconds"], 0)
                self.assertGreater(row["jobs_per_second"], 0)
                self.assertLessEqual(row["completion_p50_ms"], row["completion_p95_ms"])
                self.assertLessEqual(row["completion_p95_ms"], row["completion_p99_ms"])

    def test_process_pool_correctness(self):
        row = bench.measure("processes", workers=2, jobs=4, work=100, expected=25)
        self.assertEqual(row["executor"], "processes")
        self.assertEqual(row["checksum"], 25)

    def test_checksum_mismatch_fails_closed(self):
        with self.assertRaises(RuntimeError):
            bench.measure("serial", workers=1, jobs=1, work=10, expected=-1)


if __name__ == "__main__":
    unittest.main()
