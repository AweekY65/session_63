"""Local B+ tree index over fixed-size pages (file or memory backed)."""

from .pager import CorruptionError
from .tree import BPlusTree

__all__ = ["BPlusTree", "CorruptionError"]
