# -*- coding-utf-8 -*-
"""边界与负向测试：截断、非法 UTF-8、JSON 语法错误、契约违约、清理保证。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from portaljson import extract  # noqa: E402

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1


def collect(data, wd, size=1):
    """以固定粒度切块并耗尽迭代器。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    chunks = (data[i:i + size] for i in range(0, len(data), size))
    return list(extract(chunks, wd))


def expect_error(self, data, size=1, exc=ValueError):
    with tempfile.TemporaryDirectory() as wd:
        with self.assertRaises(exc):
            collect(data, wd, size=size)
        # 报错路径也必须清理临时文件
        self.assertEqual(os.listdir(wd), [])


class TruncationTest(unittest.TestCase):
    """合法文档的每个非空前缀都必须报 ValueError（单字节切块）。"""

    VALID = (
        b'{"stations":[{"id":"A","enabled":true,'
        b'"samples":[{"t":1,"v":2},{"t":3,"v":4}]},'
        b'{"id":"B","enabled":false,"samples":[]}],"meta":[1,2,{}]}'
    )

    def test_every_prefix_is_rejected(self):
        for cut in range(1, len(self.VALID)):
            expect_error(self, self.VALID[:cut], size=1)

    def test_empty_input(self):
        expect_error(self, b"")
        expect_error(self, b"   \n\t ")

    def test_truncated_utf8_multibyte(self):
        # 三字节 UTF-8 字符从不同位置切开，最后一块是残片
        data = '{"stations":[{"id":"中","enabled":true,"samples":[]}]}' \
            .encode("utf-8")
        for cut in range(len(data) - 2, len(data)):
            expect_error(self, data[:cut], size=1)


class InvalidUtf8Test(unittest.TestCase):

    def test_bad_continuation(self):
        expect_error(self, b'{"stations":[{"id":"\xe4\xb8","enabled":'
                           b'true,"samples":[]}]}')

    def test_lone_continuation_byte(self):
        expect_error(self, b'{"stations":[{"id":"\x80x","enabled":true,'
                           b'"samples":[]}]}')

    def test_overlong_encoding(self):
        # E0 80 80 是 NUL 的过长编码
        expect_error(self, b'{"stations":[{"id":"\xe0\x80\x80","enabled":'
                           b'true,"samples":[]}]}')
        # C0 AF 是斜杠的两字节过长编码
        expect_error(self, b'{"stations":[{"id":"\xc0\xaf","enabled":true,'
                           b'"samples":[]}]}')

    def test_surrogate_and_out_of_range(self):
        expect_error(self, b'{"stations":[{"id":"\xed\xa0\x80","enabled":'
                           b'true,"samples":[]}]}')  # U+D800
        expect_error(self, b'{"stations":[{"id":"\xf4\x90\x80\x80",'
                           b'"enabled":true,"samples":[]}]}')  # U+110000

    def test_bad_utf8_split_across_chunks(self):
        raw = b'{"stations":[{"id":"\xff\xfe","enabled":true,' \
              b'"samples":[]}]}'
        expect_error(self, raw, size=1)
        expect_error(self, raw, size=2)


