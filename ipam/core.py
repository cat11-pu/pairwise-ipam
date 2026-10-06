"""ipam.core —— 地址池分配内核（纯标准库，地址一律用整数表示）。

地址池由起始地址与前缀长度确定：起点按前缀对齐，池内有
2 ** (width - prefix) 个地址。池内的空闲空间按块登记，一个块写成
(起始地址, 前缀长度)，块长恒为 2 的幂，起始地址按块长对齐，空闲表恒按
起始地址升序排列；已分配与已预留的块分开登记。

分配走首次适配：从地址最低的空闲块开始，第一块放得下的就从前部切走，
剩下的空间拆成几个对齐块留在空闲表里。释放把块放回空闲表，并按伙伴关系
与相邻的空闲块合并。预留圈出不能分配的地址：预留块按块长对齐、完整落在
某一个空闲块里，预留成功后这段地址从空闲表里消失，既不算已分配也不算
空闲。利用率按已分配地址数除以池内地址总数计算。

内核不联网、不读写文件、不取时钟、不使用随机数，同一个调用序列永远得到
同样的结果。
"""

DEFAULT_WIDTH = 32


class IpamError(ValueError):
    """地址池操作相关的错误。"""


def block_size(prefix, width=DEFAULT_WIDTH):
    """前缀长度为 prefix 的块包含的地址数。"""
    return 1 << (width - prefix)


def _is_int(value):
    """判断是不是整数（布尔值不算）。"""
    return isinstance(value, int) and not isinstance(value, bool)


