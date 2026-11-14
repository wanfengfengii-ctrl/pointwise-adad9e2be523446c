# -*- coding-utf-8 -*-
"""portaljson 的实现：单遍流式 JSON 解析与站点采样提取。

设计要点
========
* ``chunks`` 是单遍 UTF-8 字节块流（bytes / bytearray 均可），本模块从不
  缓存“还会再次读取”的输入：维护一个小缓冲，每块追加后只保留尚未消费的
  尾部（一个多字节字符或一个词法标记的残片）。
* 顶层结构（根对象、stations 数组、站点对象、samples 数组及采样对象）由
  状态机按已知模式驱动；其余字段的值统一走 ``skip_value``，逐字节做语法
  校验后跳过——无论字符串多大、嵌套多深都不会被物化。
* 采样 (t, v) 必须是有符号 64 位整数。数字逐位读取，用“饱和”方式判断
  溢出，避免把超长数字物化成大整数。
* 当前站点的采样先写入 workdir 中的临时文件（仅对 enabled 站点才会产出），
  站点闭合后再顺序回放；保证“整站不物化”，且单站点 32 MiB 时内存恒定。
* 正常结束、抛错、消费端提前 close，临时文件都会被清理。
"""

import os
import tempfile

__all__ = ["extract"]

# 有符号 64 位整数范围
INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1

# 约束
MAX_NAME_LEN = 64       # 字段名与 id 的最大字符数
MAX_DEPTH = 32          # 最大嵌套容器层数（根对象本身不计）
SPOOL_BYTES = 16        # 每条落盘采样：两个大端 64 位整数
SPOOL_READ_BLOCK = 1 << 20  # 回放时一次最多读 1 MiB

# JSON 结构字符
_B_QUOTE = 0x22   # "
_B_BACKSLASH = 0x5C
_B_LBRACKET = 0x5B
_B_RBRACKET = 0x5D
_B_LBRACE = 0x7B
_B_RBRACE = 0x7D
_B_COLON = 0x3A
_B_COMMA = 0x2C
_B_MINUS = 0x2D
_B_PLUS = 0x2B
_B_U = 0x75
_B_B = 0x62
_B_F = 0x66
_B_N = 0x6E
_B_R = 0x72
_B_T = 0x74
_B_0 = 0x30
_B_9 = 0x39

# 简单转义字符 -> 码点
_ESCAPES = {
    _B_QUOTE: 0x22,
    _B_BACKSLASH: 0x5C,
    _B_B: 0x08,
    _B_F: 0x0C,
    _B_N: 0x0A,
    _B_R: 0x0D,
    _B_T: 0x09,
}


def _hex_value(b):
    """单个 ASCII 十六进制数字节 -> 数值，非十六进制返回 -1。"""
    if 0x30 <= b <= 0x39:
        return b - 0x30
    if 0x41 <= b <= 0x46:
        return b - 0x41 + 10
    if 0x61 <= b <= 0x66:
        return b - 0x61 + 10
    return -1


def _is_ws(b):
    return b == 0x20 or b == 0x09 or b == 0x0A or b == 0x0D


def _is_digit(b):
    return 0x30 <= b <= 0x39


def _is_digit1(b):
    return 0x31 <= b <= 0x39


class _Fallback(Exception):
    """快速路径回退信号：恢复位置后改走通用解析。"""


