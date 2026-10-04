"""Node (de)serialization onto fixed-size pages.

Leaf page layout (payload area, page_size - 4 bytes):
    B   node type (1 = leaf)
    H   number of keys
    Q   next leaf page id (0 = none)
    Q   prev leaf page id (0 = none)
    then nkeys * (key bytes, value bytes)

Internal page layout:
    B   node type (0 = internal)
    H   number of keys
    Q   first child page id
    then nkeys * (key bytes, Q child page id)

An internal node with k keys has k+1 children; key[i] is the smallest key
that may appear in child[i+1] (separator semantics).
"""

from __future__ import annotations

import struct

from .errors import CorruptionError

NODE_INTERNAL = 0
NODE_LEAF = 1

LEAF_HEADER = struct.Struct("<BHQQ")
INTERNAL_HEADER = struct.Struct("<BHQ")
PAGE_ID = struct.Struct("<Q")


def leaf_capacity(page_size, key_size, value_size):
    """Max number of entries in a leaf page."""
    usable = page_size - 4 - LEAF_HEADER.size
    return usable // (key_size + value_size)


def internal_capacity(page_size, key_size):
    """Max number of keys in an internal page."""
    usable = page_size - 4 - INTERNAL_HEADER.size
    return (usable - PAGE_ID.size) // (key_size + PAGE_ID.size)


class KeyCodec:
    """Encodes/decodes fixed-length keys (int64 or fixed bytes)."""

    def __init__(self, kind, size):
        self.kind = kind  # "int" or "bytes"
        self.size = size

    def encode(self, key):
        if self.kind == "int":
            if not isinstance(key, int):
                raise TypeError(f"expected int key, got {type(key).__name__}")
            return struct.pack("<q", key)
        if isinstance(key, str):
            key = key.encode()
        key = bytes(key)
        if len(key) > self.size:
            raise ValueError(f"key too long: {len(key)} > {self.size}")
        return key.ljust(self.size, b"\x00")

    def decode(self, raw):
        if self.kind == "int":
            return struct.unpack("<q", raw)[0]
        return raw.rstrip(b"\x00")


class LeafNode:
    __slots__ = ("page_id", "keys", "values", "next", "prev")

    def __init__(self, page_id, keys=None, values=None, next=0, prev=0):
        self.page_id = page_id
        self.keys = keys if keys is not None else []
        self.values = values if values is not None else []
        self.next = next
        self.prev = prev

    def serialize(self, codec, value_size):
        out = [LEAF_HEADER.pack(NODE_LEAF, len(self.keys), self.next,
                                self.prev)]
        for key, value in zip(self.keys, self.values):
            out.append(codec.encode(key))
            out.append(value.ljust(value_size, b"\x00")[:value_size])
        return b"".join(out)

    @classmethod
    def deserialize(cls, page_id, payload, codec, value_size):
        ntype, nkeys, nxt, prv = LEAF_HEADER.unpack(payload[:LEAF_HEADER.size])
        if ntype != NODE_LEAF:
            raise CorruptionError(f"page {page_id}: expected leaf, got {ntype}")
        node = cls(page_id, next=nxt, prev=prv)
        off = LEAF_HEADER.size
        entry = codec.size + value_size
        end = off + nkeys * entry
        if end > len(payload):
            raise CorruptionError(f"page {page_id}: leaf entries overflow")
        for i in range(nkeys):
            kraw = payload[off:off + codec.size]
            vraw = payload[off + codec.size:off + entry]
            node.keys.append(codec.decode(kraw))
            node.values.append(vraw)
            off += entry
        return node


class InternalNode:
    __slots__ = ("page_id", "keys", "children")

    def __init__(self, page_id, keys=None, children=None):
        self.page_id = page_id
        self.keys = keys if keys is not None else []
        self.children = children if children is not None else []

    def serialize(self, codec, value_size):
        out = [INTERNAL_HEADER.pack(NODE_INTERNAL, len(self.keys),
                                    self.children[0])]
        for key, child in zip(self.keys, self.children[1:]):
            out.append(codec.encode(key))
            out.append(PAGE_ID.pack(child))
        return b"".join(out)

    @classmethod
    def deserialize(cls, page_id, payload, codec, value_size):
        ntype, nkeys, child0 = INTERNAL_HEADER.unpack(
            payload[:INTERNAL_HEADER.size])
        if ntype != NODE_INTERNAL:
            raise CorruptionError(
                f"page {page_id}: expected internal, got {ntype}")
        node = cls(page_id, children=[child0])
        off = INTERNAL_HEADER.size
        entry = codec.size + PAGE_ID.size
        if off + nkeys * entry > len(payload):
            raise CorruptionError(f"page {page_id}: internal entries overflow")
        for i in range(nkeys):
            kraw = payload[off:off + codec.size]
            craw = payload[off + codec.size:off + entry]
            node.keys.append(codec.decode(kraw))
            node.children.append(PAGE_ID.unpack(craw)[0])
            off += entry
        return node


def load_node(pager, page_id, codec, value_size):
    payload = pager.read_page(page_id)
    ntype = payload[0]
    if ntype == NODE_LEAF:
        return LeafNode.deserialize(page_id, payload, codec, value_size)
    if ntype == NODE_INTERNAL:
        return InternalNode.deserialize(page_id, payload, codec, value_size)
    raise CorruptionError(f"page {page_id}: unknown node type {ntype}")


def store_node(pager, node, codec, value_size):
    pager.write_page(node.page_id, node.serialize(codec, value_size))
