"""ipam：地址池分配内核（前缀切分、首次适配、释放合并与利用率统计）。"""

from .core import DEFAULT_WIDTH, AddressPool, IpamError, block_size

__all__ = ["DEFAULT_WIDTH", "AddressPool", "IpamError", "block_size"]
