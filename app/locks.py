"""PostgreSQL 事务咨询锁键工具（无扩展依赖）。"""
import hashlib


def combined_lock_key(namespace: int, key: str) -> int:
    """高 32 位命名空间 + 低 32 位字符串哈希，映射为 bigint 有符号数。"""
    n = int.from_bytes(
        hashlib.blake2b(key.encode(), digest_size=4).digest(), "big"
    )
    combined = (namespace << 32) | (n & 0xFFFFFFFF)
    return combined - (1 << 64) if combined >= (1 << 63) else combined


def combined_int_key(namespace: int, key: int) -> int:
    """高 32 位命名空间 + 低 32 位整数键，映射为 bigint 有符号数。"""
    n = ((namespace & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF)
    return n - (1 << 64) if n >= (1 << 63) else n


def hash_int_key(key: str) -> int:
    """字符串整体哈希为 bigint 有符号数（如付款流水号锁的低 32/64 位键）。"""
    digest = hashlib.blake2b(key.encode(), digest_size=8).digest()
    n = int.from_bytes(digest, "big", signed=False)
    return n - (1 << 64) if n >= (1 << 63) else n
