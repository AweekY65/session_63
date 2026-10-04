import os
import random
import struct

import pytest

from bplustree import (BPlusTree, CorruptionError, DuplicateKeyError,
                       KeyNotFoundError)


def pack(v):
    return struct.pack("<q", v)


def unpack(b):
    return struct.unpack("<q", b)[0]


@pytest.fixture
def tree(tmp_path):
    t = BPlusTree.open(str(tmp_path / "idx.db"), page_size=256, value_size=8)
    yield t
    t.close()


# ------------------------------------------------------------------ insert

def test_sequential_insert_and_search(tree):
    n = 500
    for i in range(n):
        tree.insert(i, pack(i * 10))
    assert len(tree) == n
    for i in range(n):
        assert unpack(tree.search(i)) == i * 10
    assert tree.search(n) is None
    assert tree.verify()


def test_random_insert(tree):
    keys = list(range(1000))
    random.Random(42).shuffle(keys)
    for k in keys:
        tree.insert(k, pack(k))
    random.Random(7).shuffle(keys)
    for k in keys:
        assert unpack(tree.search(k)) == k
    assert tree.verify()


def test_multi_level_split(tree):
    # page_size=256 -> tiny fanout, 3000 keys must produce >= 3 levels.
    for i in range(3000):
        tree.insert(i, pack(i))
    assert tree.height() >= 3
    assert len(tree) == 3000
    for i in range(0, 3000, 7):
        assert unpack(tree.search(i)) == i
    assert tree.verify()


def test_negative_and_boundary_int_keys(tree):
    keys = [0, 1, -1, 2**62, -(2**62), 123456789, -987654321]
    random.Random(1).shuffle(keys)
    for k in keys:
        tree.insert(k, pack(k))
    for k in keys:
        assert unpack(tree.search(k)) == k
    assert [k for k, _ in tree.items()] == sorted(keys)
    assert tree.verify()


def test_fixed_length_bytes_keys(tmp_path):
    t = BPlusTree.open(str(tmp_path / "b.db"), page_size=256,
                       key_kind="bytes", key_size=16, value_size=8)
    keys = [f"key-{i:04d}".encode() for i in range(300)]
    random.Random(3).shuffle(keys)
    for k in keys:
        t.insert(k, pack(len(k)))
    for k in keys:
        assert unpack(t.search(k)) == len(k)
    assert [k for k, _ in t.items()] == sorted(keys)
    assert t.verify()
    t.close()


# ------------------------------------------------------------------ update

def test_update(tree):
    for i in range(200):
        tree.insert(i, pack(i))
    for i in range(0, 200, 2):
        tree.update(i, pack(-i))
    for i in range(200):
        expect = -i if i % 2 == 0 else i
        assert unpack(tree.search(i)) == expect
    with pytest.raises(KeyNotFoundError):
        tree.update(9999, pack(0))
    assert len(tree) == 200
    assert tree.verify()


# ------------------------------------------------------------------ delete

def test_delete_with_merge_and_redistribution(tree):
    n = 2000
    for i in range(n):
        tree.insert(i, pack(i))
    assert tree.verify()
    # Delete a random 90%; verify structure periodically.
    keys = list(range(n))
    random.Random(9).shuffle(keys)
    removed = keys[: int(n * 0.9)]
    for i, k in enumerate(removed):
        tree.delete(k)
        if i % 100 == 0:
            assert tree.verify()
    assert tree.verify()
    remaining = sorted(set(range(n)) - set(removed))
    assert len(tree) == len(remaining)
    assert [k for k, _ in tree.items()] == remaining
    for k in remaining:
        assert unpack(tree.search(k)) == k
    for k in removed:
        assert tree.search(k) is None


def test_delete_all_shrinks_root(tree):
    for i in range(1000):
        tree.insert(i, pack(i))
    assert tree.height() >= 3
    for i in range(1000):
        tree.delete(i)
    assert len(tree) == 0
    assert tree.height() == 0
    assert tree.verify()
    # Tree is reusable after being emptied.
    for i in range(100):
        tree.insert(i, pack(i))
    assert [k for k, _ in tree.items()] == list(range(100))
    assert tree.verify()


def test_delete_missing_key(tree):
    tree.insert(1, pack(1))
    with pytest.raises(KeyNotFoundError):
        tree.delete(2)
    with pytest.raises(KeyNotFoundError):
        BPlusTree.in_memory(page_size=256).delete(1)


def test_delete_forward_and_reverse(tree):
    # Deleting in sorted / reverse-sorted order stresses merges on both sides.
    for order in (range(800), range(799, -1, -1)):
        t = BPlusTree.in_memory(page_size=256)
        for i in range(800):
            t.insert(i, pack(i))
        for i in order:
            t.delete(i)
        assert len(t) == 0
        assert t.verify()


# ------------------------------------------------------------- range scan