class Feeder(object):
    """单遍字节投喂器。

    内部仅保留一个小 ``bytearray``：每追加一块，把已消费部分压缩掉。
    因此即便单个 JSON 字符串/站点达 32 MiB，缓冲里也只会有“当前正在
    读取的一个词法单元残片”外加少量结构字节。
    """

    __slots__ = ("_chunks", "_buf", "_pos", "_eof", "_anchor")

    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self._buf = bytearray()
        self._pos = 0
        self._eof = False
        # 锚点（绝对字节位置）：压缩时保留锚点之后的数据，用于快速路径回退
        self._anchor = None

    def set_anchor_here(self):
        """在当前位置打锚点，返回该锚点的绝对位置。"""
        self._anchor = self._pos
        return self._pos

    def clear_anchor(self):
        self._anchor = None

    def _compact_point(self):
        """压缩时允许删除的最大前缀位置（须保留锚点之后的数据）。"""
        if self._anchor is not None and self._anchor < self._pos:
            return self._anchor
        return self._pos

    def _refill(self):
        """追加下一块并压缩已消费前缀；返回是否拿到新数据。"""
        if self._eof:
            return False
        for chunk in self._chunks:
            if not isinstance(chunk, (bytes, bytearray)):
                # 不是规范约定的字节块：TypeError 原样传播
                raise TypeError(
                    "chunks 必须产出 bytes/bytearray，实际为 %s"
                    % type(chunk).__name__
                )
            cut = self._compact_point()
            if cut:
                del self._buf[:cut]
                self._pos -= cut
                if self._anchor is not None:
                    self._anchor -= cut
            self._buf.extend(chunk)
            return True
        self._eof = True
        cut = self._compact_point()
        if cut:
            del self._buf[:cut]
            self._pos -= cut
            if self._anchor is not None:
                self._anchor -= cut
        return False

    def ensure(self, n):
        """保证至少有 n 个未消费字节可读，否则抛 ValueError（截断）。"""
        while len(self._buf) - self._pos < n:
            if not self._refill():
                raise ValueError("JSON 不完整：输入在值结束前被截断")
        return

    def peek(self):
        """查看当前字节但不消费；无字节时抛 ValueError。"""
        if self._pos >= len(self._buf):
            self.ensure(1)
        return self._buf[self._pos]

    def maybe_peek(self):
        """查看当前字节但不消费；流结束返回 -1。"""
        if self._pos >= len(self._buf):
            if not self._refill():
                return -1
        return self._buf[self._pos]

    def take(self):
        """消费并返回当前字节。"""
        b = self._buf[self._pos]
        self._pos += 1
        return b

    def index_of(self, targets):
        """返回 targets（单个 int 或 int 元组）相对当前位置的下标。

        跨块循环查找；未找到（流结束）返回 -1。缓冲只保留词法单元残片，
        因此扫描成本与“当前 token 残片大小”相关，不会物化整值。
        """
        buf = self._buf
        if isinstance(targets, int):
            while True:
                idx = buf.find(targets, self._pos)
                if idx >= 0:
                    return idx - self._pos
                if self._eof or not self._refill():
                    return -1
                buf = self._buf
        while True:
            pos = self._pos
            end = len(buf)
            i = pos
            while i < end:
                if buf[i] in targets:
                    return i - pos
                i += 1
            if self._eof or not self._refill():
                return -1
            buf = self._buf

    def index_not_ws(self):
        """返回下一个非空白字节相对当前位置的下标；流结束返回 -1。"""
        while True:
            buf = self._buf
            pos = self._pos
            end = len(buf)
            i = pos
            while i < end:
                b = buf[i]
                if b != 0x20 and b != 0x09 and b != 0x0A and b != 0x0D:
                    return i - pos
                i += 1
            if self._eof or not self._refill():
                return -1

    #: 数字 token 的合法后继字节（结构符、空白、小数点、指数、负号）
    _NUMBER_END = (0x20, 0x09, 0x0A, 0x0D, _B_COMMA, _B_RBRACKET,
                   _B_RBRACE, 0x2E, 0x65, 0x45, _B_MINUS)

    #: 字符串内需要进入慢路径的字节（0x00-0x1F 控制符、引号、反斜杠）
    #: 用 translate 表把这些字节映为 1，其余映为 0，交给 C 层 find。
    _STRING_SPECIAL_TABLE = bytes(
        1 if (i < 0x20 or i == _B_QUOTE or i == _B_BACKSLASH) else 0
        for i in range(256)
    )

    def string_plain_span(self):
        """消费字符串中的连续普通字符，返回 (累计字节数, 末段切片)。

        普通段在 C 层用 ``translate``+``find`` 跳过，跨块循环；流结束
        （未闭合字符串）返回 (-1, b"")。逐段处理当前缓冲（约一个块），
        因此巨型忽略字符串也不会被物化。结束时 ``_pos`` 停在引号/反斜杠/
        控制字符上（该字节不消费）；末段切片仅覆盖最后一个缓冲内的普通
        字节，物化路径据此安全解码（键/id 很短，必在单缓冲内）。
        """
        table = self._STRING_SPECIAL_TABLE
        consumed = 0
        last_seg = b""
        while True:
            buf = self._buf
            pos = self._pos
            mapped = bytes(buf[pos:]).translate(table)
            idx = mapped.find(1)
            if idx >= 0:
                last_seg = bytes(buf[pos:pos + idx])
                self._pos = pos + idx
                return consumed + idx, last_seg
            # 本缓冲剩余全是普通字符：消费到末尾后补块，再从新块继续
            consumed += len(buf) - pos
            self._pos = len(buf)
            if self._eof or not self._refill():
                return -1, b""

    def number_token(self):
        """消费一个数字 token 并返回其字节切片（跨块、仅保留残片）。"""
        offset = self.index_of(self._NUMBER_END)
        if offset < 0:
            raise ValueError("JSON 不完整：数字后缺少结构收尾")
        start = self._pos
        end = start + offset
        token = bytes(self._buf[start:end])
        self._pos = end
        return token

    def read_codepoint(self):
        """读取一个完整 UTF-8 字符，返回 (码点, 字节数)。

        任何非法编码（含非法首字节、缺失续字节、过长编码、代理码点、
        超过 U+10FFFF）都抛 ValueError；截断同样抛 ValueError。
        """
        self.ensure(1)
        b0 = self._buf[self._pos]
        if b0 < 0x80:
            self._pos += 1
            return b0, 1
        if b0 < 0xC2:
            # 10xxxxxx 续字节出现在首字节位置，或 C0/C1 过长编码
            raise ValueError("非法 UTF-8 序列：首字节 0x%02x" % b0)
        if b0 < 0xE0:
            need = 2
            cp = b0 & 0x1F
        elif b0 < 0xF0:
            need = 3
            cp = b0 & 0x0F
        elif b0 < 0xF5:
            need = 4
            cp = b0 & 0x07
        else:
            raise ValueError("非法 UTF-8 序列：首字节 0x%02x" % b0)
        self.ensure(need)
        buf = self._buf
        pos = self._pos
        for i in range(1, need):
            b = buf[pos + i]
            if b < 0x80 or b > 0xBF:
                raise ValueError("非法 UTF-8 序列：缺少续字节")
            cp = (cp << 6) | (b & 0x3F)
        # 过长编码检查
        if need == 3 and cp < 0x800:
            raise ValueError("非法 UTF-8 序列：过长编码")
        if need == 4 and cp < 0x10000:
            raise ValueError("非法 UTF-8 序列：过长编码")
        # UTF-16 代理区与码点上界
        if 0xD800 <= cp <= 0xDFFF or cp > 0x10FFFF:
            raise ValueError("非法 UTF-8 序列：码点 U+%04X" % cp)
        self._pos += need
        return cp, need


