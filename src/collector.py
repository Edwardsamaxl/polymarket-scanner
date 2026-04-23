#!/usr/bin/env python3
"""
collector.py - 数据采集层
通过 web_search + extract_content 代替直接 API 调用
小木系统 / 2026-04-23
"""

import re
import json
import logging
from dataclasses import dataclass, asdict, field
from typing import List, Optional
from datetime import datetime, timezone

import requests

logger = logging.getLogger("collector")


# ── 数据模型 ────────────────────────────────────────────
@dataclass
class MarketSnapshot:
    """单个市场的快照数据"""
    market_id:      str = ""
    question:       str = ""
    yes_price:      float = 0.0   # 隐含 Yes 概率
    no_price:       float = 0.0   # 隐含 No 概率
    spread_pct:     float = 0.0   # Yes + No - 1（%）
    net_return_pct: float = 0.0   # 费后净收益率（%）
    volume_usd:     float = 0.0
    source:         str = ""       # 数据来源
    ts:             str = ""      # 采集时间
    url:            str = ""
    liquidity:      float = 0.0
    resolved:       bool = False

    def to_dict(self):
        d = asdict(self)
        d["ts"] = self.ts or datetime.now(timezone.utc).isoformat()
        return d


# ── 配置 ────────────────────────────────────────────────
POLYMARKET_FEE = 0.01   # Polymarket 每笔约 1% 手续费

# 热门市场页面（可抓取）
POLYMARKET_HOME = "https://polymarket.com"
POLYMARKET_API  = "https://gamma-api.polymarket.com"
CLOB_API        = "https://clob.polymarket.com"


# ── HTTP 客户端 ──────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html",
    "Accept-Language": "en-US,en;q=0.9",
})


# ── 方法1：直接 REST API（API 通时使用）──────────────────
def fetch_markets_via_rest_api(limit: int = 100) -> List[MarketSnapshot]:
    """
    通过 Polymarket REST API 获取市场列表
    API: GET https://gamma-api.polymarket.com/markets
    返回字段: id, question, volume, outcomePrices, liquidity, closed
    """
    snapshots = []
    try:
        resp = SESSION.get(
            f"{POLYMARKET_API}/markets",
            params={"limit": limit, "closed": "false"},
            timeout=10
        )
        resp.raise_for_status()
        markets = resp.json()
        if not isinstance(markets, list):
            markets = markets.get("data", []) or markets.get("markets", [])

        for m in markets:
            try:
                vol = float(m.get("volume") or 0)
                if vol < 5_000:
                    continue

                prices_raw = m.get("outcomePrices", "")
                if not prices_raw:
                    continue

                # 解析价格数组
                if isinstance(prices_raw, str):
                    prices = [float(x) for x in prices_raw.strip("[]\"\n ").split(",") if x.strip()]
                elif isinstance(prices_raw, list):
                    prices = [float(x) for x in prices_raw]
                else:
                    continue

                if len(prices) < 2:
                    continue

                yes_price = prices[0]
                no_price  = prices[1]
                if yes_price <= 0 or no_price <= 0:
                    continue

                total  = yes_price + no_price
                spread = total - 1.0
                net_yes = (1.0 - yes_price) * (1 - POLYMARKET_FEE)
                net_no  = (1.0 - no_price)  * (1 - POLYMARKET_FEE)
                net_return = min(net_yes, net_no)

                snap = MarketSnapshot(
                    market_id  = m.get("id", ""),
                    question   = m.get("question", "")[:200],
                    yes_price  = round(yes_price, 4),
                    no_price   = round(no_price, 4),
                    spread_pct = round(spread * 100, 3),
                    net_return_pct = round(net_return * 100, 3),
                    volume_usd = round(vol, 0),
                    liquidity  = round(float(m.get("liquidity") or 0), 0),
                    source     = "gamma_api",
                    url        = f"https://polymarket.com/market/{m.get('slug', m.get('id', ''))}",
                    ts         = datetime.now(timezone.utc).isoformat(),
                    resolved   = m.get("closed", False) or m.get("resolved", False),
                )
                snapshots.append(snap)
            except Exception as e:
                logger.debug(f"解析市场出错: {e}")
                continue

        logger.info(f"[REST API] 获取 {len(snapshots)} 个有效市场")
    except Exception as e:
        logger.warning(f"[REST API] 请求失败: {e}")

    return snapshots