class JsonSyntaxTest(unittest.TestCase):

    def test_top_level_must_be_object(self):
        expect_error(self, b'[1,2,3]')
        expect_error(self, b'"hello"')
        expect_error(self, b'42')
        expect_error(self, b'true')
        expect_error(self, b'null')

    def test_trailing_data(self):
        expect_error(self, b'{"stations":[]}garbage')
        expect_error(self, b'{"stations":[]}{}')
        expect_error(self, b'{"stations":[]} 123')

    def test_surrounding_whitespace_accepted(self):
        # 前导/尾随空白在任意切块下都必须被接受（含 EOF 前全空白）
        for doc in (b'{"stations":[]}', b' {"stations":[]} ',
                    b'\n\t{"stations":[]}\r\n  ', b'{"stations":[]}\n'):
            for size in (1, 2, 3, 7, 64):
                with tempfile.TemporaryDirectory() as wd:
                    self.assertEqual(
                        collect(doc, wd, size=size), [], (doc, size))
                    self.assertEqual(os.listdir(wd), [])

    def test_missing_stations(self):
        expect_error(self, b'{}')
        expect_error(self, b'{"meta":1}')

    def test_stations_wrong_type(self):
        expect_error(self, b'{"stations":{}}')
        expect_error(self, b'{"stations":1}')
        expect_error(self, b'{"stations":"x"}')

    def test_trailing_commas(self):
        expect_error(self, b'{"stations":[],}')
        expect_error(self, b'{"stations":[{"id":"a","enabled":true,'
                           b'"samples":[],}]}')
        expect_error(self, b'{"stations":[{"id":"a","enabled":true,'
                           b'"samples":[{"t":1,"v":2},]}]}')

    def test_missing_commas_and_colons(self):
        expect_error(self, b'{"stations"[]}')
        expect_error(self, b'{"stations":[{"id":"a""enabled":true,'
                           b'"samples":[]}]}')
        expect_error(self, b'{"stations":[{"id":"a","enabled"true,'
                           b'"samples":[]}]}')

    def test_unclosed_containers(self):
        expect_error(self, b'{"stations":[')
        expect_error(self, b'{"stations":[{"id":"a"')
        expect_error(self, b'{"stations":[{"id":"a",')
        expect_error(self, b'{"stations":[{"id":"a","samples":[')

    def test_bad_literals(self):
        expect_error(self, b'{"stations":[{"id":"a","enabled":True,'
                           b'"samples":[]}]}')
        expect_error(self, b'{"stations":[{"id":"a","enabled":null,'
                           b'"samples":[]}]}')
        expect_error(self, b'{"stations":[{"id":"a","enabled":1,'
                           b'"samples":[]}]}')
        expect_error(self, b'{"stations":[{"id":"a","enabled":tru,'
                           b'"samples":[]}]}')

    def test_bad_numbers(self):
        expect_error(self, b'{"stations":[{"id":"a","enabled":true,'
                           b'"samples":[{"t":01,"v":1}]}]}')
        expect_error(self, b'{"stations":[{"id":"a","enabled":true,'
                           b'"samples":[{"t":1.0,"v":1}]}]}')
        expect_error(self, b'{"stations":[{"id":"a","enabled":true,'
                           b'"samples":[{"t":1e3,"v":1}]}]}')
        expect_error(self, b'{"stations":[{"id":"a","enabled":true,'
                           b'"samples":[{"t":-,"v":1}]}]}')
        expect_error(self, b'{"stations":[{"id":"a","enabled":true,'
                           b'"samples":[{"t":1,"v":"x"}]}]}')
        expect_error(self, b'{"stations":[{"id":"a","enabled":true,'
                           b'"samples":[{"t":true,"v":1}]}]}')

    def test_int64_boundaries(self):
        for val in (INT64_MIN, INT64_MAX, 0, -1):
            with tempfile.TemporaryDirectory() as wd:
                data = ('{"stations":[{"id":"a","enabled":true,'
                        '"samples":[{"t":%d,"v":1}]}]}' % val).encode()
                self.assertEqual(collect(data, wd, size=1),
                                 [("a", val, 1)])
        expect_error(self, b'{"stations":[{"id":"a","enabled":true,'
                           b'"samples":[{"t":9223372036854775808,'
                           b'"v":1}]}]}')
        expect_error(self, b'{"stations":[{"id":"a","enabled":true,'
                           b'"samples":[{"t":-9223372036854775809,'
                           b'"v":1}]}]}')
        # 超长数字（逐位饱和，不会构造大整数）
        expect_error(self, b'{"stations":[{"id":"a","enabled":true,'
                           b'"samples":[{"t":' + b"9" * 80 +
                           b',"v":1}]}]}')
        # 忽略值里的大数字必须合法跳过
        with tempfile.TemporaryDirectory() as wd:
            data = b'{"stations":[{"id":"a","enabled":true,' \
                   b'"samples":[],"big":' + b"9" * 5000 + b'}]}'
            self.assertEqual(collect(data, wd, size=3), [])

    def test_bad_strings_and_escapes(self):
        expect_error(self, b'{"stations":[{"id":"a\\q","enabled":true,'
                           b'"samples":[]}]}')
        expect_error(self, b'{"stations":[{"id":"a\\u12","enabled":true,'
                           b'"samples":[]}]}')
        expect_error(self, b'{"stations":[{"id":"a\\uD800","enabled":true,'
                           b'"samples":[]}]}')           # 孤立高代理
        expect_error(self, b'{"stations":[{"id":"a\\uDC00","enabled":true,'
                           b'"samples":[]}]}')           # 孤立低代理
        expect_error(self, b'{"stations":[{"id":"a\\uD800\\u1234",'
                           b'"enabled":true,"samples":[]}]}')  # 代理不配对
        expect_error(self, b'{"stations":[{"id":"a\nb","enabled":true,'
                           b'"samples":[]}]}')           # 裸控制字符
        expect_error(self, b'{"stations":[{"id":"unterminated,'
                           b'"enabled":true,"samples":[]}]}')  # 引号未闭合

    def test_valid_surrogate_pair_emoji(self):
        with tempfile.TemporaryDirectory() as wd:
            data = '{"stations":[{"id":"\\uD83D\\uDE00","enabled":true,'\
                   '"samples":[]}]}'.encode()
            self.assertEqual(collect(data, wd, size=1), [])

    def test_duplicate_contract_fields(self):
        expect_error(self,
                     b'{"stations":[],"stations":[]}')
        expect_error(self,
                     b'{"stations":[{"id":"a","id":"b","enabled":true,'
                     b'"samples":[]}]}')
        expect_error(self,
                     b'{"stations":[{"id":"a","enabled":true,"enabled":'
                     b'false,"samples":[]}]}')
        expect_error(self,
                     b'{"stations":[{"id":"a","enabled":true,"samples":[],'
                     b'"samples":[]}]}')
        expect_error(self,
                     b'{"stations":[{"id":"a","enabled":true,'
                     b'"samples":[{"t":1,"t":2,"v":3}]}]}')
        expect_error(self,
                     b'{"stations":[{"id":"a","enabled":true,'
                     b'"samples":[{"t":1,"v":2,"v":3}]}]}')

    def test_missing_required_station_fields(self):
        expect_error(self, b'{"stations":[{}]}')
        expect_error(self, b'{"stations":[{"enabled":true,"samples":[]}]}')
        expect_error(self, b'{"stations":[{"id":"a","samples":[]}]}')
        expect_error(self, b'{"stations":[{"id":"a","enabled":true}]}')
        expect_error(self, b'{"stations":[{"id":42,"enabled":true,'
                           b'"samples":[]}]}')

    def test_missing_sample_fields(self):
        expect_error(self, b'{"stations":[{"id":"a","enabled":true,'
                           b'"samples":[{}]}]}')
        expect_error(self, b'{"stations":[{"id":"a","enabled":true,'
                           b'"samples":[{"t":1}]}]}')
        expect_error(self, b'{"stations":[{"id":"a","enabled":true,'
                           b'"samples":[{"v":1}]}]}')
        expect_error(self, b'{"stations":[{"id":"a","enabled":true,'
                           b'"samples":[1]}]}')

    def test_station_element_must_be_object(self):
        expect_error(self, b'{"stations":[1]}')
        expect_error(self, b'{"stations":["x"]}')
        expect_error(self, b'{"stations":[true]}')


