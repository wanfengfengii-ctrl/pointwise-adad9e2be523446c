# -*- coding-utf-8 -*-
"""统一测试入口：对照、边界、内存实测。

运行：``python3 -I run_tests.py``
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for module in ("tests.test_crosscheck",
                   "tests.test_boundary",
                   "tests.test_memory"):
        suite.addTests(loader.loadTestsFromName(module))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
