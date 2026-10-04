import random
import struct

import pytest

from bplustree import BPlusTree, CorruptionError
from bplustree.nodes import decode_node, encode_node
from bplustree.pager import NO_PAGE

PAGE = 256  # small pages force splits/merges with few keys


def make_tree(tmp_path, name="t.db", **kw):
    kw.setdefault("page_size", PAGE)
    return BPlusTree.create(str(tmp_path / name), **kw)


def assert_ok(tree):
    problems = tree.check_integrity()
    assert problems == [], f"integrity problems: {problems}"


# ---------------------------------------------------------------- inserts


def test_sequential_insert_and_search(tmp_path):
    tree = make_tree(tmp_path)
    for i in range(500):
        tree.insert(i, i * 10)
    for i in range(500):
        assert tree.search(i) == i * 10
    assert tree.search(500) is None
    assert tree.search(-1) is None
    assert_ok(tree)


def test_random_insert(tmp_path):
    tree = make_tree(tmp_path)
    keys = list(range(1000))
    random.Random(42).shuffle(keys)
    for k in keys:
        tree.insert(k, k + 1)
    for k in range(1000):
        assert tree.search(k) == k + 1
    assert_ok(tree)


def test_multi_level_split(tmp_path):
    tree = make_tree(tmp_path)
    for i in range(3000):
        tree.insert(i, i)
    assert tree.depth() >= 3  # root split at least twice
    assert [k for k, _ in tree.items()] == list(range(3000))
    assert_ok(tree)


def test_descending_insert(tmp_path):
    tree = make_tree(tmp_path)
    for i in range(800, 0, -1):
        tree.insert(i, i)
    assert [k for k, _ in tree.items()] == list(range(1, 801))
    assert_ok(tree)


# ------------------------------------------------------------- duplicates


def test_duplicate_key_rejected(tmp_path):
    tree = make_tree(tmp_path)
    tree.insert(7, 70)
    with pytest.raises(KeyError):
        tree.insert(7, 700)
    assert tree.search(7) == 70  # original value untouched


def test_update_existing_key(tmp_path):
    tree = make_tree(tmp_path)
    for i in range(100):
        tree.insert(i, i)
    tree.update(50, 999)
    assert tree.search(50) == 999
    with pytest.raises(KeyError):
        tree.update(10_000, 1)
    assert_ok(tree)


def test_delete_missing_key(tmp_path):
    tree = make_tree(tmp_path)
    tree.insert(1, 1)
    with pytest.raises(KeyError):
        tree.delete(2)


# ---------------------------------------------------------------- deletes


def test_delete_with_merge_and_redistribution(tmp_path):
    tree = make_tree(tmp_path)
    for i in range(1000):
        tree.insert(i, i)
    assert_ok(tree)
    # delete a shuffled 90% of the keys, checking integrity along the way
    keys = list(range(1000))
    random.Random(7).shuffle(keys)
    for n, k in enumerate(keys[:900]):
        tree.delete(k)
        if n % 100 == 0:
            assert_ok(tree)
    remaining = sorted(keys[900:])
    assert [k for k, _ in tree.items()] == remaining
    for k in remaining:
        assert tree.search(k) == k
    assert_ok(tree)


def test_delete_all_shrinks_root(tmp_path):
    tree = make_tree(tmp_path)
    for i in range(2000):
        tree.insert(i, i)
    deep = tree.depth()
    assert deep >= 3
    for i in range(2000):
        tree.delete(i)
    assert tree.depth() == 1  # root collapsed back to a single leaf
    assert list(tree.items()) == []
    assert_ok(tree)
    # tree is still usable after full shrink
    for i in range(300):
        tree.insert(i, i * 2)
    assert [v for _, v in tree.items()] == [i * 2 for i in range(300)]
    assert_ok(tree)


def test_delete_sequential_forward_and_reverse(tmp_path):
    for order in (list(range(600)), list(range(599, -1, -1))):
        tree = make_tree(tmp_path, name=f"t{order[0]}.db")
        for i in range(600):
            tree.insert(i, i)
        for k in order[:400]:
            tree.delete(k)
        assert [k for k, _ in tree.items()] == sorted(order[400:])
        assert_ok(tree)


# ------------------------------------------------------------- range scan


def test_range_scan_intervals(tmp_path):
    tree = make_tree(tmp_path)
    for i in range(0, 2000, 2):  # even keys only
        tree.insert(i, i)
    # closed interval
    got = [k for k, _ in tree.range_scan(100, 200)]
    assert got == list(range(100, 201, 2))
    # open interval: excludes both endpoints
    got = [k for k, _ in tree.range_scan(100, 200, include_low=False,
                                         include_high=False)]
    assert got == list(range(102, 200, 2))
    # half-open
    got = [k for k, _ in tree.range_scan(100, 200, include_high=False)]
    assert got == list(range(100, 200, 2))
    # bounds falling between existing keys
    got = [k for k, _ in tree.range_scan(101, 199)]
    assert got == list(range(102, 200, 2))
    # unbounded scans
    assert len(list(tree.range_scan(low=1900))) == 50
    assert len(list(tree.range_scan(high=99))) == 50
    # empty / invalid ranges
    assert list(tree.range_scan(200, 100)) == []
    assert list(tree.range_scan(101, 101, include_low=False)) == []
    assert list(tree.range_scan(1, 1)) == []  # key 1 does not exist


