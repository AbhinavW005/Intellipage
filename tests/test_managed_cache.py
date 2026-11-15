from pathlib import Path

from intellipage.managed_cache import ManagedFileCache
from intellipage.policies import FIFOPolicy, LRUPolicy


def _files(root: Path) -> list[Path]:
    paths = []
    for name in ("a", "b", "c"):
        path = root / f"{name}.bin"
        path.write_bytes((name * 64).encode())
        paths.append(path)
    return paths


def test_managed_cache_performs_real_reads_and_records_hits(tmp_path: Path) -> None:
    a, b, c = _files(tmp_path)
    cache = ManagedFileCache(LRUPolicy(), capacity=2)
    assert cache.access(a) is False
    assert cache.access(b) is False
    assert cache.access(a) is True
    assert cache.access(c) is False
    assert cache.hits == 1
    assert cache.misses == 3
    assert cache.bytes_read == 4 * 64
    assert cache.evictions == ["b.bin"]


def test_fifo_eviction_is_visible(tmp_path: Path) -> None:
    a, b, c = _files(tmp_path)
    cache = ManagedFileCache(FIFOPolicy(), capacity=2)
    for path in (a, b, a, c):
        cache.access(path)
    assert cache.evictions == ["a.bin"]
