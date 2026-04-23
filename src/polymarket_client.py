"""
polymarket_client.py — Polymarket API 客户端
借鉴 TopTrenDev 的 Rust 客户端设计 + Polymarket 官方 agent-skills
支持 Gamma REST / CLOB REST / GraphQL 三路数据 + 自动降级
"""

import time
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

import requests

try:
    from .cache import PriceCache
except ImportError:
    from cache import PriceCache
try:
    from .event_matcher import Event
except ImportError:
    from event_matcher import Event

logger = logging.getLogger("polymarket_client")


# ── 数据模型 ──────────────────────────────────────────────────────
@dataclass
class PMMarket:
    """Polymarket 市场"""
    market_id:   str
    slug:        str
    question:    str
    description: str = ""
    end_date:    str = ""
    category:    str = ""
    yes_price:   float = 0.0
    no_price:    float = 0.0
    volume_usd:  float = 0.0
    liquidity:   float = 0.0
    resolved:    bool = False
    ts:          str = ""

    def to_event(self) -> Event:
        return Event(
            market_id  = self.market_id,
            question   = self.question,
            description = self.description,
            end_date   = self.end_date,
            category   = self.category,
            yes_price  = self.yes_price,
            no_price   = self.no_price,
            volume_usd = self.volume_usd,
            source     = "polymarket",
            url        = f"https://polymarket.com/market/{self.slug or self.market_id}",
            ts         = self.ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )


# ── 客户端 ─────────────────────────────────────────────────────────
class PolymarketClient:
    """
    Polymarket API 客户端

    三路数据源（按优先级降级）：
      1. Gamma REST API（市场列表，高效）
      2. CLOB REST API（订单簿详细）
      3. CLOB GraphQL（灵活查询）

    内置 TTL 缓存（借鉴 TopTrenDev）：避免重复请求
    """

    def __init__(
        self,
        gamma_url:  str = "https://gamma-api.polymarket.com",
        clob_url:  str = "https://clob.polymarket.com",
        cache_ttl:  int = 60,
        timeout:    int = 15,
        max_retries: int = 3,
    ):
        self.gamma_url   = gamma_url
        self.clob_url    = clob_url
        self.timeout     = timeout
        self.max_retries = max_retries
        self.cache       = PriceCache(ttl=cache_ttl)

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; PolymarketScanner/2.0)",
            "Accept": "application/json",
        })

        # 请求计数（统计）
        self._req_count = 0

    # ── 低层 HTTP ────────────────────────────────────────────────
    def _get(self, url: str, params: dict = None, retries: int = None) -> Optional[dict]:
        retries = retries or self.max_retries
        for attempt in range(retries):
            try:
                r = self._session.get(url, params=params, timeout=self.timeout)
                self._req_count += 1
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                if attempt < retries - 1:
                    time.sleep(1 * (attempt + 1))
                else:
                    logger.warning(f"[PolymarketClient] GET {url} 失败: {e}")
        return None

    # ── 公开 API ─────────────────────────────────────────────────
    def fetch_markets_gamma(self, limit: int = 200) -> List[PMMarket]:
        """通过 Gamma REST API 获取市场列表（主要数据源）"""
        cache_key = f"gamma_markets_{limit}"
        cached = self.cache.get(cache_key)
        if cached:
            logger.info(f"[Gamma API] 命中缓存，返回 {len(cached)} 个市场")
            return cached

        url = f"{self.gamma_url}/markets"
        data = self._get(url, params={"limit": limit, "closed": "false"})

        if not data:
            return []

        markets = data if isinstance(data, list) else data.get("data", []) or data.get("markets", [])
        result = [self._parse_market(m, "gamma") for m in markets]
        result = [m for m in result if m is not None and m.volume_usd >= 1_000]

        self.cache.set(cache_key, result)
        logger.info(f"[Gamma API] 获取 {len(result)} 个有效市场")
        return result

    def fetch_markets_clob(self, limit: int = 200) -> List[PMMarket]:
        """通过 CLOB REST API 获取市场列表"""
        cache_key = f"clob_markets_{limit}"
        cached = self.cache.get(cache_key)
        if cached:
            return cached

        url = f"{self.clob_url}/markets"
        data = self._get(url, params={"limit": limit})
        if not data:
            return []

        markets = data if isinstance(data, list) else data.get("data", [])
        result = [self._parse_market(m, "clob") for m in markets]
        result = [m for m in result if m is not None and m.volume_usd >= 1_000]

        self.cache.set(cache_key, result)
        return result

    def fetch_markets_graphql(self, limit: int = 200) -> List[PMMarket]:
        """通过 CLOB GraphQL API 获取市场"""
        query = """
        {
          markets(
            limit: %d
            closed: false
            archived: false
            orderBy: "volume"
            order: "desc"
          ) {
            id slug question description endDateExpanded
            volume liquidity outcomePrices closed
          }
        }
        """ % limit

        data = self._get(f"{self.clob_url}/graphql", params={"query": query})
        # GraphQL POST，需要用 JSON body
        try:
            r = self._session.post(
                f"{self.clob_url}/graphql",
                json={"query": query},
                timeout=self.timeout
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning(f"[GraphQL] 请求失败: {e}")
            return []

        markets = data.get("data", {}).get("markets", []) or []
        result = [self._parse_market(m, "graphql") for m in markets]
        result = [m for m in result if m is not None]
        return result

    def fetch_orderbook(self, market_id: str) -> Optional[Dict[str, Any]]:
        """获取订单簿（计算实际买卖价格）"""
        cache_key = f"orderbook_{market_id}"
        cached = self.cache.get(cache_key)
        if cached:
            return cached

        try:
            r = self._session.get(
                f"{self.clob_url}/book",
                params={"token_id": market_id},
                timeout=self.timeout
            )
            r.raise_for_status()
            data = r.json()
            self.cache.set(cache_key, data)
            return data
        except Exception as e:
            logger.warning(f"[Orderbook] 获取失败 {market_id}: {e}")
            return None

    def get_best_price(self, market_id: str) -> Optional[Dict[str, float]]:
        """获取最佳买/卖价格"""
        book = self.fetch_orderbook(market_id)
        if not book:
            return None

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        return {
            "best_bid": float(bids[0]["price"]) if bids else None,
            "best_ask": float(asks[0]["price"]) if asks else None,
            "spread": (
                float(asks[0]["price"]) - float(bids[0]["price"])
                if bids and asks else None
            ),
        }

    # ── 统一采集入口（自动降级）───────────────────────────────────
    def collect_all(self, limit: int = 200) -> List[PMMarket]:
        """
        统一采集入口，按优先级降级
        1. Gamma REST（主要）
        2. CLOB REST（备用）
        3. GraphQL（最后尝试）
        """
        for fn in [
            lambda: self.fetch_markets_gamma(limit),
            lambda: self.fetch_markets_clob(limit),
            self.fetch_markets_graphql,
        ]:
            markets = fn()
            if markets:
                return markets

        logger.error("[PolymarketClient] 所有 API 均不可用")
        return []

    # ── 内部解析 ──────────────────────────────────────────────────
    def _parse_market(self, m: dict, source: str) -> Optional[PMMarket]:
        try:
            prices_raw = m.get("outcomePrices", "")
            if isinstance(prices_raw, str):
                prices = [float(x) for x in prices_raw.strip("[]\" \n").split(",") if x.strip()]
            elif isinstance(prices_raw, list):
                prices = [float(x) for x in prices_raw]
            else:
                return None

            if len(prices) < 2:
                return None

            yes_p = prices[0]
            no_p  = prices[1]
            if yes_p <= 0 or no_p <= 0:
                return None

            return PMMarket(
                market_id   = m.get("id", ""),
                slug        = m.get("slug", ""),
                question    = m.get("question", "")[:200],
                description = m.get("description", "")[:500],
                end_date    = m.get("endDateExpanded", "") or m.get("end_date", ""),
                category    = m.get("category", "") or m.get("tags", ""),
                yes_price   = round(yes_p, 4),
                no_price    = round(no_p, 4),
                volume_usd  = round(float(m.get("volume") or 0), 0),
                liquidity   = round(float(m.get("liquidity") or 0), 0),
                resolved    = bool(m.get("closed") or m.get("resolved")),
                ts          = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
        except (ValueError, TypeError) as e:
            logger.debug(f"[Parse] 市场解析失败: {e}")
            return None

    def stats(self) -> Dict[str, Any]:
        return {
            "requests": self._req_count,
            "cache":    self.cache.stats(),
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    client = PolymarketClient(cache_ttl=60)
    markets = client.collect_all(limit=20)
    print(f"\n获取 {len(markets)} 个市场:")
    for m in markets[:5]:
        print(f"  [{m.market_id[:12]}] Yes={m.yes_price:.2%} No={m.no_price:.2%} Vol=${m.volume_usd:,.0f}")
        print(f"    Q: {m.question[:60]}")
    print("\n缓存统计:", client.stats()["cache"])