class NameLengthTest(unittest.TestCase):

    def _doc_with_id(self, sid):
        return ('{"stations":[{"id":%s,"enabled":true,"samples":[]}]}'
                % sid).encode("utf-8")

    def test_id_length_limit(self):
        with tempfile.TemporaryDirectory() as wd:
            data = self._doc_with_id('"' + "a" * 64 + '"')
            self.assertEqual(collect(data, wd, size=1), [])
        expect_error(self, self._doc_with_id('"' + "a" * 65 + '"'))

    def test_key_length_limit(self):
        with tempfile.TemporaryDirectory() as wd:
            data = ('{"stations":[{"id":"a","enabled":true,"samples":[],'
                    '"%s":1}]}' % ("k" * 64)).encode()
            self.assertEqual(collect(data, wd, size=1), [])
        expect_error(self,
                     ('{"stations":[{"id":"a","enabled":true,"samples":[],'
                      '"%s":1}]}' % ("k" * 65)).encode())

    def test_ignored_giant_string_is_skipped(self):
        # 忽略字段的字符串不受 64 限制，也不得被物化
        with tempfile.TemporaryDirectory() as wd:
            data = b'{"stations":[{"id":"a","enabled":true,"samples":[],' \
                   b'"blob":"' + b"z" * (200 * 1024) + b'"}]}'
            self.assertEqual(collect(data, wd, size=4096), [])
            self.assertEqual(os.listdir(wd), [])


