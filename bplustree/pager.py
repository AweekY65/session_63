"""Fixed-size page manager.

Pages are persisted either in a local file or purely in memory. No external
database or service is involved. Every page carries a CRC32 checksum in its
last 4 bytes so that on-disk corruption is detected on read.

File layout:
    page 0          : meta page (header, fixed struct, see META_STRUCT)
    page 1..N-1     : node pages or free pages

Free pages form a singly linked list; the first 8 bytes of a free page hold
the page id of the next free page (0 = end of list, page 0 is never freed).
"""

from __future__ import annotations

import os
import struct
import zlib

from .errors import CorruptionError

MAGIC = b"BPLUST01"
META_STRUCT = struct.Struct("<8sIIQQQQB3xII")
# magic, version, page_size, root_id, free_head, num_pages, num_entries,
# key_kind, key_size, value_size

VERSION = 1
KEY_KIND_INT = 0
KEY_KIND_BYTES = 1

FREE_PAGE_SENTINEL = 0


class Pager:
    """Reads/writes fixed-size pages by page id."""

    def __init__(self, path=None, page_size=4096):
        if page_size < 64:
            raise ValueError("page_size must be >= 64")
        self.path = path
        self.page_size = page_size
        self._closed = False
        if path is None:
            # Pure in-memory pager.
            self._file = None
            self._pages = {}
            self._init_meta()
        else:
            existed = os.path.exists(path) and os.path.getsize(path) > 0
            self._file = open(path, "r+b" if existed else "w+b")
            self._pages = None
            if existed:
                self._load_meta()
            else:
                self._init_meta()

    # ------------------------------------------------------------------ meta

    def _init_meta(self):
        self.root_id = 0  # 0 = empty tree
        self.free_head = FREE_PAGE_SENTINEL
        self.num_pages = 1  # page 0 is the meta page
        self.num_entries = 0
        self.key_kind = KEY_KIND_INT
        self.key_size = 8
        self.value_size = 8
        self._write_meta_page()

    def _load_meta(self):
        raw = self._read_raw(0)
        (magic, version, page_size, self.root_id, self.free_head,
         self.num_pages, self.num_entries, key_kind,
         self.key_size, self.value_size) = META_STRUCT.unpack(
            raw[:META_STRUCT.size])
        if magic != MAGIC:
            raise CorruptionError("bad magic in meta page: not a bplustree file")
        if version != VERSION:
            raise CorruptionError(f"unsupported version {version}")
        if page_size != self.page_size:
            # Trust the on-disk page size.
            self.page_size = page_size
        if key_kind not in (KEY_KIND_INT, KEY_KIND_BYTES):
            raise CorruptionError(f"invalid key kind {key_kind}")
        self.key_kind = key_kind

    def _write_meta_page(self):
        data = META_STRUCT.pack(
            MAGIC, VERSION, self.page_size, self.root_id, self.free_head,
            self.num_pages, self.num_entries, self.key_kind,
            self.key_size, self.value_size)
        self._write_raw(0, data)

    def save_meta(self):
        """Persist the current in-memory meta fields to page 0."""
        self._write_meta_page()

    # -------------------------------------------------------------- raw i/o

    def _read_raw(self, page_id):
        if self._file is None:
            try:
                return self._pages[page_id]
            except KeyError:
                raise CorruptionError(f"page {page_id} does not exist") from None
        self._file.seek(page_id * self.page_size)
        data = self._file.read(self.page_size)
        if len(data) != self.page_size:
            raise CorruptionError(
                f"short read on page {page_id}: got {len(data)} bytes")
        return data

    def _write_raw(self, page_id, data):
        if len(data) > self.page_size - 4:
            raise ValueError(
                f"page {page_id} payload too large: {len(data)} bytes")
        payload = data.ljust(self.page_size - 4, b"\x00")
        page = payload + struct.pack("<I", zlib.crc32(payload) & 0xFFFFFFFF)
        if self._file is None:
            self._pages[page_id] = page
        else:
            self._file.seek(page_id * self.page_size)
            self._file.write(page)
            self._file.flush()

    # ------------------------------------------------------------ public api

    def read_page(self, page_id):
        """Return the payload (page_size - 4 bytes) of a page, CRC-checked."""
        raw = self._read_raw(page_id)
        payload, checksum = raw[:-4], struct.unpack("<I", raw[-4:])[0]
        if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
            raise CorruptionError(f"checksum mismatch on page {page_id}")
        return payload

    def write_page(self, page_id, data):
        if page_id <= 0 or page_id >= self.num_pages:
            raise ValueError(f"invalid page id {page_id}")
        self._write_raw(page_id, data)

    def allocate_page(self):
        """Return a page id, reusing the free list when possible."""
        if self.free_head != FREE_PAGE_SENTINEL:
            page_id = self.free_head
            nxt = struct.unpack("<Q", self.read_page(page_id)[:8])[0]
            self.free_head = nxt
            self.save_meta()
            return page_id
        page_id = self.num_pages
        self.num_pages += 1
        self.save_meta()
        # Grow the file eagerly so reads of this page never short-read.
        self._write_raw(page_id, b"")
        return page_id

    def free_page(self, page_id):
        if page_id <= 0:
            raise ValueError("cannot free the meta page")
        payload = struct.pack("<Q", self.free_head)
        self._write_raw(page_id, payload)
        self.free_head = page_id
        self.save_meta()

    def flush(self):
        self.save_meta()
        if self._file is not None:
            self._file.flush()
            os.fsync(self._file.fileno())

    def close(self):
        if self._closed:
            return
        self.flush()
        if self._file is not None:
            self._file.close()
        self._closed = True
