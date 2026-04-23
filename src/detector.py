#!/usr/bin/env python3
"""
detector.py - 套利检测引擎
小木系统 / 2026-04-23
"""

import logging
from dataclasses import dataclass
from typing import List, Optional

from collector import MarketSnapshot, POLYMARKET_FEE

logger = logging.getLogger("detector")


# ── 检测参数 ─────────────────────────────────────────────
@dataclass
class ArbParams:
    min_spread:      float = 0.02    # 最小套利空间（Yes+No-1 > 2%）
    min_net_return:  float = 0.005   # 最小费后净收益率（> 0.5%）
    min_volume_usd:  float = 5_000   # 最小成交量门槛
    max_age_hours:    float = 24.0    # 忽略超过 N 小时未更新的市场


@dataclass
class ArbOpportunity:
    """检测到的套利机会"""
    market_id:       str
    question:        str
    yes_price:       float
    no_price:        float
    spread_pct:      float   # Yes + No - 1（%）
    net_return_pct: float   # 费后净收益（%）
    volume_usd:     float
    liquidity_usd:   float
    url:             str
    source:          str
    ts:              str

    def confidence(self) -> str:
        """置信度评级"""
        if self.spread_pct >= 5 and self.net_return_pct >= 2:
            return "🔴 高"
        elif self.spread_pct >= 3 and self.net_return_pct >= 1:
            return "🟡 中"
        return "🟢 低"

    def to_message(self, rank: int = 1) -> str:
        return (
            f"{self.confidence()} Polymarket 套利机会 #{rank}\n\n"
            f"📋 {self.question}\n"
            f"────────────────────\n"
            f"Yes 概率：{self.yes_price:.2%}\n"
            f"No 概率：{self.no_price:.2%}\n"
            f"套利空间：{self.spread_pct:.2f}%\n"
            f"费后净收益：{self.net_return_pct:.2f}%\n"
            f"成交量：${self.volume_usd:,.0f}\n"
            f"────────────────────\n"
            f"⚠️ 手动确认后操作，机会随时消失\n"
            f"🔗 {self.url}"
        )


# ── 检测器 ───────────────────────────────────────────────
class ArbDetector:
    """
    概率密度套利检测器

    原理：Polymarket 的 Yes/No 股券价格理论上和应为 1.0
    当 Yes + No > 1.0 时，存在套利空间：
    - 双边各买入 $1，无论结果如何都有正收益
    - 实际收益 = (1 - price) * (1 - fee)
    """

    def __init__(self, params: ArbParams = None):
        self.params = params or ArbParams()

    def check(self, snap: MarketSnapshot) -> Optional[ArbOpportunity]:
        """检测单个市场是否构成套利机会"""
        # 跳过已结算或关闭的市场
        if snap.resolved:
            return None

        # 成交量门槛
        if snap.volume_usd < self.params.min_volume_usd:
            return None

        # 跳过价格为 0 的情况
        if snap.yes_price <= 0 or snap.no_price <= 0:
            return None

        yes_price = snap.yes_price
        no_price  = snap.no_price

        # ── 核心检测 ──
        # Yes + No > 1.0 才有套利空间
        total     = yes_price + no_price
        spread    = total - 1.0       # 正数 = 套利空间

        # 费后净收益率（以 $1 双边各买计算）
        # 结果 Yes：赚 (1 - yes_price)，扣手续费
        # 结果 No： 赚 (1 - no_price)，扣手续费
        net_yes = (1.0 - yes_price) * (1 - POLYMARKET_FEE)
        net_no  = (1.0 - no_price)  * (1 - POLYMARKET_FEE)
        net_return = min(net_yes, net_no)

        if spread >= self.params.min_spread and net_return >= self.params.min_net_return:
            return ArbOpportunity(
                market_id       = snap.market_id,
                question        = snap.question,
                yes_price       = yes_price,
                no_price        = no_price,
                spread_pct      = round(spread * 100, 3),
                net_return_pct  = round(net_return * 100, 3),
                volume_usd      = snap.volume_usd,
                liquidity_usd   = snap.liquidity,
                url             = snap.url,
                source          = snap.source,
                ts              = snap.ts,
            )
        return None

    def scan(self, snapshots: List[MarketSnapshot]) -> List[ArbOpportunity]:
        """扫描一批市场，返回所有套利机会"""
        opps = []
        for snap in snapshots:
            opp = self.check(snap)
            if opp:
                opps.append(opp)

        # 按套利空间从大到小排序
        opps.sort(key=lambda x: x.spread_pct, reverse=True)
        return opps


# ── 批量评估（带过滤）───────────────────────────────────
def evaluate_opportunities(opps: List[ArbOpportunity]) -> List[ArbOpportunity]:
    """
    对检测到的机会进行二次过滤和评级
    过滤掉流动性差、操作成本高、机会窗口可能极短的情况
    """
    if not opps:
        return []

    max_opp = opps[0]
    filtered = []

    for opp in opps:
        # 过滤：成交量低于 $10k 的机会通常流动性不够
        if opp.volume_usd < 10_000:
            logger.debug(f"过滤低成交量: {opp.question[:40]} Vol=${opp.volume_usd:,.0f}")
            continue

        # 过滤：套利空间小于最高机会的 20% 的，优先级低
        if max_opp and opp.spread_pct < max_opp.spread_pct * 0.2:
            continue

        filtered.append(opp)

    return filtered


if __name__ == "__main__":
    import json
    from collector import collect_markets

    logging.basicConfig(level=logging.INFO)

    markets = collect_markets(limit=50)
    detector = ArbDetector()
    opps = detector.scan(markets)
    filtered = evaluate_opportunities(opps)

    print(f"\n扫描 {len(markets)} 个市场 → {len(opps)} 个套利机会 → {len(filtered)} 个通过过滤")
    for i, o in enumerate(filtered, 1):
        print(f"\n[{i}] {o.question[:60]}")
        print(f"    Yes={o.yes_price:.2%} No={o.no_price:.2%} | 空间={o.spread_pct:.2f}% | 净收益={o.net_return_pct:.2f}%")
