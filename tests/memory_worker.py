# -*- coding-utf-8 -*-
"""内存实测 worker（在独立进程中运行）。

用法::

    python3 -I tests/memory_worker.py <workdir> <mode> <mib> <chunk_kib>

mode:
  station   —— 单个启用站点，samples 体量约 mib MiB（整站只能落盘）
  ignored   —— 站点的忽略字段含约 mib MiB 巨型字符串（禁止物化忽略值）

输出一行 JSON：{"peak": <字节>, "rss": <KiB>, "leftover": <文件数>,
                "count": <消费条数>}
"""

import json
import os
import resource
import sys
import tracemalloc

# 仓库根加入 sys.path（-I 下没有 cwd）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from portaljson import extract  # noqa: E402


def _stream_station(workdir, mib, chunk):
    target = mib * 1024 * 1024
    header = b'{"stations":[{"id":"big","enabled":true,"samples":['
    footer = b']}]}'
    counter = {"i": 0}

    def gen():
        yield header
        buf = bytearray()
        i = 0
        written = len(header)
        while written < target:
            rec = b'{"t":%d,"v":%d},' % (i, -i)
            buf.extend(rec)
            written += len(rec)
            i += 1
            while len(buf) >= chunk:
                yield bytes(buf[:chunk])
                del buf[:chunk]
        # 去掉最后一个逗号并收尾
        if buf and buf[-1] == 0x2C:
            buf[-1] = 0x5D  # ',' -> ']'
            buf.extend(footer[1:])
        else:
            buf.extend(footer)
        while buf:
            yield bytes(buf[:chunk])
            del buf[:chunk]
        counter["i"] = i

    return gen(), counter


def _stream_ignored(workdir, mib, chunk):
    target = mib * 1024 * 1024
    # 禁用站点 + 巨型忽略字符串：既测忽略值不物化，也测禁用站不产出
    prefix = (b'{"stations":[{"id":"off","enabled":false,"samples":['
              b'{"t":1,"v":2}],"blob":"')
    suffix = b'"}],"meta":{"k":[1,2,3]}}'

    def gen():
        yield prefix
        remaining = target
        fill = b"q" * min(chunk, 1 << 20)
        while remaining > 0:
            piece = fill[:min(len(fill), remaining)]
            remaining -= len(piece)
            yield piece
        yield suffix

    return gen(), 0


def main():
    workdir, mode, mib_s, chunk_s = sys.argv[1:5]
    mib = int(mib_s)
    chunk = int(chunk_s) * 1024

    tracemalloc.start()
    if mode == "station":
        chunks, expected = _stream_station(workdir, mib, chunk)
    elif mode == "ignored":
        chunks, expected = _stream_ignored(workdir, mib, chunk)
    else:
        raise SystemExit("unknown mode: %s" % mode)

    count = 0
    it = extract(chunks, workdir)
    # 消费端不保存结果，逐条丢弃
    for _sid, _t, _v in it:
        count += 1
    it.close()

    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = {
        "peak": peak,
        "rss_kib": rss,
        "leftover": len(os.listdir(workdir)),
        "count": count,
        "expected": expected["i"] if mode == "station" else 0,
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
