"""Exception types for the B+ tree package."""


class BPlusTreeError(Exception):
    """Base class for all B+ tree errors."""


class DuplicateKeyError(BPlusTreeError):
    """Raised when inserting a key that already exists."""


class KeyNotFoundError(BPlusTreeError, KeyError):
    """Raised when updating/deleting a key that does not exist."""


class CorruptionError(BPlusTreeError):
    """Raised when a page or the file header fails integrity checks."""


class IntegrityError(BPlusTreeError):
    """Raised by verify() when the tree structure is inconsistent."""
