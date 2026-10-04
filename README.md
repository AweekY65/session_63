# bplustree — 基于固定大小 page 的本地 B+ Tree 索引

纯 Python 实现，所有 page、元数据与索引节点只持久化在**本地文件**或**内存**中，
不依赖 SQLite、RocksDB、数据库服务器或任何外部服务。

## 功能

- 支持 int64 或固定长度 bytes 的 key，固定长度 value
- `insert` / `search` / `update` / `delete`，叶子节点按 key 严格有序
- page 满时 split，separator 向父节点传播；根分裂时创建新根
- 删除导致低于最小占用率时先 redistribution（向兄弟借），否则 merge；根可收缩
- 叶子节点双向链表，支持闭/开区间范围扫描，结果严格有序、无重复遗漏
- page 通过 page id 写入本地文件，重新打开后树结构与查询结果一致
- 每页 CRC32 校验，损坏即报 `CorruptionError`
- `verify()` 完整性检查：排序、父子 separator 约束、占用率、叶子链、条目计数
- 重复 key 策略：key 唯一，`insert` 重复 key 抛 `DuplicateKeyError`，
  修改已有 key 用 `update`

## Page 格式

文件由固定大小（默认 4096 字节，可配置）的 page 组成，每页最后 4 字节为
前 `page_size - 4` 字节的 CRC32 校验和。

```
page 0        : 元数据页（magic、版本、page 大小、根 page id、空闲页链表头、
                总页数、条目数、key 类型/长度、value 长度）
page 1..N-1   : 内部节点页 / 叶子节点页 / 空闲页（空闲页前 8 字节存下一空闲页 id）
```

叶子节点页：

```
B  node type = 1
H  key 数量
Q  next 叶子 page id（0 表示无）
Q  prev 叶子 page id
N × (key bytes, value bytes)      # 按 key 升序
```

内部节点页：

```
B  node type = 0
H  key 数量 k
Q  child[0] page id
k × (key bytes, Q child page id)  # 共 k+1 个子指针
```

内部节点中 `key[i]` 是 separator：`child[i]` 子树的 key 全部 `< key[i]`，
`child[i+1]` 子树的 key 全部 `>= key[i]`。

节点容量由 page 大小与 key/value 长度推导，例如 4KB 页、8 字节 key/value 时
叶子容量 254 条、内部节点容量 254 个 key。最小占用率为容量的一半（向上取整），
根节点除外。

## Split / Merge 算法

**插入 split（自底向上）**

1. 递归定位到目标叶子并按键序插入；若超过容量，从中间分裂为左右两个叶子，
   右叶子的最小 key 作为 separator 上传，同时维护叶子间 next/prev 链表。
2. 父内部节点插入 separator 与新子指针；若同样溢出，则将中间 key 上传，
   左右各分一半 key 与子指针（中间 key 不保留在下层）。
3. 若根节点分裂，创建只含一个 separator 的新根，树高加一。

**删除 merge / redistribution（自底向上）**

1. 从叶子删除 key 后，若节点 key 数低于最小占用率：
   - 优先向 key 数大于最小值的左（或右）兄弟**借**一个 key
     （内部节点借 key 时经过父节点 separator 旋转）；
   - 兄弟也不够借时与兄弟**合并**，父节点中对应 separator 下移/删除，
     被清空的 page 挂入空闲链表复用；叶子合并时修复链表指针。
2. 若内部根节点只剩一个子节点，子节点提升为新根，树高减一；
   根叶子删空则树变为空树。

## 复杂度

设树高为 h（h = O(log n)，n 为条目数，扇出由 page 大小决定，通常数百）：

| 操作          | 复杂度            |
| ------------- | ----------------- |
| search        | O(log n)          |
| insert        | O(log n)          |
| update        | O(log n)          |
| delete        | O(log n)          |
| range_scan    | O(log n + k)，k 为结果条数 |
| verify        | O(n)              |

## 使用示例

```python
from bplustree import BPlusTree

with BPlusTree.open("idx.db", page_size=4096, key_kind="int",
                    value_size=8) as t:
    t.insert(10, b"\x01" * 8)
    t.update(10, (123).to_bytes(8, "little"))
    print(t.search(10))
    t.delete(10)
    for k, v in t.range_scan(0, 100, hi_inclusive=False):
        ...
    t.verify()

# 纯内存模式
t = BPlusTree.in_memory(page_size=512, key_kind="bytes", key_size=16)
```

重新打开已有文件时，page 大小、key/value 配置自动从元数据页恢复。

## 运行测试

```bash
cd <项目根目录>
python3 -m pytest tests/ -q
```

测试覆盖：连续插入、随机插入、多层 split、删除合并/借用与根收缩、
闭/开区间范围扫描、重复 key 策略、文件重载一致性、页损坏与元数据损坏检测、
叶子链破坏后的完整性检查、固定长度 bytes key、纯内存模式。

## 目录结构

```
bplustree/
  __init__.py   # 包导出
  errors.py     # 异常类型
  pager.py      # 固定 page 读写、CRC 校验、空闲页链表、元数据页
  nodes.py      # 叶子/内部节点的序列化与反序列化
  tree.py       # B+ Tree：insert/search/update/delete/range_scan/verify
tests/
  test_bplustree.py
```
