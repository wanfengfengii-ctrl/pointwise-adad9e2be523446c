# -*- coding-utf-8 -*-
"""端到端演示：任意切块、禁用站点过滤、忽略字段、临时文件自动清理。

直接运行：``python3 -I demo.py``
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from portaljson import extract  # noqa: E402

DOCUMENT = r"""
{
  "version": 1,
  "stations": [
    {
      "id": "alpha-站点",
      "enabled": true,
      "samples": [
        {"t": 1700000000, "v": -9223372036854775808, "note": "边界值"},
        {"t": 1700000060, "v": 9223372036854775807},
        {"t": 1700000120, "v": 0}
      ],
      "firmware": {"name": "x1", "logs": ["a", "b", {"deep": [1, 2]}]}
    },
    {
      "id": "beta",
      "enabled": false,
      "samples": [{"t": 1, "v": 999}]
    },
    {
      "id": "gamma",
      "enabled": true,
      "samples": [],
      "blob": "THIS FIELD IS SKIPPED"
    }
  ],
  "trace": "ignored"
}
"""


def main():
    data = DOCUMENT.encode("utf-8")
    chunk_size = 17  # 故意切得很碎，切穿多字节字符与转义
    chunks = (data[i:i + chunk_size]
              for i in range(0, len(data), chunk_size))

    with tempfile.TemporaryDirectory(prefix="portaljson-demo-") as workdir:
        print("workdir: %s（结束后应保持为空）" % workdir)
        rows = list(extract(chunks, workdir))
        print("产出 %d 条采样：" % len(rows))
        for sid, t_value, v_value in rows:
            print("  id=%-10s t=%-12d v=%d" % (sid, t_value, v_value))
        leftovers = os.listdir(workdir)
        print("workdir 残留文件: %s" % (leftovers or "无"))
        assert not leftovers
        assert rows == [
            ("alpha-站点", 1700000000, -9223372036854775808),
            ("alpha-站点", 1700000060, 9223372036854775807),
            ("alpha-站点", 1700000120, 0),
        ], rows
        print("演示通过：禁用站点 beta 被过滤，空采样 gamma 无输出。")


if __name__ == "__main__":
    main()