def test_range_scan_strictly_ordered_no_duplicates(tmp_path):
    tree = make_tree(tmp_path)
    keys = random.Random(3).sample(range(10_000), 3000)
    for k in keys:
        tree.insert(k, k)
    got = [k for k, _ in tree.range_scan(2000, 8000)]
    expected = sorted(k for k in keys if 2000 <= k <= 8000)
    assert got == expected
    assert len(got) == len(set(got))


# ------------------------------------------------------------- persistence


def test_reload_preserves_tree_and_results(tmp_path):
    path = str(tmp_path / "reload.db")
    tree = BPlusTree.create(path, page_size=PAGE)
    keys = random.Random(9).sample(range(5000), 1500)
    for k in keys:
        tree.insert(k, k * 3)
    for k in keys[:500]:
        tree.delete(k)
    tree.close()

    reopened = BPlusTree.open(path, page_size=PAGE)
    expected = sorted(keys[500:])
    assert [k for k, _ in reopened.items()] == expected
    for k in expected[:50]:
        assert reopened.search(k) == k * 3
    assert reopened.search(keys[0]) is None
    assert_ok(reopened)
    # still writable after reload
    reopened.insert(999_999, 1)
    assert reopened.search(999_999) == 1
    reopened.close()


def test_in_memory_tree():
    tree = BPlusTree.create(page_size=PAGE)
    for i in range(300):
        tree.insert(i, i)
    assert tree.search(250) == 250
    assert_ok(tree)


# ------------------------------------------------------------- corruption


def test_corrupted_page_detected(tmp_path):
    path = str(tmp_path / "corrupt.db")
    tree = BPlusTree.create(path, page_size=PAGE)
    for i in range(500):
        tree.insert(i, i)
    tree.close()

    with open(path, "r+b") as f:
        f.seek(3 * PAGE + 40)  # scribble inside data page 3
        f.write(b"\xff\xff\xff\xff")

    reopened = BPlusTree.open(path, page_size=PAGE)
    # the damaged page fails checksum validation on load
    with pytest.raises(CorruptionError):
        reopened._load(3)
    # normal operations that traverse the page also surface the error
    with pytest.raises(CorruptionError):
        for k in range(500):
            reopened.search(k)


def test_integrity_reports_broken_leaf_chain(tmp_path):
    path = str(tmp_path / "chain.db")
    tree = BPlusTree.create(path, page_size=PAGE)
    for i in range(300):
        tree.insert(i, i)
    assert_ok(tree)

    # corrupt the next-pointer of the leftmost leaf, then re-encode with a
    # valid checksum so only the *structure* is wrong
    leaf = tree._leftmost_leaf()
    assert leaf.next != NO_PAGE
    leaf.next = NO_PAGE
    tree._store(leaf)

    problems = tree.check_integrity()
    assert any("leaf chain" in p for p in problems)


def test_integrity_reports_bad_parent_pointer(tmp_path):
    path = str(tmp_path / "parent.db")
    tree = BPlusTree.create(path, page_size=PAGE)
    for i in range(300):
        tree.insert(i, i)
    leaf = tree._leftmost_leaf()
    leaf.parent = 12345
    tree._store(leaf)
    problems = tree.check_integrity()
    assert any("parent pointer" in p for p in problems)


def test_open_garbage_file_rejected(tmp_path):
    path = tmp_path / "garbage.db"
    path.write_bytes(b"\x00" * PAGE * 2)
    with pytest.raises(CorruptionError):
        BPlusTree.open(str(path), page_size=PAGE)


# --------------------------------------------------------------- key types


def test_fixed_length_bytes_keys(tmp_path):
    tree = make_tree(tmp_path, key_kind="bytes", key_size=16,
                     value_kind="bytes", value_size=8)
    names = [f"user-{i:04d}".encode() for i in range(400)]
    random.Random(1).shuffle(names)
    for n in names:
        tree.insert(n, b"v" + n[-4:])
    for n in names:
        assert tree.search(n) == b"v" + n[-4:]
    got = [k for k, _ in tree.range_scan(b"user-0100", b"user-0109")]
    assert got == [f"user-{i:04d}".encode() for i in range(100, 110)]
    assert_ok(tree)


def test_negative_and_large_int_keys(tmp_path):
    tree = make_tree(tmp_path)
    keys = [-(2**40), -7, 0, 7, 2**40]
    for k in keys:
        tree.insert(k, k)
    assert [k for k, _ in tree.items()] == sorted(keys)
    assert tree.search(-(2**40)) == -(2**40)
    assert_ok(tree)
