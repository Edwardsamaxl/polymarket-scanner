"""
simulator.py — 模拟数据生成器
ImMike 的 simulation/real 双模式设计
用于测试，无需真实 API
"""

import random
import hashlib
from datetime import datetime, timezone
from event_matcher import Event


TEMPLATES = [
    ("Will {party} win the {election} election?", "politics"),
    ("Will {country}'s inflation exceed {pct}% by {date}?", "economics"),
    ("Will the Fed cut rates by {date}?", "economics"),
    ("Will BTC exceed ${price} by {date}?", "crypto"),
    ("Will {team1} beat {team2} in {event}?", "sports"),
    ("Will S&P 500 exceed {level} by {month}?", "finance"),
]

FILL = {
    "party":    ["Republican", "Democrat"],
    "election": ["2026", "midterm"],
    "country":  ["China", "Russia", "Iran"],
    "pct":      ["3", "4", "5"],
    "date":     ["June 2026", "December 2026"],
    "price":    ["100000", "150000"],
    "team1":    ["Lakers", "Chiefs"],
    "team2":    ["Celtics", "Ravens"],
    "event":    ["the Finals", "the Championship"],
    "level":    ["6000", "7000"],
    "month":    ["Q3 2026", "2026"],
}

def _fill(t):
    for k, vs in FILL.items():
        if "{"+k+"}" in t:
            t = t.replace("{"+k+"}", random.choice(vs))
    return t

def create_opportunities(n=5, seed=None):
    if seed is not None:
        random.seed(seed)
    evts = []
    for i in range(n):
        tpl, cat = random.choice(TEMPLATES)
        q = _fill(tpl)
        mid = hashlib.md5(f"{i}_{datetime.now().isoformat()}".encode()).hexdigest()[:16]
        # 偶尔生成概率密度套利机会
        if i == 0 and random.random() < 0.3:
            base = random.uniform(0.45, 0.58)
            yes  = min(round(base + random.uniform(0.01, 0.05), 4), 0.99)
            no   = min(round(1.0 - base + random.uniform(0.01, 0.05), 4), 0.99)
        else:
            yes = round(random.uniform(0.10, 0.90), 4)
            no  = round(random.uniform(0.10, 0.90), 4)
        evts.append(Event(
            market_id  = mid,
            question   = q,
            end_date   = f"2026-{random.randint(6,12):02d}-{random.randint(1,28):02d}",
            category   = cat,
            yes_price  = yes, no_price=no,
            volume_usd = random.randint(5000, 800000),
            source     = "simulation",
            url        = f"https://polymarket.com/market/{mid}",
            ts         = datetime.now(timezone.utc).isoformat(),
        ))
    return evts