class NestingTest(unittest.TestCase):

    @staticmethod
    def _nested(inner_levels):
        # 根对象不计层；x 的值含 inner_levels 层嵌套数组
        return (b'{"stations":[],"x":' + b"[" * inner_levels + b"1"
                + b"]" * inner_levels + b"}")

    def test_depth_limit(self):
        with tempfile.TemporaryDirectory() as wd:
            self.assertEqual(collect(self._nested(32), wd, size=1), [])
        expect_error(self, self._nested(33))
        # 极深但结构合法：必须稳定报“深度超限”而非递归崩溃
        expect_error(self, self._nested(2000))

    def test_nested_station_path(self):
        # 深度链条（根不计）：stations 数组(1) -> 站点对象(2) -> samples(3)
        # -> 采样对象(4) -> deep 首对象(5)；28 个嵌套对象最深 32，合法，
        # 29 个则达 33，超限。
        inner = (b'"deep":' + b'{"a":' * 28 + b"1" + b"}" * 28)
        doc = (b'{"stations":[{"id":"a","enabled":true,'
               b'"samples":[{"t":1,"v":2,' + inner + b'}]}]}')
        with tempfile.TemporaryDirectory() as wd:
            self.assertEqual(collect(doc, wd, size=1), [("a", 1, 2)])
        inner_bad = (b'"deep":' + b'{"a":' * 29 + b"1" + b"}" * 29)
        doc_bad = (b'{"stations":[{"id":"a","enabled":true,'
                   b'"samples":[{"t":1,"v":2,' + inner_bad + b'}]}]}')
        expect_error(self, doc_bad)


class CleanupAndLifecycleTest(unittest.TestCase):

    def test_error_cleans_temp_files(self):
        # 在多个站点、已产生暂存文件后制造语法错误
        data = b'{"stations":['
        for i in range(5):
            data += ('{"id":"s%d","enabled":true,"samples":['
                     '{"t":1,"v":2},{"t":3,"v":4}]},' % i).encode()
        data += b'BROKEN]}'
        expect_error(self, data, size=7)

    def test_early_close_cleans_temp_files(self):
        with tempfile.TemporaryDirectory() as wd:
            data = b'{"stations":[' + b",".join(
                ('{"id":"s%d","enabled":true,"samples":['
                 '{"t":1,"v":2}]}' % i).encode()
                for i in range(20)) + b"]}"
            it = extract(iter([data]), wd)
            first = next(it)
            self.assertEqual(first, ("s0", 1, 2))
            it.close()
            it.close()  # 幂等
            self.assertEqual(os.listdir(wd), [])
            # close 后继续迭代应终止（不抛异常）
            self.assertEqual(list(it), [])

    def test_context_manager(self):
        with tempfile.TemporaryDirectory() as wd:
            data = b'{"stations":[{"id":"a","enabled":true,' \
                   b'"samples":[{"t":1,"v":2}]}]}'
            with extract(iter([data]), wd) as it:
                self.assertEqual(next(it), ("a", 1, 2))
            self.assertEqual(os.listdir(wd), [])

    def test_source_exception_propagates_unchanged(self):
        class Boom(Exception):
            pass

        def gen():
            yield b'{"stations":[{"id":"a","enabled":true,'
            yield b'"samples":[{"t":1,"v":2}]},'
            raise Boom("from source")

        with tempfile.TemporaryDirectory() as wd:
            with self.assertRaises(Boom):
                list(extract(gen(), wd))
            self.assertEqual(os.listdir(wd), [])

    def test_value_error_type_on_bad_chunk_type(self):
        with tempfile.TemporaryDirectory() as wd:
            def gen():
                yield b'{"stations":[]}'
                yield "not-bytes"  # noqa: E501  非字节块
            with self.assertRaises(TypeError):
                list(extract(gen(), wd))

    def test_bad_workdir(self):
        with self.assertRaises(ValueError):
            extract(iter([b"{}"]), "/nonexistent/portaljson/workdir")

    def test_partial_consumption_then_gc(self):
        import gc
        with tempfile.TemporaryDirectory() as wd:
            data = b'{"stations":[' + b",".join(
                ('{"id":"s%d","enabled":true,"samples":'
                 '[{"t":1,"v":2}]}' % i).encode()
                for i in range(10)) + b"]}"
            it = extract(iter([data]), wd)
            next(it)
            del it
            gc.collect()
            self.assertEqual(os.listdir(wd), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
