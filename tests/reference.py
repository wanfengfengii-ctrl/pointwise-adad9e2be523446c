# -*- coding-utf-8 -*-
"""独立对照实现。

刻意不复用 portaljson 的任何代码：直接用标准库 ``json`` 整体解析，再按
契约过滤 enabled 站点并按源序展开采样。仅用于规模可控的对照测试。
"""

import json


def reference_extract(data):
    """输入 bytes/str，返回 [(id, t, v), ...]。"""
    if isinstance(data, (bytes, bytearray)):
        doc = json.loads(data.decode("utf-8"))
    else:
        doc = json.loads(data)
    out = []
    for station in doc["stations"]:
        if station["enabled"] is not True:
            continue
        sid = station["id"]
        for sample in station["samples"]:
            out.append((sid, sample["t"], sample["v"]))
    return out
