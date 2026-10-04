"""B+ tree on top of the fixed-size page manager.

Duplicate-key policy: keys are unique. insert() on an existing key raises
DuplicateKeyError; update() on a missing key raises KeyNotFoundError.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right

from .errors import (CorruptionError, DuplicateKeyError, IntegrityError,
                     KeyNotFoundError)
from .nodes import (InternalNode, KeyCodec, LeafNode, internal_capacity,
                    leaf_capacity, load_node, store_node)
from .pager import KEY_KIND_BYTES, KEY_KIND_INT, Pager


class BPlusTree:
    def __init__(self, path=None, page_size=4096, key_kind="int",
                 key_size=8, value_size=8, _pager=None):
        """Create a new tree, or attach to an existing file/pager.

        If `path` points to an existing tree file its stored configuration
        (page size, key kind/size, value size) takes precedence.
        """
        if _pager is not None:
            self._pager = _pager
        else:
            self._pager = Pager(path, page_size)
        pager = self._pager
        fresh = pager.num_entries == 0 and pager.root_id == 0
        if fresh:
            pager.key_kind = KEY_KIND_INT if key_kind == "int" else KEY_KIND_BYTES
            if key_kind == "int":
                key_size = 8
            pager.key_size = key_size
            pager.value_size = value_size
            pager.save_meta()
        self._codec = KeyCodec(
            "int" if pager.key_kind == KEY_KIND_INT else "bytes",
            pager.key_size)
        self.value_size = pager.value_size
        self.leaf_cap = leaf_capacity(pager.page_size, pager.key_size,
                                      self.value_size)
        self.internal_cap = internal_capacity(pager.page_size,
                                              pager.key_size)
        if self.leaf_cap < 2 or self.internal_cap < 2:
            raise ValueError(
                "page_size too small for the given key/value sizes")
        # Minimum occupancy thresholds.
        self._leaf_min = (self.leaf_cap + 1) // 2
        self._internal_min = (self.internal_cap + 2) // 2 - 1

    # ------------------------------------------------------------- lifecycle

    @classmethod
    def open(cls, path, **kwargs):
        """Open (or create) a tree stored in a local file."""
        return cls(path=path, **kwargs)

    @classmethod
    def in_memory(cls, **kwargs):
        """Create a tree that lives purely in memory."""
        return cls(path=None, **kwargs)

    def close(self):
        self._pager.close()

    def flush(self):
        self._pager.flush()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __len__(self):
        return self._pager.num_entries

    # -------------------------------------------------------------- helpers

    def _load(self, page_id):
        return load_node(self._pager, page_id, self._codec, self.value_size)

    def _store(self, node):
        store_node(self._pager, node, self._codec, self.value_size)

    def _new_page(self):
        return self._pager.allocate_page()

    def _normalize_value(self, value):
        if isinstance(value, int):
            value = value.to_bytes(self.value_size, "little", signed=True)
        value = bytes(value)
        if len(value) > self.value_size:
            raise ValueError(
                f"value too long: {len(value)} > {self.value_size}")
        return value.ljust(self.value_size, b"\x00")

    def _find_leaf(self, key):
        """Descend to the leaf that should contain `key`."""
        if self._pager.root_id == 0:
            return None
        node = self._load(self._pager.root_id)
        while isinstance(node, InternalNode):
            idx = bisect_right(node.keys, key)
            node = self._load(node.children[idx])
        return node

    # ------------------------------------------------------------ public api

    def insert(self, key, value):
        value = self._normalize_value(value)
        if self._pager.root_id == 0:
            leaf = LeafNode(self._new_page(), keys=[key], values=[value])
            self._store(leaf)
            self._pager.root_id = leaf.page_id
            self._pager.num_entries = 1
            self._pager.save_meta()
            return
        split = self._insert(self._load(self._pager.root_id), key, value)
        if split is not None:
            sep, right = split
            root = InternalNode(self._new_page(), keys=[sep],
                                children=[self._pager.root_id,
                                          right.page_id])
            self._store(root)
            self._pager.root_id = root.page_id
        self._pager.num_entries += 1
        self._pager.save_meta()

    def _insert(self, node, key, value):
        """Insert into subtree rooted at node.

        Returns None, or (separator, new_right_node) if node split.
        """
        if isinstance(node, LeafNode):
            idx = bisect_left(node.keys, key)
            if idx < len(node.keys) and node.keys[idx] == key:
                raise DuplicateKeyError(f"duplicate key {key!r}")
            node.keys.insert(idx, key)
            node.values.insert(idx, value)
            if len(node.keys) <= self.leaf_cap:
                self._store(node)
                return None
            # Split: left keeps the lower half.
            mid = len(node.keys) // 2
            right = LeafNode(self._new_page(),
                             keys=node.keys[mid:],
                             values=node.values[mid:],
                             next=node.next, prev=node.page_id)
            del node.keys[mid:]
            del node.values[mid:]
            if right.next:
                nxt = self._load(right.next)
                nxt.prev = right.page_id
                self._store(nxt)
            node.next = right.page_id
            self._store(node)
            self._store(right)
            return right.keys[0], right
        # Internal node.
        idx = bisect_right(node.keys, key)
        child = self._load(node.children[idx])
        split = self._insert(child, key, value)
        if split is None:
            return None
        sep, right = split
        node.keys.insert(idx, sep)
        node.children.insert(idx + 1, right.page_id)
        if len(node.keys) <= self.internal_cap:
            self._store(node)
            return None
        mid = len(node.keys) // 2
        new_right = InternalNode(self._new_page(),
                                 keys=node.keys[mid + 1:],
                                 children=node.children[mid + 1:])
        up = node.keys[mid]
        del node.keys[mid:]
        del node.children[mid + 1:]
        self._store(node)
        self._store(new_right)
        return up, new_right

    def search(self, key, default=None):
        """Return the value for `key`, or `default` if absent."""
        leaf = self._find_leaf(key)
        if leaf is None:
            return default
        idx = bisect_left(leaf.keys, key)
        if idx < len(leaf.keys) and leaf.keys[idx] == key:
            return leaf.values[idx]
        return default

    def __contains__(self, key):
        sentinel = object()
        return self.search(key, sentinel) is not sentinel

    def update(self, key, value):
        value = self._normalize_value(value)
        leaf = self._find_leaf(key)
        if leaf is None:
            raise KeyNotFoundError(key)
        idx = bisect_left(leaf.keys, key)
        if idx >= len(leaf.keys) or leaf.keys[idx] != key:
            raise KeyNotFoundError(key)
        leaf.values[idx] = value
        self._store(leaf)

    def delete(self, key):
        if self._pager.root_id == 0:
            raise KeyNotFoundError(key)
        root = self._load(self._pager.root_id)
        self._delete(root, key)
        # Root shrink: an internal root with a single child is replaced.
        if isinstance(root, InternalNode) and len(root.keys) == 0:
            self._pager.root_id = root.children[0]
            self._pager.free_page(root.page_id)
        elif isinstance(root, LeafNode) and len(root.keys) == 0:
            self._pager.root_id = 0
            self._pager.free_page(root.page_id)
        self._pager.num_entries -= 1
        self._pager.save_meta()

    def _delete(self, node, key):
        if isinstance(node, LeafNode):
            idx = bisect_left(node.keys, key)
            if idx >= len(node.keys) or node.keys[idx] != key:
                raise KeyNotFoundError(key)
            del node.keys[idx]
            del node.values[idx]
            self._store(node)
            return
        idx = bisect_right(node.keys, key)
        child = self._load(node.children[idx])
        self._delete(child, key)
        min_keys = (self._leaf_min if isinstance(child, LeafNode)
                    else self._internal_min)
        if len(child.keys) >= min_keys:
            return
        self._rebalance(node, idx, child)

    def _rebalance(self, parent, idx, child):
        """Fix an underflowing child of `parent` at position `idx`."""
        left = self._load(parent.children[idx - 1]) if idx > 0 else None
        right = (self._load(parent.children[idx + 1])
                 if idx + 1 < len(parent.children) else None)
        min_keys = (self._leaf_min if isinstance(child, LeafNode)
                    else self._internal_min)
        # 1. Redistribute from the left sibling.
        if left is not None and len(left.keys) > min_keys:
            if isinstance(child, LeafNode):
                child.keys.insert(0, left.keys.pop())
                child.values.insert(0, left.values.pop())
                parent.keys[idx - 1] = child.keys[0]
            else:
                child.keys.insert(0, parent.keys[idx - 1])
                child.children.insert(0, left.children.pop())
                parent.keys[idx - 1] = left.keys.pop()
            self._store(left)
            self._store(child)
            self._store(parent)
            return
        # 2. Redistribute from the right sibling.
        if right is not None and len(right.keys) > min_keys:
            if isinstance(child, LeafNode):
                child.keys.append(right.keys.pop(0))
                child.values.append(right.values.pop(0))
                parent.keys[idx] = right.keys[0]
            else:
                child.keys.append(parent.keys[idx])
                child.children.append(right.children.pop(0))
                parent.keys[idx] = right.keys.pop(0)
            self._store(right)
            self._store(child)
            self._store(parent)
            return
        # 3. Merge. Prefer merging into the left sibling.
        if left is not None:
            self._merge(parent, idx - 1, left, child)
        else:
            self._merge(parent, idx, child, right)

    def _merge(self, parent, sep_idx, left, right):
        """Merge `right` into `left`, dropping parent.keys[sep_idx]."""
        if isinstance(left, LeafNode):
            left.keys.extend(right.keys)
            left.values.extend(right.values)
            left.next = right.next
            if right.next:
                nxt = self._load(right.next)
                nxt.prev = left.page_id
                self._store(nxt)
        else:
            left.keys.append(parent.keys[sep_idx])
            left.keys.extend(right.keys)
            left.children.extend(right.children)
        del parent.keys[sep_idx]
        del parent.children[sep_idx + 1]
        self._store(left)
        self._store(parent)
        self._pager.free_page(right.page_id)

    # ------------------------------------------------------------ range scan

    def range_scan(self, lo=None, hi=None, lo_inclusive=True,
                   hi_inclusive=True):
        """Yield (key, value) pairs in ascending key order.

        Bounds are optional; pass None for an open-ended scan.
        """
        if self._pager.root_id == 0:
            return
        if lo is None:
            node = self._load(self._pager.root_id)
            while isinstance(node, InternalNode):
                node = self._load(node.children[0])
            idx = 0
        else:
            node = self._find_leaf(lo)
            idx = (bisect_left if lo_inclusive else bisect_right)(node.keys,
                                                                  lo)
        while node is not None:
            while idx < len(node.keys):
                key = node.keys[idx]
                if hi is not None:
                    if key > hi or (key == hi and not hi_inclusive):
                        return
                yield key, node.values[idx]
                idx += 1
            node = self._load(node.next) if node.next else None
            idx = 0

    def items(self):
        """Yield all (key, value) pairs in ascending order."""
        return self.range_scan()

    # --------------------------------------------------------------- verify

    def verify(self):
        """Check structural integrity; raise IntegrityError on problems.

        Validates: key ordering, separator/child key-range consistency,
        minimum occupancy, uniform leaf depth, leaf-chain links, and the
        stored entry count.
        """
        pager = self._pager
        if pager.root_id == 0:
            if pager.num_entries != 0:
                raise IntegrityError("empty tree with nonzero entry count")
            return True
        leaves = []
        depths = []
        count = self._verify_node(self._load(pager.root_id), None, None, 0,
                                  is_root=True, leaves=leaves, depths=depths)
        if len(set(depths)) != 1:
            raise IntegrityError(f"leaves at different depths: {depths}")
        # Leaf chain must match the in-order leaf sequence, both directions.
        for i, leaf in enumerate(leaves):
            expect_next = leaves[i + 1].page_id if i + 1 < len(leaves) else 0
            expect_prev = leaves[i - 1].page_id if i > 0 else 0
            if leaf.next != expect_next:
                raise IntegrityError(
                    f"leaf {leaf.page_id}: next={leaf.next}, "
                    f"expected {expect_next}")
            if leaf.prev != expect_prev:
                raise IntegrityError(
                    f"leaf {leaf.page_id}: prev={leaf.prev}, "
                    f"expected {expect_prev}")
        if count != pager.num_entries:
            raise IntegrityError(
                f"entry count mismatch: counted {count}, "
                f"meta says {pager.num_entries}")
        return True

    def _verify_node(self, node, lo, hi, depth, is_root, leaves, depths):
        for i in range(1, len(node.keys)):
            if not node.keys[i - 1] < node.keys[i]:
                raise IntegrityError(
                    f"page {node.page_id}: keys not strictly increasing")
        if lo is not None and node.keys and node.keys[0] < lo:
            raise IntegrityError(
                f"page {node.page_id}: key {node.keys[0]!r} below lower "
                f"bound {lo!r}")
        if hi is not None and node.keys and node.keys[-1] >= hi:
            raise IntegrityError(
                f"page {node.page_id}: key {node.keys[-1]!r} above upper "
                f"bound {hi!r}")
        if isinstance(node, LeafNode):
            if not is_root and len(node.keys) < self._leaf_min:
                raise IntegrityError(
                    f"leaf {node.page_id}: underflow "
                    f"({len(node.keys)} < {self._leaf_min})")
            if len(node.keys) > self.leaf_cap:
                raise IntegrityError(f"leaf {node.page_id}: overflow")
            leaves.append(node)
            depths.append(depth)
            return len(node.keys)
        # Internal node.
        if len(node.children) != len(node.keys) + 1:
            raise IntegrityError(
                f"page {node.page_id}: {len(node.keys)} keys but "
                f"{len(node.children)} children")
        if not is_root and len(node.keys) < self._internal_min:
            raise IntegrityError(
                f"internal {node.page_id}: underflow "
                f"({len(node.keys)} < {self._internal_min})")
        if len(node.keys) > self.internal_cap:
            raise IntegrityError(f"internal {node.page_id}: overflow")
        if is_root and len(node.keys) < 1:
            raise IntegrityError("internal root must have >= 1 key")
        total = 0
        bounds = [lo] + list(node.keys) + [hi]
        for i, child_id in enumerate(node.children):
            child = self._load(child_id)
            total += self._verify_node(child, bounds[i], bounds[i + 1],
                                       depth + 1, False, leaves, depths)
        return total

    # ------------------------------------------------------------- debugging

    def height(self):
        if self._pager.root_id == 0:
            return 0
        h = 1
        node = self._load(self._pager.root_id)
        while isinstance(node, InternalNode):
            node = self._load(node.children[0])
            h += 1
        return h
