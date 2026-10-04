# bplustree — 基于固定大小 Page 的本地 B+ Tree 索引

一个纯 Python 实现的磁盘 B+ Tree 索引。所有 page、元数据和索引节点只持久化在
**本地文件**（或纯内存）中，不依赖 SQLite、RocksDB、数据库服务器或任何外部服务。

## 功能

- 支持 8 字节整数 key 或固定长度字节 key 的 `insert` / `search` / `update` / `delete`
- 叶子节点按 key 严格有序，叶间双向通过 `next` 指针形成链表
- page 满时 split 并向父节点传播 separator；根分裂时创建新根
- 删除导致低于最小占用率时先 redistribution（向兄弟借 key），否则 merge；
  根节点空 key 时自动收缩（降低树高）
- 闭区间 / 开区间 / 半开区间范围扫描，结果严格有序、无重复遗漏
- 重复 key 策略：key 唯一，`insert` 重复 key 抛 `KeyError`，改值用 `update`
- `check_integrity()` 校验排序、父子指针、占用率、叶链一致性与树高一致
- page 带校验和，结构损坏时抛出 `CorruptionError`

## Page 格式

所有整数小端。文件第 0 页为 meta page，数据页从 page id 1 开始。

**Meta page（page 0）**

| 字段 | 大小 |
|---|---|
| magic `"BPLUSTRE"` | 8 B |
| page_size / key_size / value_size / key_kind / value_kind | 5 × 4 B |
| root_pid / page_count | 2 × 8 B |
| free_head（空闲页链表头） | 8 B |

**数据页（叶子 / 内部节点）**

| 偏移 | 内容 |
|---|---|
| 0 | 节点魔数 `0xB7`（1 B） |
| 1 | 类型：1 = 叶子，2 = 内部（1 B） |
| 2 | key 数量 num_keys（4 B） |
| 6 | 父节点 page id，-1 表示无（8 B） |
| 14 | 叶子专有：下一个叶子的 page id（8 B） |
| 数据区 | 叶子：num_keys 个 key + num_keys 个 value；内部：num_keys 个 key + (num_keys+1) 个子页指针（各 8 B） |
| 末尾 2 B | 校验和 = 前面所有字节之和 mod 2¹⁶ |

容量由 page 大小与 key/value 长度推导：
`leaf_max = (page_size − 24) / (key_size + value_size)`，
`internal_max = (page_size − 24) / (key_size + 8)`。
最小占用率为容量的一半（向上取整），根节点豁免。被删除的页进入空闲链表，
前 8 字节存放下一个空闲页 id，新页优先从空闲链表分配。

## Split / Merge 算法

**插入**：沿 separator 下行到目标叶子，按序插入。叶子溢出时对半分裂，
右叶首 key 作为 separator **复制**到父节点，并维护叶链（`leaf.next`）。
内部节点溢出时中间 key **上移**（不复制）到父节点，子节点均分；若父节点
也溢出则递归向上分裂。根溢出时创建新根，树高加一。

**删除**：从叶子移除 key 后若低于最小占用率：
1. **Redistribution** — 若相邻兄弟有多余 key，经父节点 separator 借一个
   （叶子直接搬 key，内部节点把 separator 拉下、把兄弟的 key 推上）；
2. **Merge** — 兄弟也处于下限时与兄弟合并，父节点中的 separator 被删除
   （内部节点合并时 separator 被拉入合并节点），并向上递归修复；
3. **根收缩** — 根为内部节点且 key 数为 0 时，唯一子节点成为新根，树高减一。

## 查找复杂度

- `search` / `insert` / `update` / `delete`：`O(log n)` 次 page 访问
  （每层一次比较定位 + 一次 page 读），页内二分 `O(log m)`
- 范围扫描：`O(log n + k)`，k 为命中条数（定位起始叶子后沿叶链顺序读）
- 空间：每个 key/value 占 `key_size + value_size` 字节，页占用率 ≥ 50%

## 使用示例

```python
from bplustree import BPlusTree

tree = BPlusTree.create("index.db", page_size=4096)   # 或 path=None 纯内存
tree.insert(42, 4200)
tree.update(42, 4300)
assert tree.search(42) == 4300
for key, value in tree.range_scan(10, 100, include_high=False):
    ...
tree.delete(42)
assert tree.check_integrity() == []
tree.close()

reopened = BPlusTree.open("index.db")                 # 重新加载，结果一致
```

固定长度字节 key：`BPlusTree.create("i.db", key_kind="bytes", key_size=16,
value_kind="bytes", value_size=8)`。

## 运行测试

```bash
python3 -m pytest tests/ -q
```

测试覆盖：连续/随机/倒序插入、多层 split、update、重复 key 拒绝、
删除引发的 redistribution 与 merge、根收缩、闭/开区间范围扫描、
有序性与去重、文件重载一致性、固定长度字节 key、负整数与大整数 key、
page 校验和损坏检测、叶链断裂与父指针损坏的完整性报告。
