# -*- coding-utf-8 -*-
"""内存实测：64 KiB 块、32 MiB 站点/忽略值，Python 峰值分配 ≤ 16 MiB。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "tests", "memory_worker.py")

LIMIT = 16 * 1024 * 1024          # 16 MiB Python 峰值分配
TARGET_MIB = 32                   # 站点/忽略值各达 32 MiB
CHUNK_KIB = 64                    # 64 KiB 块


class MemoryTest(unittest.TestCase):

    def _run_worker(self, mode, mib):
        with tempfile.TemporaryDirectory(prefix="portaljson-mem-") as wd:
            proc = subprocess.run(
                [sys.executable, "-I", WORKER, wd, mode, str(mib),
                 str(CHUNK_KIB)],
                capture_output=True, text=True, timeout=480,
            )
            if proc.returncode != 0:
                self.fail("worker 失败:\n%s\n%s"
                          % (proc.stdout, proc.stderr))
            result = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertEqual(result["leftover"], 0,
                             "workdir 残留临时文件: %r" % result)
            return result

    def test_large_enabled_station_spools_to_disk(self):
        result = self._run_worker("station", TARGET_MIB)
        self.assertGreater(result["count"], 100_000)
        self.assertEqual(result["count"], result["expected"])
        self.assertLessEqual(
            result["peak"], LIMIT,
            "峰值分配 %.1f MiB 超过 16 MiB 限制" % (result["peak"] / 2 ** 20))
        print("\n[station 32MiB] Python 峰值 %.2f MiB，RSS %.1f MiB，"
              "采样 %d 条"
              % (result["peak"] / 2 ** 20,
                 result["rss_kib"] / 1024, result["count"]))

    def test_large_ignored_value_never_materialized(self):
        result = self._run_worker("ignored", TARGET_MIB)
        self.assertEqual(result["count"], 0, "禁用站点不应产出采样")
        self.assertLessEqual(
            result["peak"], LIMIT,
            "峰值分配 %.1f MiB 超过 16 MiB 限制" % (result["peak"] / 2 ** 20))
        print("\n[ignored 32MiB] Python 峰值 %.2f MiB，RSS %.1f MiB"
              % (result["peak"] / 2 ** 20, result["rss_kib"] / 1024))


if __name__ == "__main__":
    unittest.main(verbosity=2)
