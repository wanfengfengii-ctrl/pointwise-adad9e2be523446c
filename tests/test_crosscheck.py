# -*- coding-utf-8 -*-
"""对照测试：portaljson.extract 与独立 json 实现结果必须一致。"""

import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from portaljson import extract  # noqa: E402
from tests.reference import reference_extract  # noqa: E402

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1


def _junk_value(rng):
    """生成形态各异的“忽略字段”值（含大字符串与深层嵌套）。"""
    kind = rng.randrange(6)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.randint(-10 ** 12, 10 ** 12)
    if kind == 3:
        return "x" * rng.randrange(0, 500)
    if kind == 4:
        return [rng.randrange(1000) for _ in range(rng.randrange(0, 20))]
    return {
        "nested": [{"a": [rng.randrange(10) for _ in range(3)]}]
        for _ in range(rng.randrange(0, 5))
    }


def _random_station(rng, sid):
    n_samples = rng.randrange(0, 8)
    samples = []
    for _ in range(n_samples):
        sample = {"t": rng.randint(INT64_MIN, INT64_MAX),
                  "v": rng.randint(INT64_MIN, INT64_MAX)}
        # 采样上的额外字段必须被跳过
        if rng.random() < 0.4:
            sample["extra"] = _junk_value(rng)
        rng.shuffle(list(sample.items()))
        samples.append(sample)
    station = {
        "id": sid,
        "enabled": rng.random() < 0.7,
        "samples": samples,
    }
    # 站点级额外字段（含巨大字符串）
    if rng.random() < 0.6:
        station["debug"] = _junk_value(rng)
    if rng.random() < 0.3:
        station["blob"] = "B" * rng.randrange(0, 2000)
    return station


def _random_document(rng):
    n_stations = rng.randrange(1, 12)
    stations = [
        _random_station(rng, "S%02d-%d" % (i, rng.randrange(1000)))
        for i in range(n_stations)
    ]
    doc = {"stations": stations}
    if rng.random() < 0.5:
        doc["meta"] = _junk_value(rng)
    return doc


def _chunk_sizes(rng):
    mode = rng.randrange(4)
    if mode == 0:
        return [1]                       # 每字节一块（最极端）
    if mode == 1:
        return [rng.randrange(1, 8) for _ in range(64)]
    if mode == 2:
        return [rng.randrange(1, 128) for _ in range(32)]
    return [rng.randrange(1, 4096) for _ in range(16)]


def _iter_chunks(data, sizes):
    pos = 0
    i = 0
    while pos < len(data):
        size = sizes[i % len(sizes)]
        i += 1
        yield data[pos:pos + size]
        pos += size


class CrossCheckTest(unittest.TestCase):

    def test_random_documents_match_reference(self):
        rng = random.Random(20261114)
        for trial in range(60):
            doc = _random_document(rng)
            data = __import__("json").dumps(
                doc, ensure_ascii=False).encode("utf-8")
            expected = reference_extract(data)
            sizes = _chunk_sizes(rng)
            with tempfile.TemporaryDirectory() as wd:
                got = list(extract(_iter_chunks(data, sizes), wd))
                self.assertEqual(got, expected)
                self.assertEqual(os.listdir(wd), [])

    def test_unicode_ids_and_keys(self):
        rng = random.Random(77)
        doc = {"stations": [
            {"id": "站点-α😀", "enabled": True,
             "samples": [{"t": -1, "v": INT64_MIN},
                         {"t": INT64_MAX, "v": 0}],
             "备注": [1, {"x": "y"}]},
            {"id": "β", "enabled": False,
             "samples": [{"t": 5, "v": 6}]},
        ]}
        data = __import__("json").dumps(doc, ensure_ascii=False).encode("utf-8")
        expected = reference_extract(data)
        for size in (1, 2, 3, 7, 64):
            with tempfile.TemporaryDirectory() as wd:
                got = list(extract(
                    (data[i:i + size] for i in range(0, len(data), size)),
                    wd))
                self.assertEqual(got, expected)
                self.assertEqual(os.listdir(wd), [])

    def test_bytearray_chunks_accepted(self):
        data = b'{"stations":[{"id":"z","enabled":true,"samples":' \
               b'[{"t":1,"v":2}]}]}'
        with tempfile.TemporaryDirectory() as wd:
            chunks = [bytearray(data[i:i + 3])
                      for i in range(0, len(data), 3)]
            self.assertEqual(list(extract(iter(chunks), wd)),
                             [("z", 1, 2)])

    def test_empty_stations_array(self):
        with tempfile.TemporaryDirectory() as wd:
            self.assertEqual(list(extract(iter([b'{"stations":[]}']), wd)),
                             [])
            self.assertEqual(os.listdir(wd), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