class Parser(object):
    """字节级 JSON 语法分析器（只做语法校验与结构化提取，不建通用值树）。"""

    __slots__ = ("f", "depth")

    def __init__(self, feeder):
        self.f = feeder
        self.depth = 0

    # ------------------------------------------------------------------ #
    # 空白与结构辅助
    # ------------------------------------------------------------------ #
    def skip_ws(self):
        offset = self.f.index_not_ws()
        if offset > 0:
            self.f._pos += offset
        elif offset < 0:
            # 直到流结束都是空白：把尾部空白全部消费掉
            self.f._pos = len(self.f._buf)

    def expect(self, b):
        self.skip_ws()
        f = self.f
        f.ensure(1)
        if f.peek() != b:
            raise ValueError(
                "JSON 语法错误：期望 0x%02x，实际 0x%02x" % (b, f.peek())
            )
        f.take()

    def enter(self):
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ValueError("嵌套深度超过 %d 层" % MAX_DEPTH)

    def leave(self):
        self.depth -= 1

    # ------------------------------------------------------------------ #
    # 字符串
    # ------------------------------------------------------------------ #
    def scan_string(self, materialize, max_len=MAX_NAME_LEN):
        """扫描一个 JSON 字符串。

        materialize=True 时返回解码后的 ``str``（字符数超过 max_len 抛
        ValueError）；False 时只做完整语法校验并返回 None，可安全处理
        任意长度的忽略字符串。块边界可能切开字符、转义或 \\uXXXX。

        热路径用 ``find`` 一次跳过“普通字符段”，仅在引号、反斜杠、控制
        字符处进入逐字符慢路径；忽略字符串因此只有少量 Python 级调用。
        """
        f = self.f
        f.ensure(1)
        if f.peek() != _B_QUOTE:
            raise ValueError("JSON 语法错误：字符串必须以双引号开始")
        f.take()
        chars = [] if materialize else None
        length = 0
        if not materialize:
            # 非物化路径（巨型忽略字符串）：C 层 translate+find 快速跳过，
            # 仅在引号、反斜杠、控制字符处进入逐字节慢路径。
            while True:
                if f.string_plain_span()[0] < 0:
                    raise ValueError("JSON 不完整：字符串缺少闭合引号")
                cp, _ = f.read_codepoint()
                if cp == _B_QUOTE:
                    return None
                if cp < 0x20:
                    raise ValueError(
                        "JSON 语法错误：字符串中存在未转义控制字符")
                if cp == _B_BACKSLASH:
                    self._scan_escape(f)
            # 不可达
        # 物化路径（键/id，最多 64 字符）：逐码点读取，天然支持任意切块
        while True:
            cp, _ = f.read_codepoint()
            if cp == _B_QUOTE:
                return "".join(chars)
            if cp < 0x20:
                raise ValueError("JSON 语法错误：字符串中存在未转义控制字符")
            if cp == _B_BACKSLASH:
                cp = self._scan_escape(f)
            length += 1
            if length > max_len:
                raise ValueError("字段名或 id 超过 %d 字符上限" % max_len)
            chars.append(chr(cp))

    def _scan_escape(self, f):
        """处理反斜杠之后的转义，返回转义后的码点（代理对已合并）。"""
        f.ensure(1)
        e = f.take()
        simple = _ESCAPES.get(e)
        if simple is not None:
            return simple
        if e != _B_U:
            raise ValueError("JSON 语法错误：非法转义 \\%s" % chr(e))
        cp = self._read_hex4()
        if 0xD800 <= cp <= 0xDBFF:
            # 高代理，后面必须紧跟 \uXXXX 低代理
            f.ensure(2)
            if f.peek() != _B_BACKSLASH:
                raise ValueError("JSON 语法错误：高代理缺少转义延续")
            f.take()
            if f.peek() != _B_U:
                raise ValueError("JSON 语法错误：高代理后应为 \\u")
            f.take()
            lo = self._read_hex4()
            if not (0xDC00 <= lo <= 0xDFFF):
                raise ValueError("JSON 语法错误：代理对不匹配")
            return 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)
        if 0xDC00 <= cp <= 0xDFFF:
            raise ValueError("JSON 语法错误：出现未配对的低代理")
        return cp

    def _read_hex4(self):
        f = self.f
        f.ensure(4)
        value = 0
        for _ in range(4):
            d = _hex_value(f.take())
            if d < 0:
                raise ValueError("JSON 语法错误：\\u 后需要 4 位十六进制数")
            value = (value << 4) | d
        return value

    # ------------------------------------------------------------------ #
    # 数字
    # ------------------------------------------------------------------ #
    def skip_number(self):
        """按 JSON 数字语法跳过一个数字，不构造任何数值对象。

        先定位 token 边界（跨块批量扫描，缓冲只留残片），再对 token 做
        完整正则级语法校验。
        """
        f = self.f
        f.ensure(1)
        offset = f.index_of((0x20, 0x09, 0x0A, 0x0D, _B_COMMA, _B_RBRACKET,
                             _B_RBRACE))
        if offset < 0:
            raise ValueError("JSON 不完整：数字后缺少结构收尾")
        start = f._pos
        end = start + offset
        token = bytes(f._buf[start:end])
        f._pos = end
        self._check_number_grammar(token)

    @staticmethod
    def _check_number_grammar(s):
        i = 0
        n = len(s)
        if n == 0:
            raise ValueError("JSON 语法错误：数字缺少整数部分")
        if s[i] == _B_MINUS:
            i += 1
            if i == n:
                raise ValueError("JSON 语法错误：数字缺少整数部分")
        # 整数部分
        if s[i] == _B_0:
            i += 1
        elif _is_digit1(s[i]):
            i += 1
            while i < n and _is_digit(s[i]):
                i += 1
        else:
            raise ValueError("JSON 语法错误：数字缺少整数部分")
        # 小数部分
        if i < n and s[i] == 0x2E:
            i += 1
            if i >= n or not _is_digit(s[i]):
                raise ValueError("JSON 语法错误：小数点后至少需要一位数字")
            while i < n and _is_digit(s[i]):
                i += 1
        # 指数部分
        if i < n and (s[i] == 0x65 or s[i] == 0x45):
            i += 1
            if i < n and (s[i] == _B_MINUS or s[i] == _B_PLUS):
                i += 1
            if i >= n or not _is_digit(s[i]):
                raise ValueError("JSON 语法错误：指数部分至少需要一位数字")
            while i < n and _is_digit(s[i]):
                i += 1
        if i != n:
            raise ValueError("JSON 语法错误：非法数字字面量")

    def read_int64(self):
        """读取 t/v：必须是落在有符号 64 位范围内的整数字面量。

        用 ``number_token`` 一次定位 token 边界，再整体解析；超长数字走
        逐位饱和路径，不会形成大整数对象。小数、指数、越界一律 ValueError。
        """
        f = self.f
        f.ensure(1)
        negative = False
        if f.peek() == _B_MINUS:
            negative = True
            f.take()
            f.ensure(1)
        if not _is_digit(f.peek()):
            raise ValueError("t/v 必须是 64 位有符号整数")
        token = f.number_token()
        # 小数或指数 -> 非整数（token 在 . / e / E 前结束）
        nxt = f.peek()
        if nxt == 0x2E or nxt == 0x65 or nxt == 0x45:
            raise ValueError("t/v 必须是整数，不允许小数或指数")
        return self._int_from_token(token, negative)

    @staticmethod
    def _int_from_token(token, negative):
        """对已切出的纯数字字节做 int64 校验与转换。

        token 至多 19 位（下面先判长度），用 C 实现的 ``int(bytes, 10)``
        转换；超过 19 位走逐位饱和，绝不构造超大整数对象。
        """
        n_digits = len(token)
        if n_digits == 1 and token[0] == _B_0:
            return 0
        if token[0] == _B_0:
            raise ValueError("JSON 语法错误：数字不允许前导零")
        if n_digits <= 19:
            value = int(token, 10)
            limit = INT64_MAX + 1 if negative else INT64_MAX
            if value > limit:
                raise ValueError("t/v 超出有符号 64 位整数范围")
            return -value if negative else value
        value = 0
        overflow = False
        for d in token:
            if not overflow:
                if value > INT64_MAX // 10:
                    overflow = True
                else:
                    value = value * 10 + (d - 0x30)
                    if value > INT64_MAX + (1 if negative else 0):
                        overflow = True
        if overflow:
            raise ValueError("t/v 超出有符号 64 位整数范围")
        return -value if negative else value

    def try_fast_sample(self):
        """采样对象热路径：仅当对象恰好含 t、v 两个整数字段时快速解析。

        全程直接操作 Feeder 的 ``_buf``/``_pos``（局部变量缓存），键按
        单字节 ``"t"``/``"v"`` 识别，不物化字符串。遇到额外字段抛
        :class:`_Fallback`（恢复到对象起始 ``{`` 改走通用路径）；其余
        确定性语法错误直接抛 ValueError。
        """
        f = self.f
        # ---- 内联 skip_ws + 起始 '{' 判定 ----
        buf = f._buf
        pos = f._pos
        while True:
            while pos < len(buf):
                b = buf[pos]
                if b != 0x20 and b != 0x09 and b != 0x0A and b != 0x0D:
                    break
                pos += 1
            f._pos = pos
            if pos < len(buf):
                break
            if f._eof or not f._refill():
                raise _Fallback()
            buf = f._buf
            pos = f._pos
        if buf[pos] != _B_LBRACE:
            raise _Fallback()
        f.set_anchor_here()       # 锚定 '{'，压缩也不丢失
        pos += 1
        f._pos = pos
        self.enter()
        number_end = f._NUMBER_END
        try:
            t_value = 0
            v_value = 0
            seen = 0
            T_BIT, V_BIT = 1, 2
            while True:
                # ---- 内联 skip_ws ----
                while True:
                    while pos < len(buf):
                        b = buf[pos]
                        if b != 0x20 and b != 0x09 and b != 0x0A and b != 0x0D:
                            break
                        pos += 1
                    f._pos = pos
                    if pos < len(buf):
                        break
                    if not f._refill():
                        raise ValueError(
                            "JSON 不完整：采样对象被截断")
                    buf = f._buf
                    pos = f._pos
                if buf[pos] != _B_QUOTE:
                    raise ValueError("JSON 语法错误：采样字段名必须是字符串")
                pos += 1
                # 键名：快速识别单字节 "t" / "v"
                f._pos = pos
                f.ensure(2)
                buf = f._buf
                pos = f._pos
                if buf[pos + 1] == _B_QUOTE:
                    k0 = buf[pos]
                    pos += 2
                    if k0 == 0x74:
                        key = 1          # t
                    elif k0 == 0x76:
                        key = 2          # v
                    else:
                        raise _Fallback()
                else:
                    f._pos = pos - 1
                    key_name = self.scan_string(materialize=True)
                    if key_name == "t":
                        key = 1
                    elif key_name == "v":
                        key = 2
                    else:
                        raise _Fallback()
                    buf = f._buf
                    pos = f._pos
                # ---- 冒号（含空白）----
                while True:
                    while pos < len(buf):
                        b = buf[pos]
                        if b != 0x20 and b != 0x09 and b != 0x0A and b != 0x0D:
                            break
                        pos += 1
                    f._pos = pos
                    if pos < len(buf):
                        break
                    if not f._refill():
                        raise ValueError("JSON 不完整：缺少 ':'")
                    buf = f._buf
                    pos = f._pos
                if buf[pos] != _B_COLON:
                    raise ValueError("JSON 语法错误：键后应为 ':'")
                pos += 1
                # ---- 值前空白 ----
                while True:
                    while pos < len(buf):
                        b = buf[pos]
                        if b != 0x20 and b != 0x09 and b != 0x0A and b != 0x0D:
                            break
                        pos += 1
                    f._pos = pos
                    if pos < len(buf):
                        break
                    if not f._refill():
                        raise ValueError("JSON 不完整：缺少采样值")
                    buf = f._buf
                    pos = f._pos
                head = buf[pos]
                if head != _B_MINUS and not _is_digit(head):
                    raise _Fallback()
                neg = head == _B_MINUS
                if neg:
                    pos += 1
                    f._pos = pos
                    f.ensure(1)
                    buf = f._buf
                    pos = f._pos
                    if not _is_digit(buf[pos]):
                        raise ValueError("t/v 必须是 64 位有符号整数")
                # ---- 定位数字 token ----
                f._pos = pos
                offset = f.index_of(number_end)
                if offset < 0:
                    raise ValueError("JSON 不完整：数字后缺少结构收尾")
                start = f._pos
                token = bytes(f._buf[start:start + offset])
                pos = start + offset
                f._pos = pos
                if pos >= len(buf):
                    f.ensure(1)
                    buf = f._buf
                    pos = f._pos
                nxt = buf[pos]
                if nxt == 0x2E or nxt == 0x65 or nxt == 0x45:
                    raise ValueError("t/v 必须是整数，不允许小数或指数")
                ivalue = self._int_from_token(token, neg)
                if key == 1:
                    if seen & T_BIT:
                        raise ValueError("采样字段 t 重复")
                    seen |= T_BIT
                    t_value = ivalue
                else:
                    if seen & V_BIT:
                        raise ValueError("采样字段 v 重复")
                    seen |= V_BIT
                    v_value = ivalue
                # ---- 值后空白 + , / } ----
                while True:
                    while pos < len(buf):
                        b = buf[pos]
                        if b != 0x20 and b != 0x09 and b != 0x0A and b != 0x0D:
                            break
                        pos += 1
                    f._pos = pos
                    if pos < len(buf):
                        break
                    if not f._refill():
                        raise ValueError("JSON 不完整：采样对象未闭合")
                    buf = f._buf
                    pos = f._pos
                b = buf[pos]
                pos += 1
                f._pos = pos
                if b == _B_RBRACE:
                    self.leave()
                    break
                if b != _B_COMMA:
                    raise ValueError(
                        "JSON 语法错误：采样字段后应为 ',' 或 '}'")
                # 逗号后空白，并禁止尾随逗号
                while True:
                    while pos < len(buf):
                        b = buf[pos]
                        if b != 0x20 and b != 0x09 and b != 0x0A and b != 0x0D:
                            break
                        pos += 1
                    f._pos = pos
                    if pos < len(buf):
                        break
                    if not f._refill():
                        raise ValueError("JSON 不完整：对象被截断")
                    buf = f._buf
                    pos = f._pos
                if buf[pos] == _B_RBRACE:
                    raise ValueError("JSON 语法错误：对象不允许尾随逗号")
            if seen != (T_BIT | V_BIT):
                raise ValueError("采样对象必须同时包含 t 与 v")
            f.clear_anchor()
            return t_value, v_value
        except _Fallback:
            # 恢复到锚点（对象起始 '{'），交由通用路径完整重解析
            self.leave()
            f._pos = f._anchor
            f.clear_anchor()
            raise

    # ------------------------------------------------------------------ #
    # 字面量
    # ------------------------------------------------------------------ #
    def scan_literal(self):
        """跳过 true / false / null，并返回其文本（用于 bool 判定）。"""
        f = self.f
        f.ensure(1)
        b = f.take()
        if b == _B_T:
            for ch in (0x72, 0x75, 0x65):  # rue
                f.ensure(1)
                if f.take() != ch:
                    raise ValueError("JSON 语法错误：非法字面量")
            return "true"
        if b == _B_F:
            for ch in (0x61, 0x6C, 0x73, 0x65):  # alse
                f.ensure(1)
                if f.take() != ch:
                    raise ValueError("JSON 语法错误：非法字面量")
            return "false"
        if b == _B_N:
            for ch in (0x75, 0x6C, 0x6C):  # ull
                f.ensure(1)
                if f.take() != ch:
                    raise ValueError("JSON 语法错误：非法字面量")
            return "null"
        raise ValueError("JSON 语法错误：无法识别的字面量 0x%02x" % b)

    # ------------------------------------------------------------------ #
    # 忽略值：只校验语法、绝不物化
    # ------------------------------------------------------------------ #
    def skip_value(self):
        """跳过任意 JSON 值并完整校验语法（字符串/数组/对象可任意大）。"""
        self.skip_ws()
        f = self.f
        f.ensure(1)
        b = f.peek()
        if b == _B_QUOTE:
            self.scan_string(materialize=False)
            return
        if b == _B_LBRACE:
            self.enter()
            f.take()
            self.skip_ws()
            if f.maybe_peek() == _B_RBRACE:
                f.take()
                self.leave()
                return
            while True:
                self.skip_ws()
                self.scan_string(materialize=False)
                self.expect(_B_COLON)
                self.skip_value()
                self.skip_ws()
                f.ensure(1)
                b = f.take()
                if b == _B_RBRACE:
                    self.leave()
                    return
                if b != _B_COMMA:
                    raise ValueError("JSON 语法错误：对象成员后应为 ',' 或 '}'")
                # 逗号后禁止直接是 }（尾随逗号）
                self.skip_ws()
                f.ensure(1)
                if f.peek() == _B_RBRACE:
                    raise ValueError("JSON 语法错误：对象不允许尾随逗号")
        if b == _B_LBRACKET:
            self.enter()
            f.take()
            self.skip_ws()
            if f.maybe_peek() == _B_RBRACKET:
                f.take()
                self.leave()
                return
            while True:
                self.skip_value()
                self.skip_ws()
                f.ensure(1)
                b = f.take()
                if b == _B_RBRACKET:
                    self.leave()
                    return
                if b != _B_COMMA:
                    raise ValueError("JSON 语法错误：数组元素后应为 ',' 或 ']'")
                self.skip_ws()
                f.ensure(1)
                if f.peek() == _B_RBRACKET:
                    raise ValueError("JSON 语法错误：数组不允许尾随逗号")
        if b == _B_MINUS or _is_digit(b):
            self.skip_number()
            return
        # true / false / null
        self.scan_literal()


