"""
cache.py — TTL 价格缓存
借鉴 TopTrenDev 的 Rust PriceCache 模式（Arc<RwLock<HashMap>>）
纯 Python 实现：线程安全 + TTL 自动过期
"""

import time
import threading
from typing import Optional, Dict, Any


class PriceCache:
    """
    线程安全的 TTL 价格缓存

    用法：
        cache = PriceCache(ttl=60)  # 60秒过期
        cache.set("btc_price", {"yes": 0.55, "no": 0.47})
        val = cache.get("btc_price")  # 有缓存且未过期时返回
    """

    def __init__(self, ttl: int = 60):
        self.ttl = ttl
        self._data: Dict[str, tuple[Any, float]] = {}  # key → (value, timestamp)
        self._lock = threading.RLock()
        self._hits  = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        """获取缓存值，未命中或已过期返回 None"""
        with self._lock:
            if key not in self._data:
                self._misses += 1
                return None
            val, ts = self._data[key]
            if time.time() - ts < self.ttl:
                self._hits += 1
                return val
            # 已过期，删除
            del self._data[key]
            self._misses += 1
            return None

    def set(self, key: str, val: Any):
        """写入缓存（带时间戳）"""
        with self._lock:
            self._data[key] = (val, time.time())

    def get_or_fetch(self, key: str, fetch_fn, *args, **kwargs) -> Any:
        """
        缓存读取模式：如果缓存存在直接返回，否则调用 fetch_fn 获取
        用法：price = cache.get_or_fetch("btc_jun", api.fetch_price, "btc_jun")
        """
        cached = self.get(key)
        if cached is not None:
            return cached
        val = fetch_fn(*args, **kwargs)
        if val is not None:
            self.set(key, val)
        return val

    def clear(self):
        """清空所有缓存"""
        with self._lock:
            self._data.clear()

    def stats(self) -> Dict[str, Any]:
        """缓存命中率统计"""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0.0
            return {
                "hits":   self._hits,
                "misses": self._misses,
                "total":  total,
                "hit_rate": round(hit_rate, 3),
                "size":   len(self._data),
                "ttl":    self.ttl,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __repr__(self) -> str:
        s = self.stats()
        return (f"PriceCache(hits={s['hits']}, misses={s['misses']}, "
                f"hit_rate={s['hit_rate']:.1%}, size={s['size']})")


# ── 全局缓存实例（供各模块共享）───────────────────────────────────
# 在 main.py 初始化时创建，通过参数传递或模块级单例
_global_cache: Optional[PriceCache] = None
_cache_lock = threading.Lock()


def get_cache(ttl: int = 60) -> PriceCache:
    global _global_cache
    with _cache_lock:
        if _global_cache is None:
            _global_cache = PriceCache(ttl=ttl)
        return _global_cache


if __name__ == "__main__":
    # 简单测试
    cache = PriceCache(ttl=2)  # 2秒 TTL
    cache.set("test_key", {"price": 123})
    print("读取缓存:", cache.get("test_key"))  # 有
    print("统计:", cache.stats())
    time.sleep(3)
    print("3秒后读取:", cache.get("test_key"))  # 无（已过期）
    print("统计:", cache.stats())
