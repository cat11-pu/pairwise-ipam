"""ipam.core 的验收测试。

覆盖按前缀切分、首次适配、释放回填与相邻块合并、预留的冲突与边界、
利用率统计，以及地址池本身的范围校验。
"""

import unittest

from ipam import AddressPool, IpamError, block_size

WIDTH = 32
BASE = 0
PREFIX = 24
SIZE = 256


def pool_of(base=BASE, prefix=PREFIX, width=WIDTH):
    """建一个缺省覆盖 0 .. 255 的池。"""
    return AddressPool(base, prefix, width)


def addresses_of(blocks, width=WIDTH):
    """把若干块摊成地址集合。"""
    addresses = set()
    for start, prefix in blocks:
        length = block_size(prefix, width)
        addresses.update(range(start, start + length))
    return addresses


class PoolTests(unittest.TestCase):
    def test_a_fresh_pool_is_one_whole_free_block(self):
        pool = pool_of()
        self.assertEqual(pool.size(), SIZE)
        self.assertEqual(pool.end(), SIZE - 1)
        self.assertEqual(pool.free_blocks(), [(BASE, PREFIX)])
        self.assertEqual(pool.allocated_blocks(), [])
        self.assertEqual(pool.reserved_blocks(), [])
        self.assertEqual(pool.free_addresses(), SIZE)
        self.assertEqual(pool.used_addresses(), 0)
        self.assertEqual(pool.utilization(), 0.0)
        self.assertTrue(pool.contains(BASE))
        self.assertTrue(pool.contains(SIZE - 1))
        self.assertFalse(pool.contains(SIZE))
        self.assertIsNone(pool.owner_of(SIZE // 2))
        other = AddressPool(1 << 16, 20, width=WIDTH)
        self.assertEqual(other.free_blocks(), [(1 << 16, 20)])
        self.assertEqual(other.end(), (1 << 16) + 4096 - 1)


class SplitTests(unittest.TestCase):
    def test_the_rest_of_a_split_block_stays_as_large_as_possible(self):
        pool = pool_of()
        self.assertEqual(pool.allocate(26, "a"), (0, 26))
        self.assertEqual(pool.allocated_blocks(), [(0, 26)])
        self.assertEqual(pool.free_blocks(), [(64, 26), (128, 25)])
        self.assertEqual(pool.allocate(25, "b"), (128, 25))
        self.assertEqual(pool.free_blocks(), [(64, 26)])
        self.assertEqual(pool.used_addresses(), 192)
        self.assertEqual(pool.free_addresses() + pool.used_addresses(), SIZE)

    def test_blocks_stay_aligned_and_never_overlap(self):
        pool = pool_of()
        got = [pool.allocate(25, "a"), pool.allocate(26, "b"),
               pool.allocate(27, "c"), pool.allocate(28, "d"),
               pool.allocate(28, "e")]
        self.assertEqual(got, [(0, 25), (128, 26), (192, 27),
                               (224, 28), (240, 28)])
        self.assertEqual(pool.free_blocks(), [])
        self.assertEqual(pool.allocated_blocks(), got)
        self.assertEqual(addresses_of(got), set(range(SIZE)))
        self.assertEqual(pool.used_addresses(), SIZE)
        self.assertEqual(pool.utilization(), 1.0)
        for start, prefix in got:
            self.assertEqual(start % block_size(prefix, WIDTH), 0, start)
        with self.assertRaises(IpamError):
            pool.allocate(28, "f")


class FirstFitTests(unittest.TestCase):
    def test_the_lowest_address_that_fits_is_used(self):
        pool = pool_of()
        for name in ("a", "b", "c"):
            pool.allocate(26, name)
        self.assertEqual(pool.free_blocks(), [(192, 26)])
        pool.release(64)
        self.assertEqual(pool.free_blocks(), [(64, 26), (192, 26)])
        self.assertEqual(pool.allocate(26, "d"), (64, 26))
        self.assertEqual(pool.allocate(26, "e"), (192, 26))
        self.assertEqual(pool.allocated_blocks(),
                         [(0, 26), (64, 26), (128, 26), (192, 26)])
        self.assertEqual(pool.free_blocks(), [])


class ReleaseTests(unittest.TestCase):
    def test_released_space_comes_back_and_merges(self):
        pool = pool_of()
        for name in ("a", "b", "c", "d"):
            pool.allocate(26, name)
        self.assertEqual(pool.free_blocks(), [])
        self.assertEqual(pool.release(128), (128, 26))
        self.assertEqual(pool.free_blocks(), [(128, 26)])
        self.assertEqual(pool.release(192), (192, 26))
        self.assertEqual(pool.free_blocks(), [(128, 25)])
        self.assertEqual(pool.release(64), (64, 26))
        self.assertEqual(pool.free_blocks(), [(64, 26), (128, 25)])
        self.assertEqual(pool.release(0), (0, 26))
        self.assertEqual(pool.free_blocks(), [(0, 24)])
        self.assertEqual(pool.allocated_blocks(), [])
        self.assertEqual(pool.free_addresses(), SIZE)


class ReleaseGuardTests(unittest.TestCase):
    def test_releasing_a_block_that_is_not_allocated_is_rejected(self):
        pool = pool_of()
        pool.allocate(25, "a")
        with self.assertRaises(IpamError):
            pool.release(128)
        self.assertEqual(pool.release(0), (0, 25))
        with self.assertRaises(IpamError):
            pool.release(0)
        self.assertEqual(pool.free_blocks(), [(0, 24)])


class ReserveTests(unittest.TestCase):
    def test_a_reservation_sits_inside_one_free_block(self):
        pool = pool_of()
        self.assertEqual(pool.reserve(64, 26, "gw"), (64, 26))
        self.assertEqual(pool.reserved_blocks(), [(64, 26)])
        self.assertEqual(pool.free_blocks(), [(0, 26), (128, 25)])
        self.assertEqual(pool.owner_of(100), "gw")
        self.assertIsNone(pool.owner_of(200))
        self.assertEqual(pool.reserved_addresses(), 64)
        self.assertEqual(pool.free_addresses(), 192)
        self.assertEqual(pool.allocate(25, "a"), (128, 25))
        self.assertEqual(pool.allocate(26, "b"), (0, 26))
        self.assertEqual(pool.free_blocks(), [])
        self.assertEqual(pool.used_addresses() + pool.reserved_addresses(), SIZE)


class ConflictTests(unittest.TestCase):
    def test_a_reservation_may_not_reach_into_allocated_addresses(self):
        pool = pool_of()
        for name in ("x", "a", "b", "c"):
            pool.allocate(26, name)
        self.assertEqual(pool.free_blocks(), [])
        self.assertEqual(pool.release(0), (0, 26))
        self.assertEqual(pool.free_blocks(), [(0, 26)])
        self.assertEqual(pool.allocated_blocks(),
                         [(64, 26), (128, 26), (192, 26)])
        with self.assertRaises(IpamError):
            pool.reserve(0, 25, "gw")
        self.assertEqual(pool.reserved_blocks(), [])
        self.assertEqual(pool.free_blocks(), [(0, 26)])


class BoundaryTests(unittest.TestCase):
    def test_a_pool_may_not_reach_past_the_address_space(self):
        with self.assertRaises(IpamError):
            AddressPool(1 << WIDTH, 8, width=WIDTH)
        with self.assertRaises(IpamError):
            AddressPool(1 << WIDTH, 0, width=WIDTH)
        pool = pool_of()
        self.assertTrue(pool.contains(SIZE - 1))
        self.assertFalse(pool.contains(SIZE))
        self.assertTrue(pool.contains(BASE))
        self.assertFalse(pool.contains(BASE - 1))
        with self.assertRaises(IpamError):
            pool.reserve(192, 25, "gw")
        self.assertEqual(pool.reserve(192, 26, "gw"), (192, 26))
        self.assertEqual(pool.free_blocks(), [(0, 25), (128, 26)])


class UtilizationTests(unittest.TestCase):
    def test_utilization_counts_allocated_addresses_only(self):
        pool = pool_of()
        self.assertEqual(pool.allocate(25, "a"), (0, 25))
        self.assertEqual(pool.reserve(128, 26, "gw"), (128, 26))
        self.assertEqual(pool.used_addresses(), 128)
        self.assertEqual(pool.reserved_addresses(), 64)
        self.assertEqual(pool.free_addresses(), 64)
        self.assertEqual(pool.utilization(), 0.5)
        self.assertEqual(pool.free_blocks(), [(192, 26)])
        self.assertEqual(pool.allocate(26, "b"), (192, 26))
        self.assertEqual(pool.utilization(), 0.75)


if __name__ == "__main__":
    unittest.main()
