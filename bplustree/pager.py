"""Fixed-size page management backed by a local file or memory.

Page 0 is reserved for metadata. Data pages are addressed by page id
(starting at 1). Freed pages form a singly linked free list whose head
is stored in the meta page; the first 8 bytes of a free page hold the
page id of the next free page (-1 terminates the list).
"""

import os
import struct

META_MAGIC = b"BPLUSTRE"
META_STRUCT = struct.Struct("<8sIIIIIQQq")
# magic, page_size, key_size, value_size, key_kind, value_kind,
# root_pid, page_count, free_head
META_PAGE_ID = 0
NO_PAGE = -1

KEY_INT = 0
KEY_BYTES = 1


class CorruptionError(Exception):
    """Raised when a page or the meta page fails validation."""


class Meta:
    __slots__ = (
        "page_size",
        "key_size",
        "value_size",
        "key_kind",
        "value_kind",
        "root_pid",
        "page_count",
        "free_head",
    )

    def __init__(self, page_size, key_size, value_size, key_kind, value_kind):
        self.page_size = page_size
        self.key_size = key_size
        self.value_size = value_size
        self.key_kind = key_kind
        self.value_kind = value_kind
        self.root_pid = NO_PAGE
        self.page_count = 1  # page 0 is the meta page
        self.free_head = NO_PAGE


class Pager:
    """Reads/writes fixed-size pages from a local file or memory."""

    def __init__(self, path=None, page_size=4096):
        if page_size < 64:
            raise ValueError("page_size must be >= 64")
        self.path = path
        self.page_size = page_size
        if path is None:
            self._mem = {}
            self._file = None
        else:
            self._mem = None
            self._file = open(path, "r+b" if os.path.exists(path) else "w+b")

    # -- meta page -----------------------------------------------------

    def write_meta(self, meta):
        buf = bytearray(self.page_size)
        META_STRUCT.pack_into(
            buf,
            0,
            META_MAGIC,
            meta.page_size,
            meta.key_size,
            meta.value_size,
            meta.key_kind,
            meta.value_kind,
            meta.root_pid,
            meta.page_count,
            meta.free_head,
        )
        self.write_raw(META_PAGE_ID, bytes(buf))

    def read_meta(self):
        raw = self.read_raw(META_PAGE_ID)
        if len(raw) < META_STRUCT.size:
            raise CorruptionError("meta page is truncated")
        (magic, page_size, key_size, value_size, key_kind, value_kind,
         root_pid, page_count, free_head) = META_STRUCT.unpack_from(raw, 0)
        if magic != META_MAGIC:
            raise CorruptionError("bad meta page magic")
        if page_size != self.page_size:
            raise CorruptionError(
                f"page size mismatch: file={page_size} expected={self.page_size}"
            )
        meta = Meta(page_size, key_size, value_size, key_kind, value_kind)
        meta.root_pid = root_pid
        meta.page_count = page_count
        meta.free_head = free_head
        return meta

    def has_meta(self):
        if self._mem is not None:
            return META_PAGE_ID in self._mem
        self._file.seek(0, os.SEEK_END)
        return self._file.tell() >= self.page_size

    # -- raw page io ----------------------------------------------------

    def read_raw(self, pid):
        if pid < 0 or pid >= 2 ** 31:
            raise CorruptionError(f"invalid page id {pid}")
        if self._mem is not None:
            page = self._mem.get(pid)
            if page is None:
                raise CorruptionError(f"page {pid} does not exist")
            return page
        self._file.seek(pid * self.page_size)
        data = self._file.read(self.page_size)
        if len(data) != self.page_size:
            raise CorruptionError(f"page {pid} is truncated")
        return data

    def write_raw(self, pid, data):
        if len(data) != self.page_size:
            raise ValueError("page data must be exactly page_size bytes")
        if self._mem is not None:
            self._mem[pid] = bytes(data)
            return
        self._file.seek(pid * self.page_size)
        self._file.write(data)

    # -- allocation -----------------------------------------------------

    def allocate(self, meta):
        if meta.free_head != NO_PAGE:
            pid = meta.free_head
            raw = self.read_raw(pid)
            (meta.free_head,) = struct.unpack_from("<q", raw, 0)
            return pid
        pid = meta.page_count
        meta.page_count += 1
        return pid

    def free(self, meta, pid):
        buf = bytearray(self.page_size)
        struct.pack_into("<q", buf, 0, meta.free_head)
        self.write_raw(pid, bytes(buf))
        meta.free_head = pid

    # -- lifecycle ------------------------------------------------------

    def sync(self):
        if self._file is not None:
            self._file.flush()
            os.fsync(self._file.fileno())

    def close(self):
        if self._file is not None:
            self.sync()
            self._file.close()
            self._file = None