def test_range_scan_closed_and_open(tree):
    n = 1000
    for i in range(n):
        tree.insert(i, pack(i))
    # Closed interval.
    got = [k for k, _ in tree.range_scan(100, 200)]
    assert got == list(range(100, 201))
    # Open interval.
    got = [k for k, _ in tree.range_scan(100, 200, lo_inclusive=False,
                                         hi_inclusive=False)]
    assert got == list(range(101, 200))
    # Half-open.
    got = [k for k, _ in tree.range_scan(0, 10, hi_inclusive=False)]
    assert got == list(range(10))
    # Open-ended scans.
    assert [k for k, _ in tree.range_scan(995, None)] == list(range(995, n))
    assert [k for k, _ in tree.range_scan(None, 2)] == [0, 1, 2]
    # Bounds that are not existing keys.
    got = [k for k, _ in tree.range_scan(100, 200)]
    assert got == sorted(got) and len(got) == len(set(got))
    # Empty range.
    assert list(tree.range_scan(500, 400)) == []
    # Full scan strictly ordered, no duplicates, no gaps.
    got = [k for k, _ in tree.range_scan()]
    assert got == list(range(n))


def test_range_scan_after_deletes(tree):
    for i in range(500):
        tree.insert(i, pack(i))
    for i in range(0, 500, 3):
        tree.delete(i)
    expect = [i for i in range(500) if i % 3 != 0 and 50 <= i <= 250]
    got = [k for k, _ in tree.range_scan(50, 250)]
    assert got == expect
    assert tree.verify()


# -------------------------------------------------------- duplicate policy

def test_duplicate_key_rejected(tree):
    tree.insert(42, pack(1))
    with pytest.raises(DuplicateKeyError):
        tree.insert(42, pack(2))
    # Failed insert must not corrupt state.
    assert unpack(tree.search(42)) == 1
    assert len(tree) == 1
    tree.update(42, pack(2))
    assert unpack(tree.search(42)) == 2
    assert tree.verify()


def test_duplicate_insert_stress(tree):
    keys = list(range(300))
    random.Random(5).shuffle(keys)
    for k in keys:
        tree.insert(k, pack(k))
    for k in keys:
        with pytest.raises(DuplicateKeyError):
            tree.insert(k, pack(k))
    assert len(tree) == 300
    assert tree.verify()


# ------------------------------------------------------------------ reload

def test_reload_from_file(tmp_path):
    path = str(tmp_path / "idx.db")
    t = BPlusTree.open(path, page_size=256, value_size=8)
    keys = list(range(1500))
    random.Random(11).shuffle(keys)
    for k in keys:
        t.insert(k, pack(k * 2))
    for k in keys[:500]:
        t.delete(k)
    expect = sorted(keys[500:])
    t.close()

    t2 = BPlusTree.open(path)
    assert len(t2) == len(expect)
    assert [k for k, _ in t2.items()] == expect
    for k in expect[:100]:
        assert unpack(t2.search(k)) == k * 2
    assert [k for k, _ in t2.range_scan(600, 700)] == [
        k for k in expect if 600 <= k <= 700]
    assert t2.verify()
    # Still writable after reload.
    t2.insert(10**6, pack(1))
    assert unpack(t2.search(10**6)) == 1
    assert t2.verify()
    t2.close()


def test_reload_in_memory_pager_state(tmp_path):
    # In-memory trees simply keep working without any file.
    t = BPlusTree.in_memory(page_size=256)
    for i in range(300):
        t.insert(i, pack(i))
    assert len(t) == 300
    assert t.verify()
    t.close()


# -------------------------------------------------------------- corruption

def test_corrupted_page_detected(tmp_path):
    path = str(tmp_path / "idx.db")
    t = BPlusTree.open(path, page_size=256)
    for i in range(200):
        t.insert(i, pack(i))
    t.close()
    # Flip a byte inside a data page (page 5, offset 20).
    with open(path, "r+b") as f:
        f.seek(5 * 256 + 20)
        orig = f.read(1)
        f.seek(5 * 256 + 20)
        f.write(bytes([orig[0] ^ 0xFF]))
    t2 = BPlusTree.open(path)
    with pytest.raises(CorruptionError):
        list(t2.items())
    t2.close()


def test_corrupted_meta_detected(tmp_path):
    path = str(tmp_path / "idx.db")
    t = BPlusTree.open(path, page_size=256)
    for i in range(10):
        t.insert(i, pack(i))
    t.close()
    with open(path, "r+b") as f:
        f.seek(0)
        f.write(b"XXXXXXXX")
    with pytest.raises(CorruptionError):
        BPlusTree.open(path)


def test_verify_detects_structural_damage(tmp_path):
    # Corrupt the leaf-chain pointer directly through the pager internals,
    # then confirm verify() reports it (checksum stays valid).
    t = BPlusTree.open(str(tmp_path / "idx.db"), page_size=256)
    for i in range(300):
        t.insert(i, pack(i))
    leaf = t._find_leaf(0)
    leaf.next = 0  # break the chain
    t._store(leaf)
    with pytest.raises(Exception):
        t.verify()
    t.close()
