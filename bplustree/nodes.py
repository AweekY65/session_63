"""B+ tree node encoding/decoding into fixed-size pages.

Page layout (all integers little-endian):

  offset 0      : 1 byte  node magic (0xB7)
  offset 1      : 1 byte  node type (1 = leaf, 2 = internal)
  offset 2      : 4 bytes number of keys (uint32)
  offset 6      : 8 bytes parent page id (-1 = none)
  offset 14     : 8 bytes next leaf page id (leaf only, -1 = none)
  data region   : keys followed by values (leaf) or child page ids
                  (internal, num_keys + 1 of them, 8 bytes each)
  last 2 bytes  : checksum = sum of all preceding bytes mod 2**16
"""

import struct

from .pager import CorruptionError, KEY_BYTES, KEY_INT, NO_PAGE

NODE_MAGIC = 0xB7
TYPE_LEAF = 1
TYPE_INTERNAL = 2

LEAF_HEADER = 22
INTERNAL_HEADER = 14
CHECKSUM_SIZE = 2
CHILD_PTR_SIZE = 8


class Codec:
    """Packs/unpacks keys and values: 8-byte ints or fixed-length bytes."""

    def __init__(self, kind, size):
        if kind == KEY_INT:
            if size != 8:
                raise ValueError("integer keys/values must be 8 bytes")
        elif kind != KEY_BYTES:
            raise ValueError(f"unknown codec kind {kind}")
        self.kind = kind
        self.size = size

    def pack(self, value):
        if self.kind == KEY_INT:
            return struct.pack("<q", value)
        if not isinstance(value, (bytes, bytearray)):
            raise TypeError("expected bytes")
        if len(value) > self.size:
            raise ValueError(f"value too long: {len(value)} > {self.size}")
        return bytes(value).ljust(self.size, b"\x00")

    def unpack(self, data):
        if self.kind == KEY_INT:
            return struct.unpack("<q", data)[0]
        return bytes(data).rstrip(b"\x00")


class Node:
    def __init__(self, pid=NO_PAGE, is_leaf=True, parent=NO_PAGE):
        self.pid = pid
        self.is_leaf = is_leaf
        self.parent = parent
        self.keys = []


class LeafNode(Node):
    def __init__(self, pid=NO_PAGE, parent=NO_PAGE):
        super().__init__(pid, True, parent)
        self.values = []
        self.next = NO_PAGE


class InternalNode(Node):
    def __init__(self, pid=NO_PAGE, parent=NO_PAGE):
        super().__init__(pid, False, parent)
        self.children = []


def leaf_capacity(page_size, key_size, value_size):
    """Maximum number of key/value pairs a leaf page can hold."""
    return (page_size - LEAF_HEADER - CHECKSUM_SIZE) // (key_size + value_size)


def internal_capacity(page_size, key_size):
    """Maximum number of keys an internal page can hold."""
    usable = page_size - INTERNAL_HEADER - CHECKSUM_SIZE
    return (usable - CHILD_PTR_SIZE) // (key_size + CHILD_PTR_SIZE)


def encode_node(node, page_size, key_codec, value_codec):
    buf = bytearray(page_size)
    buf[0] = NODE_MAGIC
    buf[1] = TYPE_LEAF if node.is_leaf else TYPE_INTERNAL
    struct.pack_into("<I", buf, 2, len(node.keys))
    struct.pack_into("<q", buf, 6, node.parent)
    if node.is_leaf:
        struct.pack_into("<q", buf, 14, node.next)
        off = LEAF_HEADER
        for key in node.keys:
            buf[off:off + key_codec.size] = key_codec.pack(key)
            off += key_codec.size
        for value in node.values:
            buf[off:off + value_codec.size] = value_codec.pack(value)
            off += value_codec.size
    else:
        off = INTERNAL_HEADER
        for key in node.keys:
            buf[off:off + key_codec.size] = key_codec.pack(key)
            off += key_codec.size
        for child in node.children:
            struct.pack_into("<q", buf, off, child)
            off += CHILD_PTR_SIZE
    struct.pack_into("<H", buf, page_size - CHECKSUM_SIZE,
                     sum(buf[:page_size - CHECKSUM_SIZE]) & 0xFFFF)
    return bytes(buf)


def decode_node(data, pid, key_codec, value_codec, max_leaf_keys,
                max_internal_keys):
    page_size = len(data)
    if page_size < LEAF_HEADER + CHECKSUM_SIZE:
        raise CorruptionError(f"page {pid}: too small")
    if data[0] != NODE_MAGIC:
        raise CorruptionError(f"page {pid}: bad node magic")
    (stored_checksum,) = struct.unpack_from("<H", data, page_size - CHECKSUM_SIZE)
    actual = sum(data[:page_size - CHECKSUM_SIZE]) & 0xFFFF
    if stored_checksum != actual:
        raise CorruptionError(f"page {pid}: checksum mismatch")
    node_type = data[1]
    (num_keys,) = struct.unpack_from("<I", data, 2)
    (parent,) = struct.unpack_from("<q", data, 6)
    if node_type == TYPE_LEAF:
        if num_keys > max_leaf_keys:
            raise CorruptionError(f"page {pid}: leaf key count {num_keys} overflows")
        node = LeafNode(pid, parent)
        (node.next,) = struct.unpack_from("<q", data, 14)
        off = LEAF_HEADER
        for _ in range(num_keys):
            node.keys.append(key_codec.unpack(data[off:off + key_codec.size]))
            off += key_codec.size
        for _ in range(num_keys):
            node.values.append(value_codec.unpack(data[off:off + value_codec.size]))
            off += value_codec.size
        return node
    if node_type == TYPE_INTERNAL:
        if num_keys > max_internal_keys:
            raise CorruptionError(f"page {pid}: internal key count overflows")
        node = InternalNode(pid, parent)
        off = INTERNAL_HEADER
        for _ in range(num_keys):
            node.keys.append(key_codec.unpack(data[off:off + key_codec.size]))
            off += key_codec.size
        for _ in range(num_keys + 1):
            (child,) = struct.unpack_from("<q", data, off)
            node.children.append(child)
            off += CHILD_PTR_SIZE
        return node
    raise CorruptionError(f"page {pid}: unknown node type {node_type}")
