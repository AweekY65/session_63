"""A local B+ tree index on fixed-size pages.

Pure Python + local file (or in-memory) storage. No SQLite, no RocksDB,
no database server, no external services.
"""

from .errors import (BPlusTreeError, CorruptionError, DuplicateKeyError,
                     IntegrityError, KeyNotFoundError)
from .tree import BPlusTree

__all__ = [
    "BPlusTree",
    "BPlusTreeError",
    "CorruptionError",
    "DuplicateKeyError",
    "IntegrityError",
    "KeyNotFoundError",
]

__version__ = "0.1.0"