class AddressPool:
    """地址池：按前缀切分、首次适配分配、释放合并、预留与统计。"""

    def __init__(self, base, prefix, width=DEFAULT_WIDTH):
        if not _is_int(width) or width < 1:
            raise IpamError("地址位宽必须是正整数")
        if not _is_int(prefix) or prefix < 0 or prefix > width:
            raise IpamError("前缀长度必须落在 0 与位宽之间")
        if not _is_int(base) or base < 0:
            raise IpamError("起始地址必须是非负整数")
        span = block_size(prefix, width)
        if base > (1 << width):
            raise IpamError("池超出地址空间")
        if base % span:
            raise IpamError("起始地址必须按前缀对齐")
        self.width = width
        self.base = base
        self.prefix = prefix
        self._free = [(base, prefix)]
        self._allocated = {}
        self._reserved = {}

    # ------------------------------------------------------------ 基本信息
    def size(self):
        """池内地址总数。"""
        return block_size(self.prefix, self.width)

    def end(self):
        """池内最后一个地址。"""
        return self.base + self.size() - 1

    def contains(self, address):
        """判断地址是否落在池内。"""
        if not _is_int(address):
            raise IpamError("地址必须是整数")
        return self.base <= address <= self.end()

    def free_blocks(self):
        """按起始地址升序返回空闲块。"""
        return list(self._free)

    def allocated_blocks(self):
        """按起始地址升序返回已分配的块。"""
        return sorted((start, entry[0])
                      for start, entry in self._allocated.items())

    def reserved_blocks(self):
        """按起始地址升序返回预留的块。"""
        return sorted((start, entry[0])
                      for start, entry in self._reserved.items())

    def _lookup(self, table, address):
        """在登记表里找出覆盖该地址的块，返回它的标签。"""
        for start, (prefix, label) in table.items():
            length = block_size(prefix, self.width)
            if start <= address < start + length:
                return label
        return None

    def owner_of(self, address):
        """返回覆盖该地址的标签；既没分配也没预留时返回空。"""
        if not self.contains(address):
            raise IpamError("地址不在池内")
        found = self._lookup(self._allocated, address)
        if found is None:
            found = self._lookup(self._reserved, address)
        return found

    # ------------------------------------------------------------ 地址统计
    def used_addresses(self):
        """已分配的地址总数。"""
        return sum(block_size(prefix, self.width)
                   for prefix, label in self._allocated.values())

    def reserved_addresses(self):
        """预留的地址总数。"""
        return sum(block_size(prefix, self.width)
                   for prefix, label in self._reserved.values())

    def free_addresses(self):
        """空闲的地址总数。"""
        return sum(block_size(prefix, self.width)
                   for start, prefix in self._free)

    def utilization(self):
        """已分配地址数占池内地址总数的比例。"""
        busy = self.used_addresses() + self.reserved_addresses()
        return busy / self.size()

    # ------------------------------------------------------------ 参数校验
    def _require_label(self, label):
        """确认标签是非空字符串。"""
        if not isinstance(label, str) or not label:
            raise IpamError("标签必须是非空字符串")
        return label

    def _require_prefix(self, prefix):
        """确认前缀长度落在池允许的范围内。"""
        if not _is_int(prefix) or prefix < self.prefix or prefix > self.width:
            raise IpamError("前缀长度超出池允许的范围")
        return prefix

    def _require_start(self, start, prefix):
        """确认起始地址是整数，且按块长对齐。"""
        if not _is_int(start):
            raise IpamError("起始地址必须是整数")
        if start % block_size(prefix, self.width):
            raise IpamError("起始地址必须按块长对齐")
        return start

    # ------------------------------------------------------------ 分配
    def allocate(self, prefix, label):
        """按首次适配分配一个块，返回它的 (起始地址, 前缀长度)。"""
        self._require_prefix(prefix)
        self._require_label(label)
        for index in range(len(self._free) - 1, -1, -1):
            start, block_prefix = self._free[index]
            if block_prefix <= prefix:
                taken = self._take(index, prefix)
                self._allocated[taken[0]] = (prefix, label)
                return taken
        raise IpamError("池里没有放得下的空闲块")

    def _take(self, index, prefix):
        """从空闲表第 index 块的前部切走一块，返回被切走的块。"""
        start, block_prefix = self._free[index]
        length = block_size(prefix, self.width)
        rest = self._pieces(start + length,
                            start + block_size(block_prefix, self.width),
                            prefix)
        self._free[index:index + 1] = rest
        return (start, prefix)

    def _pieces(self, lo, hi, fine):
        """把 [lo, hi) 拆成若干对齐块，每块的前缀长度不超过 fine。"""
        pieces = []
        cursor = lo
        while cursor < hi:
            chosen = None
            for prefix in range(fine, self.prefix - 1, -1):
                length = block_size(prefix, self.width)
                if cursor % length == 0 and cursor + length <= hi:
                    chosen = (cursor, prefix)
                    break
            if chosen is None:
                break
            pieces.append(chosen)
            cursor += block_size(chosen[1], self.width)
        return pieces

    # ------------------------------------------------------------ 释放
    def release(self, start):
        """释放起始地址为 start 的已分配块，返回它的 (起始地址, 前缀长度)。"""
        entry = self._allocated.pop(start, None)
        if entry is None:
            return None
        prefix = entry[0]
        self._insert_free(start, prefix)
        self._coalesce()
        return (start, prefix)

    def _insert_free(self, start, prefix):
        """把一块空闲空间按起始地址升序插回空闲表。"""
        index = 0
        while index < len(self._free) and self._free[index][0] < start:
            index += 1
        self._free.insert(index, (start, self.prefix))

    def _coalesce(self):
        """把大小相同、地址相邻的伙伴块合成更大的块。"""
        index = 0
        while index + 1 < len(self._free):
            start, prefix = self._free[index]
            neighbour, neighbour_prefix = self._free[index + 1]
            if (prefix == neighbour_prefix and prefix > self.prefix
                    and start + block_size(prefix, self.width) == neighbour):
                self._free[index:index + 2] = [(start, prefix - 1)]
            index += 1

    # ------------------------------------------------------------ 预留
    def reserve(self, start, prefix, label):
        """把一块地址标为不可分配，返回它的 (起始地址, 前缀长度)。"""
        self._require_prefix(prefix)
        self._require_label(label)
        self._require_start(start, prefix)
        index = self._holder(start, prefix)
        if index is None:
            raise IpamError("预留块与已占用的地址冲突")
        block_start, block_prefix = self._free[index]
        length = block_size(prefix, self.width)
        fine = max(block_prefix, prefix)
        rest = self._pieces(block_start, start, fine)
        rest += self._pieces(start + length,
                             block_start + block_size(block_prefix, self.width),
                             fine)
        self._free[index:index + 1] = rest
        self._reserved[start] = (prefix, label)
        return (start, prefix)

    def _holder(self, start, prefix):
        """找出接管该块的空闲块下标；找不着合适的空闲块时返回空。"""
        length = block_size(prefix, self.width)
        for index, (block_start, block_prefix) in enumerate(self._free):
            block_end = block_start + block_size(block_prefix, self.width)
            if block_start <= start < block_end:
                return index
        return None
