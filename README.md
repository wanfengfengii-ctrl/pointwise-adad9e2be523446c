# portaljson — 低内存单遍流式 JSON 站点采样提取

仅依赖 **Python 标准库**，从单遍 UTF-8 字节块流中提取启用站点的采样。

## 接口

```python
from portaljson import extract

it = extract(chunks, workdir)   # 可关闭迭代器，也支持 with
for sid, t, v in it:            # 按源序产出 (id, t, v)
    ...
it.close()                      # 提前终止；清理自建临时文件（幂等）
```

- `chunks`：逐项产出 `bytes`/`bytearray` 的可迭代对象。块可以在**任意位置**
  切开 UTF-8 多字节字符、转义序列或 JSON 标记。
- `workdir`：**已存在**的目录，用于存放当前站点采样的临时文件。
- 产出：`(id: str, t: int, v: int)`，仅来自 `enabled=true` 的站点，按源序。
- 异常：截断、非法 UTF-8、JSON 语法错误、契约违约一律抛 `ValueError`；
  `chunks` 自身抛出的异常**原样传播**（不包装、不吞掉）。

## 契约（被严格校验）

- 顶层对象必含 `stations` 数组；每站必含字符串 `id`、布尔 `enabled`、
  数组 `samples`；每个采样必含 `t`、`v`。字段顺序任意，缺字段/类型错/
  契约内字段重名均报 `ValueError`。
- 其余字段只做**语法校验后跳过**，键名与 `id` 长度 ≤ 64 字符。
- `t`/`v` 必须是有符号 64 位整数（含边界 `±9223372036854775807/8`），
  小数、指数、越界均报错。
- 嵌套容器最多 32 层（根对象本身不计层）。

## 内存设计（关键）

| 关注点 | 处理方式 |
| --- | --- |
| 输入缓冲 | 只保留“当前词法单元残片”，每块追加后压缩已消费前缀 |
| 忽略值（可含巨型字符串/深层嵌套） | `skip_value` 逐字节/逐段做语法校验，**从不物化**；字符串普通段用 C 层 `translate+find` 跳过 |
| 当前站点采样 | 仅启用站点写临时文件（每条 16 字节定长记录），站点闭合后顺序回放，**整站不入内存** |
| 整数 | 切出 token 后转换；超长数字逐位饱和，不构造超大整数对象 |
| 临时文件 | 正常结束、抛错、提前 `close()`、GC 回收均经 `finally` 清理 |

实测（64 KiB 块、站点与忽略值各 32 MiB、消费端不存结果，Python 3.11）：

- 单站点 32 MiB / 132 万条采样：Python 峰值分配 **≈ 3.2 MiB**（≤ 16 MiB）。
- 32 MiB 忽略字符串：Python 峰值分配 **< 0.5 MiB**。

## 目录

```
portaljson/_impl.py      实现（Feeder 字节投喂器 + Parser 状态机 + 暂存回放）
tests/reference.py       独立对照实现（标准库 json 整体解析，仅用于对照）
tests/test_crosscheck.py 随机文档 × 多种切块粒度的对照测试
tests/test_boundary.py   截断/非法 UTF-8/语法/契约/深度/清理 边界测试
tests/test_memory.py     32 MiB 内存实测（tracemalloc 峰值，独立 worker）
tests/memory_worker.py   内存实测 worker
demo.py                  端到端演示
run_tests.py             统一测试入口
.pointwise-runtime.json  运行清单（setup/verify/demo 的真实 argv）
```

## 运行

```bash
python3 -I run_tests.py    # 对照 + 边界 + 内存实测
python3 -I demo.py         # 演示
```

> 所有命令用 `-I`（隔离模式）运行，避免当前目录下被植入的 `json.py` 等
> 模块影响导入；测试脚本自行把仓库根加入 `sys.path`。
