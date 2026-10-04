"""A B+ tree index over fixed-size pages persisted to a local file or memory.

Duplicate-key policy: keys are unique. `insert` raises KeyError when the
key already exists; use `update` to change the value of an existing key.
"""

import bisect

from .nodes import (
    Codec,
    InternalNode,
    LeafNode,
    decode_node,
    encode_node,
    internal_capacity,
    leaf_capacity,
)
from .pager import KEY_BYTES, KEY_INT, NO_PAGE, CorruptionError, Meta, Pager


class BPlusTree:
    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    @classmethod
    def create(cls, path=None, page_size=4096, key_kind="int", key_size=8,
               value_kind="int", value_size=8):
        """Create a new empty tree backed by `path` (or memory if None)."""
        pager = Pager(path, page_size)
        meta = Meta(
            page_size=page_size,
            key_size=key_size,
            value_size=value_size,
            key_kind=KEY_INT if key_kind == "int" else KEY_BYTES,
            value_kind=KEY_INT if value_kind == "int" else KEY_BYTES,
        )
        root = LeafNode(pager.allocate(meta))
        tree = cls(pager, meta)
        tree._store(root)
        meta.root_pid = root.pid
        pager.write_meta(meta)
        return tree

    @classmethod
    def open(cls, path, page_size=4096):
        """Open an existing tree file previously created with `create`."""
        pager = Pager(path, page_size)
        if not pager.has_meta():
            raise CorruptionError("file does not contain a B+ tree")
        meta = pager.read_meta()
        return cls(pager, meta)

    def __init__(self, pager, meta):
        self.pager = pager
        self.meta = meta
        self.key_codec = Codec(meta.key_kind, meta.key_size)
        self.value_codec = Codec(meta.value_kind, meta.value_size)
        self.max_leaf_keys = leaf_capacity(
            meta.page_size, meta.key_size, meta.value_size)
        self.max_internal_keys = internal_capacity(
            meta.page_size, meta.key_size)
        if self.max_leaf_keys < 2 or self.max_internal_keys < 2:
            raise ValueError("page_size too small for the given key/value sizes")
        # minimum occupancy (root is exempt)
        self.min_leaf_keys = (self.max_leaf_keys + 1) // 2
        # internal nodes hold at most max_internal_keys+1 children and at
        # least ceil((max+1)/2) children -> min keys = that minus one
        self.min_internal_keys = (self.max_internal_keys + 2) // 2 - 1

    # ------------------------------------------------------------------
    # node io
    # ------------------------------------------------------------------

    def _load(self, pid):
        raw = self.pager.read_raw(pid)
        return decode_node(raw, pid, self.key_codec, self.value_codec,
                           self.max_leaf_keys, self.max_internal_keys)

    def _store(self, node):
        raw = encode_node(node, self.meta.page_size, self.key_codec,
                          self.value_codec)
        self.pager.write_raw(node.pid, raw)

    def _new_page(self, node):
        node.pid = self.pager.allocate(self.meta)
        return node

    def _is_root(self, node):
        return node.pid == self.meta.root_pid

    # ------------------------------------------------------------------
    # traversal helpers
    # ------------------------------------------------------------------

    def _root(self):
        return self._load(self.meta.root_pid)

    def _find_leaf(self, key):
        node = self._root()
        while not node.is_leaf:
            idx = bisect.bisect_right(node.keys, key)
            node = self._load(node.children[idx])
        return node

    def _leftmost_leaf(self):
        node = self._root()
        while not node.is_leaf:
            node = self._load(node.children[0])
        return node

    # ------------------------------------------------------------------
    # public read API
    # ------------------------------------------------------------------

    def search(self, key):
        """Return the value for `key`, or None if absent."""
        leaf = self._find_leaf(key)
        idx = bisect.bisect_left(leaf.keys, key)
        if idx < len(leaf.keys) and leaf.keys[idx] == key:
            return leaf.values[idx]
        return None

    def __contains__(self, key):
        return self.search(key) is not None

    def range_scan(self, low=None, high=None, include_low=True,
                   include_high=True):
        """Yield (key, value) pairs in strictly ascending key order.

        Bounds are optional; each may be inclusive or exclusive.
        """
        if low is not None and high is not None:
            if low > high or (low == high and not
                              (include_low and include_high)):
                return
        leaf = self._find_leaf(low) if low is not None else self._leftmost_leaf()
        while True:
            for key, value in zip(leaf.keys, leaf.values):
                if low is not None:
                    if key < low or (key == low and not include_low):
                        continue
                if high is not None:
                    if key > high or (key == high and not include_high):
                        return
                yield key, value
            if leaf.next == NO_PAGE:
                return
            leaf = self._load(leaf.next)

    def items(self):
        """Yield all (key, value) pairs in ascending key order."""
        return self.range_scan()

    def depth(self):
        node = self._root()
        d = 1
        while not node.is_leaf:
            node = self._load(node.children[0])
            d += 1
        return d

    # ------------------------------------------------------------------
    # insert / update
    # ------------------------------------------------------------------

    def insert(self, key, value):
        """Insert a new key/value pair. Raises KeyError on duplicates."""
        leaf = self._find_leaf(key)
        idx = bisect.bisect_left(leaf.keys, key)
        if idx < len(leaf.keys) and leaf.keys[idx] == key:
            raise KeyError(f"duplicate key {key!r}")
        leaf.keys.insert(idx, key)
        leaf.values.insert(idx, value)
        if len(leaf.keys) > self.max_leaf_keys:
            self._split_leaf(leaf)  # split first: an overflowing node
        else:                       # no longer fits into a single page
            self._store(leaf)

    def update(self, key, value):
        """Replace the value of an existing key. Raises KeyError if absent."""
        leaf = self._find_leaf(key)
        idx = bisect.bisect_left(leaf.keys, key)
        if idx >= len(leaf.keys) or leaf.keys[idx] != key:
            raise KeyError(f"key {key!r} not found")
        leaf.values[idx] = value
        self._store(leaf)

    def _split_leaf(self, leaf):
        right = self._new_page(LeafNode(parent=leaf.parent))
        mid = (len(leaf.keys) + 1) // 2
        right.keys = leaf.keys[mid:]
        right.values = leaf.values[mid:]
        leaf.keys = leaf.keys[:mid]
        leaf.values = leaf.values[:mid]
        right.next = leaf.next
        leaf.next = right.pid
        self._store(leaf)
        self._store(right)
        # separator copied up is the first key of the right leaf
        self._insert_into_parent(leaf, right.keys[0], right)

    def _split_internal(self, node):
        right = self._new_page(InternalNode(parent=node.parent))
        mid = len(node.keys) // 2
        up_key = node.keys[mid]
        right.keys = node.keys[mid + 1:]
        right.children = node.children[mid + 1:]
        node.keys = node.keys[:mid]
        node.children = node.children[:mid + 1]
        for child_pid in right.children:
            child = self._load(child_pid)
            child.parent = right.pid
            self._store(child)
        self._store(node)
        self._store(right)
        # middle key moves up (not copied)
        self._insert_into_parent(node, up_key, right)

    def _insert_into_parent(self, left, key, right):
        if self._is_root(left):
            root = self._new_page(InternalNode(parent=NO_PAGE))
            root.keys = [key]
            root.children = [left.pid, right.pid]
            left.parent = root.pid
            right.parent = root.pid
            self._store(left)
            self._store(right)
            self._store(root)
            self.meta.root_pid = root.pid
            self.pager.write_meta(self.meta)
            return
        parent = self._load(left.parent)
        idx = parent.children.index(left.pid)
        parent.keys.insert(idx, key)
        parent.children.insert(idx + 1, right.pid)
        right.parent = parent.pid
        self._store(right)
        if len(parent.keys) > self.max_internal_keys:
            self._split_internal(parent)
        else:
            self._store(parent)

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    def delete(self, key):
        """Remove `key`. Raises KeyError if absent."""
        leaf = self._find_leaf(key)
        idx = bisect.bisect_left(leaf.keys, key)
        if idx >= len(leaf.keys) or leaf.keys[idx] != key:
            raise KeyError(f"key {key!r} not found")
        leaf.keys.pop(idx)
        leaf.values.pop(idx)
        self._store(leaf)
        if self._is_root(leaf):
            return
        if len(leaf.keys) < self.min_leaf_keys:
            self._rebalance_leaf(leaf)

    def _rebalance_leaf(self, leaf):
        parent = self._load(leaf.parent)
        idx = parent.children.index(leaf.pid)
        left = self._load(parent.children[idx - 1]) if idx > 0 else None
        right = (self._load(parent.children[idx + 1])
                 if idx + 1 < len(parent.children) else None)

        # 1) redistribution: borrow from a sibling that can spare a key
        if left is not None and len(left.keys) > self.min_leaf_keys:
            leaf.keys.insert(0, left.keys.pop())
            leaf.values.insert(0, left.values.pop())
            parent.keys[idx - 1] = leaf.keys[0]
            self._store(left)
            self._store(leaf)
            self._store(parent)
            return
        if right is not None and len(right.keys) > self.min_leaf_keys:
            leaf.keys.append(right.keys.pop(0))
            leaf.values.append(right.values.pop(0))
            parent.keys[idx] = right.keys[0]
            self._store(right)
            self._store(leaf)
            self._store(parent)
            return

        # 2) merge with a sibling, then fix the parent recursively
        if left is not None:
            left.keys.extend(leaf.keys)
            left.values.extend(leaf.values)
            left.next = leaf.next
            self._store(left)
            parent.keys.pop(idx - 1)
            parent.children.pop(idx)
            self.pager.free(self.meta, leaf.pid)
        else:
            leaf.keys.extend(right.keys)
            leaf.values.extend(right.values)
            leaf.next = right.next
            self._store(leaf)
            parent.keys.pop(idx)
            parent.children.pop(idx + 1)
            self.pager.free(self.meta, right.pid)
        self._store(parent)
        self._rebalance_internal(parent)

    def _rebalance_internal(self, node):
        if self._is_root(node):
            # root shrink: an internal root with no keys is replaced by
            # its single child
            if not node.is_leaf and len(node.keys) == 0:
                child = self._load(node.children[0])
                child.parent = NO_PAGE
                self._store(child)
                self.meta.root_pid = child.pid
                self.pager.write_meta(self.meta)
                self.pager.free(self.meta, node.pid)
            return
        if len(node.keys) >= self.min_internal_keys:
            return

        parent = self._load(node.parent)
        idx = parent.children.index(node.pid)
        left = self._load(parent.children[idx - 1]) if idx > 0 else None
        right = (self._load(parent.children[idx + 1])
                 if idx + 1 < len(parent.children) else None)

        # 1) redistribution through the parent separator
        if left is not None and len(left.keys) > self.min_internal_keys:
            node.keys.insert(0, parent.keys[idx - 1])
            moved_child = left.children.pop()
            node.children.insert(0, moved_child)
            child = self._load(moved_child)
            child.parent = node.pid
            self._store(child)
            parent.keys[idx - 1] = left.keys.pop()
            self._store(left)
            self._store(node)
            self._store(parent)
            return
        if right is not None and len(right.keys) > self.min_internal_keys:
            node.keys.append(parent.keys[idx])
            moved_child = right.children.pop(0)
            node.children.append(moved_child)
            child = self._load(moved_child)
            child.parent = node.pid
            self._store(child)
            parent.keys[idx] = right.keys.pop(0)
            self._store(right)
            self._store(node)
            self._store(parent)
            return

        # 2) merge: pull the separator down and fuse the nodes
        if left is not None:
            left.keys.append(parent.keys[idx - 1])
            left.keys.extend(node.keys)
            left.children.extend(node.children)
            for child_pid in node.children:
                child = self._load(child_pid)
                child.parent = left.pid
                self._store(child)
            self._store(left)
            parent.keys.pop(idx - 1)
            parent.children.pop(idx)
            self.pager.free(self.meta, node.pid)
        else:
            node.keys.append(parent.keys[idx])
            node.keys.extend(right.keys)
            node.children.extend(right.children)
            for child_pid in right.children:
                child = self._load(child_pid)
                child.parent = node.pid
                self._store(child)
            self._store(node)
            parent.keys.pop(idx)
            parent.children.pop(idx + 1)
            self.pager.free(self.meta, right.pid)
        self._store(parent)
        self._rebalance_internal(parent)

    # ------------------------------------------------------------------
    # integrity check
    # ------------------------------------------------------------------

    def check_integrity(self):
        """Validate the whole tree. Returns a list of problem strings;
        an empty list means the structure is fully consistent."""
        problems = []

        def fail(msg):
            problems.append(msg)

        try:
            root = self._root()
        except CorruptionError as exc:
            return [f"root unreadable: {exc}"]

        leaf_depths = []
        traversal_leaves = []
        visited = set()

        def visit(node, depth, low, high):
            if node.pid in visited:
                fail(f"page {node.pid} visited twice")
                return
            visited.add(node.pid)
            if any(node.keys[i] >= node.keys[i + 1]
                   for i in range(len(node.keys) - 1)):
                fail(f"page {node.pid}: keys not strictly sorted")
            if low is not None and node.keys and node.keys[0] < low:
                fail(f"page {node.pid}: key below lower bound")
            if high is not None and node.keys and node.keys[-1] >= high:
                fail(f"page {node.pid}: key above upper bound")
            if not self._is_root(node):
                limit = (self.min_leaf_keys if node.is_leaf
                         else self.min_internal_keys)
                if len(node.keys) < limit:
                    fail(f"page {node.pid}: underflow "
                         f"({len(node.keys)} < {limit})")
            if node.is_leaf:
                if len(node.keys) != len(node.values):
                    fail(f"page {node.pid}: key/value count mismatch")
                leaf_depths.append(depth)
                traversal_leaves.append(node)
                return
            if len(node.children) != len(node.keys) + 1:
                fail(f"page {node.pid}: child count mismatch")
                return
            for i, child_pid in enumerate(node.children):
                try:
                    child = self._load(child_pid)
                except CorruptionError as exc:
                    fail(f"page {node.pid}: child {child_pid} unreadable: {exc}")
                    continue
                if child.parent != node.pid:
                    fail(f"page {child_pid}: parent pointer "
                         f"{child.parent} != {node.pid}")
                child_low = low if i == 0 else node.keys[i - 1]
                child_high = high if i == len(node.keys) else node.keys[i]
                visit(child, depth + 1, child_low, child_high)

        visit(root, 1, None, None)

        if len(set(leaf_depths)) > 1:
            fail("leaves at different depths")

        # leaf chain must visit exactly the same leaves in the same order
        chain = []
        seen = set()
        try:
            leaf = self._leftmost_leaf()
            while True:
                if leaf.pid in seen:
                    fail("leaf chain contains a cycle")
                    break
                seen.add(leaf.pid)
                chain.append(leaf)
                if leaf.next == NO_PAGE:
                    break
                leaf = self._load(leaf.next)
        except CorruptionError as exc:
            fail(f"leaf chain broken: {exc}")
        if [n.pid for n in chain] != [n.pid for n in traversal_leaves]:
            fail("leaf chain inconsistent with tree traversal")
        chain_keys = [k for leaf in chain for k in leaf.keys]
        if chain_keys != sorted(chain_keys):
            fail("leaf chain keys not globally sorted")
        if len(chain_keys) != len(set(chain_keys)):
            fail("duplicate keys in leaf chain")
        return problems

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def sync(self):
        self.pager.write_meta(self.meta)
        self.pager.sync()

    def close(self):
        self.pager.write_meta(self.meta)
        self.pager.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