# ── 方法2：网页抓取（API 不通时的降级方案）─────────────
def fetch_markets_via_scrape() -> List[MarketSnapshot]:
    """
    直接抓取 polymarket.com 热门市场页面
    解析 HTML 中的 JSON 数据
    """
    snapshots = []
    try:
        resp = SESSION.get(POLYMARKET_HOME, timeout=10)
        resp.raise_for_status()
        html = resp.text

        # 从 script 标签中提取 JSON 数据（Next.js 注入）
        patterns = [
            r'window\.MARKET_DATA\s*=\s*(\{.*?\});',
            r'"markets"\s*:\s*(\[.*?\])',
            r'data-markets\s*=\s*["\'](\{.*?\})["\']',
        ]

        for pat in patterns:
            match = re.search(pat, html, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                    markets = data if isinstance(data, list) else data.get("markets", [])
                    logger.info(f"[Web Scrape] 从 HTML 解析 {len(markets)} 个市场")
                    # 解析逻辑同 REST API
                    return [_parse_market_dict(m) for m in markets]
                except json.JSONDecodeError:
                    pass

        # 备用：从 meta 标签提取
        og_data = re.findall(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+>', html)
        logger.info(f"[Web Scrape] 获取 HTML 完成，OG 数据 {len(og_data)} 条")

    except Exception as e:
        logger.warning(f"[Web Scrape] 请求失败: {e}")

    return snapshots


# ── 方法3：Polymarket CLOB API───────────────────────────
def fetch_markets_via_clob(limit: int = 100) -> List[MarketSnapshot]:
    """
    通过 CLOB API 获取市场列表（需要订单簿）
    Endpoint: GET https://clob.polymarket.com/markets
    """
    snapshots = []
    try:
        resp = SESSION.get(
            f"{CLOB_API}/markets",
            params={"limit": limit, "closed": "false"},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        markets = data if isinstance(data, list) else data.get("data", [])

        for m in markets:
            try:
                vol = float(m.get("volume") or 0)
                if vol < 5_000:
                    continue

                prices_raw = m.get("outcomePrices", "")
                if not prices_raw:
                    continue

                prices = [float(x) for x in prices_raw.replace("[", "").replace("]", "").replace('"', "").split(",") if x.strip()]
                if len(prices) < 2:
                    continue

                yes_price = prices[0]
                no_price  = prices[1]
                if yes_price <= 0 or no_price <= 0:
                    continue

                total  = yes_price + no_price
                spread = total - 1.0
                net_yes = (1.0 - yes_price) * (1 - POLYMARKET_FEE)
                net_no  = (1.0 - no_price)  * (1 - POLYMARKET_FEE)
                net_return = min(net_yes, net_no)

                snap = MarketSnapshot(
                    market_id  = m.get("id", ""),
                    question   = m.get("question", "")[:200],
                    yes_price  = round(yes_price, 4),
                    no_price   = round(no_price, 4),
                    spread_pct = round(spread * 100, 3),
                    net_return_pct = round(net_return * 100, 3),
                    volume_usd = round(vol, 0),
                    liquidity  = round(float(m.get("liquidity") or 0), 0),
                    source     = "clob_api",
                    url        = f"https://polymarket.com/market/{m.get('slug', m.get('id', ''))}",
                    ts         = datetime.now(timezone.utc).isoformat(),
                    resolved   = m.get("closed", False),
                )
                snapshots.append(snap)
            except Exception:
                continue

        logger.info(f"[CLOB API] 获取 {len(snapshots)} 个有效市场")
    except Exception as e:
        logger.warning(f"[CLOB API] 请求失败: {e}")

    return snapshots


# ── 统一采集入口（自动降级）─────────────────────────────
def collect_markets(limit: int = 100) -> List[MarketSnapshot]:
    """
    统一采集入口，按优先级尝试各数据源：
    1. CLOB REST API
    2. Gamma REST API
    3. 网页抓取（降级方案）
    """
    # 尝试 CLOB API
    snapshots = fetch_markets_via_clob(limit)
    if snapshots:
        return snapshots

    # 尝试 Gamma API
    snapshots = fetch_markets_via_rest_api(limit)
    if snapshots:
        return snapshots

    # 降级：网页抓取
    snapshots = fetch_markets_via_scrape()
    if snapshots:
        return snapshots

    logger.warning("[Collector] 所有数据源均不可用")
    return []


# ── 辅助 ────────────────────────────────────────────────
def _parse_market_dict(m: dict) -> MarketSnapshot:
    """将原始市场字典解析为 MarketSnapshot"""
    vol = float(m.get("volume") or 0)
    prices_raw = m.get("outcomePrices", "")
    prices = [float(x) for x in str(prices_raw).strip("[]\" ").split(",") if x.strip()]
    yes_price = prices[0] if prices else 0.0
    no_price  = prices[1] if len(prices) > 1 else 0.0
    total     = yes_price + no_price
    spread    = total - 1.0
    net_yes   = (1.0 - yes_price) * (1 - POLYMARKET_FEE)
    net_no    = (1.0 - no_price)  * (1 - POLYMARKET_FEE)

    return MarketSnapshot(
        market_id      = m.get("id", ""),
        question       = m.get("question", "")[:200],
        yes_price      = round(yes_price, 4),
        no_price       = round(no_price, 4),
        spread_pct     = round(spread * 100, 3),
        net_return_pct = round(min(net_yes, net_no) * 100, 3),
        volume_usd     = round(vol, 0),
        source         = "web_scrape",
        url            = f"https://polymarket.com/market/{m.get('slug', m.get('id', ''))}",
        ts             = datetime.now(timezone.utc).isoformat(),
        resolved       = m.get("closed", False),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    markets = collect_markets(limit=20)
    for m in markets:
        print(f"[{m.source}] {m.question[:60]} | Yes={m.yes_price:.2%} No={m.no_price:.2%} | Vol=${m.volume_usd:,.0f}")
