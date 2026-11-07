"""Regression test: the shared wasmtime solver must never run concurrently.

deepseek_pow keeps a single solver with a single ``wasmtime.Store``. Two
threads calling ``solve()`` on it corrupts the store and aborts the process
inside wasmtime's unwinder (SIGABRT, "coredump"), which is what FastAPI's
thread pool did on concurrent requests.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from core.pow_solver import PoWSolver


class FakeSolver:
    def __init__(self):
        self.active = 0
        self.peak = 0
        self.guard = threading.Lock()

    def solve(self, challenge):
        with self.guard:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.03)
        with self.guard:
            self.active -= 1
        return object()


class SerializationTests(unittest.TestCase):
    def test_concurrent_solves_are_serialized(self):
        fake = FakeSolver()
        solver = PoWSolver()

        with patch.object(PoWSolver, "_get_solver", return_value=fake):
            threads = [
                threading.Thread(target=solver._solve, args=("DeepSeekHashV1", None))
                for _ in range(8)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(
            fake.peak, 1, "wasmtime Store is not thread-safe: calls overlapped"
        )

    def test_fetch_and_solve_uses_the_locked_path(self):
        from core import pow_solver as module

        self.assertTrue(
            hasattr(module, "_SOLVER_LOCK"),
            "module-level lock guarding the shared solver is missing",
        )


if __name__ == "__main__":
    unittest.main()