class _ExtractIterator(object):
    """extract() 返回的可关闭迭代器（同时支持上下文管理器协议）。"""

    __slots__ = ("_gen", "_spool_path", "_spool", "_closed")

    def __init__(self, chunks, workdir):
        # 在启动生成器之前校验 workdir，错误尽量早且确定
        if not os.path.isdir(workdir):
            raise ValueError("workdir 不是已存在的目录: %r" % (workdir,))
        self._spool_path = None
        self._spool = None
        self._closed = False
        parser = Parser(Feeder(chunks))
        self._gen = self._run(parser, workdir)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._gen)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        """提前终止消费并清理本迭代器自建的全部临时文件（幂等）。"""
        if self._closed:
            return
        self._closed = True
        # 关闭生成器会触发其 finally，进而调用 _discard_spool；
        # 这里再兜底一次，覆盖生成器尚未启动/已结束等状态。
        try:
            self._gen.close()
        finally:
            self._discard_spool()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _discard_spool(self):
        spool = self._spool
        if spool is not None:
            self._spool = None
            try:
                spool.close()
            except OSError:
                pass
        path = self._spool_path
        if path is not None:
            self._spool_path = None
            try:
                os.unlink(path)
            except OSError:
                pass

    def _open_spool(self, workdir):
        fd, path = tempfile.mkstemp(prefix="portaljson-", suffix=".spool",
                                    dir=workdir)
        self._spool_path = path
        self._spool = os.fdopen(fd, "wb", buffering=1 << 16)
        return self._spool

    # ------------------------------------------------------------------ #
    # 结构化解析主流程
    # ------------------------------------------------------------------ #
    def _run(self, p, workdir):
        # 任何退出路径（正常耗尽、ValueError、源异常、GeneratorExit）
        # 都会经过 finally，保证临时文件被清理。
        try:
            f = p.f
            p.skip_ws()
            f.ensure(1)
            if f.peek() != _B_LBRACE:
                raise ValueError("JSON 顶层必须是对象")
            f.take()
            # 根对象本身不计入嵌套层数；enter() 统计的是内部容器层数
            p.skip_ws()
            f.ensure(1)
            if f.peek() == _B_RBRACE:
                raise ValueError("顶层对象缺少必需的 stations 字段")
            stations_found = False
            while True:
                p.skip_ws()
                key = p.scan_string(materialize=True)
                p.expect(_B_COLON)
                if key == "stations":
                    if stations_found:
                        raise ValueError("顶层 stations 字段重复")
                    stations_found = True
                    p.skip_ws()
                    for item in self._parse_stations(p, workdir):
                        yield item
                else:
                    # 其余顶层字段：校验语法后跳过（值可能巨大）
                    p.skip_value()
                p.skip_ws()
                f.ensure(1)
                b = f.take()
                if b == _B_RBRACE:
                    break
                if b != _B_COMMA:
                    raise ValueError("JSON 语法错误：顶层成员后应为 ',' 或 '}'")
                p.skip_ws()
                f.ensure(1)
                if f.peek() == _B_RBRACE:
                    raise ValueError("JSON 语法错误：对象不允许尾随逗号")
            if not stations_found:
                raise ValueError("顶层对象缺少必需的 stations 字段")
            # 根对象之后只允许空白
            p.skip_ws()
            if f.maybe_peek() != -1:
                raise ValueError("JSON 语法错误：根对象之后存在多余数据")
        finally:
            self._discard_spool()

    def _parse_stations(self, p, workdir):
        f = p.f
        f.ensure(1)
        if f.peek() != _B_LBRACKET:
            raise ValueError("stations 必须是数组")
        f.take()
        p.enter()
        p.skip_ws()
        if f.maybe_peek() == _B_RBRACKET:
            f.take()
            p.leave()
            return
        while True:
            p.skip_ws()
            for item in self._parse_station(p, workdir):
                yield item
            p.skip_ws()
            f.ensure(1)
            b = f.take()
            if b == _B_RBRACKET:
                p.leave()
                return
            if b != _B_COMMA:
                raise ValueError("JSON 语法错误：stations 元素后应为 ',' 或 ']'")
            p.skip_ws()
            f.ensure(1)
            if f.peek() == _B_RBRACKET:
                raise ValueError("JSON 语法错误：数组不允许尾随逗号")

    def _parse_station(self, p, workdir):
        f = p.f
        f.ensure(1)
        if f.peek() != _B_LBRACE:
            raise ValueError("stations 的每个元素必须是对象")
        f.take()
        p.enter()

        station_id = None
        enabled = None
        samples_seen = False
        # 仅追踪契约内字段的重名；忽略字段不建集合，避免字段极多时膨胀
        seen = 0  # 位掩码：1=id 2=enabled 4=samples
        ID_BIT, EN_BIT, SA_BIT = 1, 2, 4

        p.skip_ws()
        f.ensure(1)
        if f.peek() == _B_RBRACE:
            raise ValueError("站点对象缺少 id / enabled / samples 字段")
        while True:
            p.skip_ws()
            key = p.scan_string(materialize=True)
            p.expect(_B_COLON)
            if key == "id":
                if seen & ID_BIT:
                    raise ValueError("站点字段 id 重复")
                seen |= ID_BIT
                p.skip_ws()
                f.ensure(1)
                if f.peek() != _B_QUOTE:
                    raise ValueError("站点 id 必须是字符串")
                station_id = p.scan_string(materialize=True)
            elif key == "enabled":
                if seen & EN_BIT:
                    raise ValueError("站点字段 enabled 重复")
                seen |= EN_BIT
                p.skip_ws()
                f.ensure(1)
                b = f.peek()
                if b == _B_T:
                    if p.scan_literal() != "true":
                        raise ValueError("enabled 必须是布尔值")
                    enabled = True
                elif b == _B_F:
                    p.scan_literal()
                    enabled = False
                else:
                    raise ValueError("enabled 必须是布尔值")
            elif key == "samples":
                if seen & SA_BIT:
                    raise ValueError("站点字段 samples 重复")
                seen |= SA_BIT
                samples_seen = True
                p.skip_ws()
                for t_value, v_value in self._parse_samples(p):
                    # 延迟建临时文件：没有采样就不产生任何文件
                    if self._spool is None:
                        self._open_spool(workdir)
                    self._spool.write(
                        t_value.to_bytes(8, "big", signed=True)
                        + v_value.to_bytes(8, "big", signed=True)
                    )
            else:
                # 其余字段：语法校验后整体跳过（可含巨型字符串/深层嵌套）
                p.skip_value()

            p.skip_ws()
            f.ensure(1)
            b = f.take()
            if b == _B_RBRACE:
                p.leave()
                break
            if b != _B_COMMA:
                raise ValueError("JSON 语法错误：站点字段后应为 ',' 或 '}'")
            p.skip_ws()
            f.ensure(1)
            if f.peek() == _B_RBRACE:
                raise ValueError("JSON 语法错误：对象不允许尾随逗号")

        if station_id is None:
            raise ValueError("站点缺少必需的字符串字段 id")
        if enabled is None:
            raise ValueError("站点缺少必需的布尔字段 enabled")
        if not samples_seen:
            raise ValueError("站点缺少必需的 samples 字段")

        if enabled:
            spool = self._spool
            if spool is not None:
                spool.flush()
                os.fsync(spool.fileno())
                path = self._spool_path
                # 先按源序回放，再删除文件；回放期间不缓存整站结果
                with open(path, "rb", buffering=SPOOL_READ_BLOCK) as rf:
                    while True:
                        block = rf.read(SPOOL_READ_BLOCK)
                        if not block:
                            break
                        if len(block) % SPOOL_BYTES:
                            raise ValueError("采样暂存文件损坏")
                        for off in range(0, len(block), SPOOL_BYTES):
                            t_value = int.from_bytes(
                                block[off:off + 8], "big", signed=True
                            )
                            v_value = int.from_bytes(
                                block[off + 8:off + SPOOL_BYTES],
                                "big", signed=True,
                            )
                            yield (station_id, t_value, v_value)
        # 站点闭合即清理本站临时文件
        self._discard_spool()

    def _parse_samples(self, p):
        f = p.f
        f.ensure(1)
        if f.peek() != _B_LBRACKET:
            raise ValueError("samples 必须是数组")
        f.take()
        p.enter()
        p.skip_ws()
        if f.maybe_peek() == _B_RBRACKET:
            f.take()
            p.leave()
            return
        while True:
            # 热路径：仅含 t/v 两个整数字段的采样对象走快速内联解析；
            # 含额外字段则回退通用路径；非对象起始由通用路径报错。
            try:
                fast = p.try_fast_sample()
            except _Fallback:
                t_value, v_value = self._parse_sample_generic(p)
            else:
                t_value, v_value = fast
            yield (t_value, v_value)

            p.skip_ws()
            f.ensure(1)
            b = f.take()
            if b == _B_RBRACKET:
                p.leave()
                return
            if b != _B_COMMA:
                raise ValueError("JSON 语法错误：samples 元素后应为 ',' 或 ']'")
            p.skip_ws()
            f.ensure(1)
            if f.peek() == _B_RBRACKET:
                raise ValueError("JSON 语法错误：数组不允许尾随逗号")

    def _parse_sample_generic(self, p):
        """通用采样对象解析（支持额外字段、任意字段顺序、重复检测）。"""
        f = p.f
        p.skip_ws()
        f.ensure(1)
        if f.peek() != _B_LBRACE:
            raise ValueError("samples 的每个元素必须是对象")
        f.take()
        p.enter()
        t_value = None
        v_value = None
        sample_seen = 0  # 1=t 2=v
        T_BIT, V_BIT = 1, 2
        p.skip_ws()
        f.ensure(1)
        if f.peek() == _B_RBRACE:
            raise ValueError("采样对象缺少 t / v 字段")
        while True:
            p.skip_ws()
            key = p.scan_string(materialize=True)
            p.expect(_B_COLON)
            if key == "t":
                if sample_seen & T_BIT:
                    raise ValueError("采样字段 t 重复")
                sample_seen |= T_BIT
                p.skip_ws()
                t_value = p.read_int64()
            elif key == "v":
                if sample_seen & V_BIT:
                    raise ValueError("采样字段 v 重复")
                sample_seen |= V_BIT
                p.skip_ws()
                v_value = p.read_int64()
            else:
                p.skip_value()
            p.skip_ws()
            f.ensure(1)
            b = f.take()
            if b == _B_RBRACE:
                p.leave()
                break
            if b != _B_COMMA:
                raise ValueError("JSON 语法错误：采样字段后应为 ',' 或 '}'")
            p.skip_ws()
            f.ensure(1)
            if f.peek() == _B_RBRACE:
                raise ValueError("JSON 语法错误：对象不允许尾随逗号")
        if t_value is None or v_value is None:
            raise ValueError("采样对象必须同时包含 t 与 v")
        return t_value, v_value


def extract(chunks, workdir):
    """从单遍 UTF-8 字节块流中提取启用站点的采样。

    :param chunks: 可迭代对象，逐项产出 ``bytes``/``bytearray`` 块；
                   块可以在任意位置切开 UTF-8 字符、转义或 JSON 标记。
    :param workdir: 已存在的目录，用于存放当前站点采样的临时文件。
    :returns: 可关闭迭代器（支持 ``with`` 与 ``close()``），按源序产出
              ``(id, t, v)`` 三元组；``enabled=false`` 的站点不产出。
    :raises ValueError: 输入截断、非法 UTF-8 或任何 JSON 语法/契约错误。
    :raises Exception: ``chunks`` 自身抛出的异常原样传播。
    """
    return _ExtractIterator(chunks, workdir)
