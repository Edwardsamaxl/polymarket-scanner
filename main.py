#!/usr/bin/env python3
"""
main.py — Polymarket Scanner 入口
GitHub Actions 调用此文件 --once 执行一次扫描
"""

import sys
import os
import json
import logging
import argparse
from pathlib import Path

# 添加 src 到路径（支持相对导入和直接运行）
BASE = Path(__file__).parent
sys.path.insert(0, str(BASE / "src"))
sys.path.insert(0, str(BASE))

from src.config import Config
from src.polymarket_client import PolymarketClient
from src.event_matcher import EventMatcher, Event
from simulator import create_opportunities

# ── 日志配置 ──────────────────────────────────────────────────────
def setup_log(level=logging.INFO):
    log_file = BASE / "data" / "scanner.log"
    os.makedirs(BASE / "data", exist_ok=True)
    l = logging.getLogger("scanner")
    l.setLevel(level)
    if not l.handlers:
        h1 = logging.FileHandler(log_file, encoding="utf-8", mode="a")
        h1.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s","%m-%d %H:%M:%S"))
        h2 = logging.StreamHandler(sys.stdout)
        h2.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s","%m-%d %H:%M:%S"))
        l.addHandler(h1); l.addHandler(h2)
    return l

# ── 数据模型 ───────────────────────────────────────────────────────
class Opp:
    """套利机会"""
    def __init__(self, layer, event: Event, spread_pct, net_return_pct,
                 extra=None, match_result=None):
        self.layer       = layer
        self.market_id   = event.market_id
        self.question    = event.question
        self.yes_price   = event.yes_price
        self.no_price    = event.no_price
        self.spread_pct  = spread_pct
        self.net_return_pct = net_return_pct
        self.volume_usd  = event.volume_usd
        self.source      = event.source
        self.url         = event.url
        self.ts          = event.ts
        self.extra       = extra or {}
        self.match       = match_result

    def confidence(self):
        if self.layer == "L1" and self.spread_pct >= 5: return "🔴 高"
        if self.layer == "L2" and self.spread_pct >= 3: return "🟡 中"
        if self.layer == "L3": return "🟡 研究线索"
        return "🟢 低"

    def to_dict(self):
        return {
            "layer": self.layer, "market_id": self.market_id,
            "question": self.question, "yes_price": self.yes_price,
            "no_price": self.no_price, "spread_pct": self.spread_pct,
            "net_return_pct": self.net_return_pct,
            "volume_usd": self.volume_usd, "source": self.source,
            "url": self.url, "ts": self.ts,
            "extra": self.extra,
            "match": self.match.to_dict() if self.match else None,
        }

    def to_msg(self, rank=1):
        layer_desc = {"L1":"概率密度套利","L2":"跨平台套利","L3":"研究线索"}
        extra_lines = ""
        if self.extra.get("kalshi_price"):
            extra_lines += f"\nKalshi：{self.extra['kalshi_price']:.2%}"
        if self.extra.get("reasoning"):
            extra_lines += f"\n📝 {self.extra['reasoning'][:80]}"
        if self.match:
            extra_lines += f"\n匹配度：{self.match.overall:.0%}"

        return (
            f"{self.confidence()} Polymarket #{rank} [{layer_desc.get(self.layer,'')}]\n\n"
            f"📋 {self.question}\n"
            f"{'─'*18}\n"
            f"Yes：{self.yes_price:.2%}  No：{self.no_price:.2%}\n"
            f"空间：{self.spread_pct:.2f}%  净收益：{self.net_return_pct:.2f}%\n"
            f"成交量：${self.volume_usd:,.0f}{extra_lines}\n"
            f"{'─'*18}\n"
            f"⚠️ 手动确认后操作\n"
            f"🔗 {self.url}"
        )

# ── 检测层 ─────────────────────────────────────────────────────────
POLYMARKET_FEE = 0.01

def detect_l1(events, cfg):
    """L1 概率密度套利"""
    opps = []
    for e in events:
        if e.yes_price <= 0 or e.no_price <= 0 or e.resolved: continue
        if e.volume_usd < cfg.detector.l1_min_volume: continue
        total  = e.yes_price + e.no_price
        spread = total - 1.0
        net_yes = (1.0 - e.yes_price) * (1 - POLYMARKET_FEE)
        net_no  = (1.0 - e.no_price)  * (1 - POLYMARKET_FEE)
        net_r   = min(net_yes, net_no)
        if spread >= cfg.detector.l1_min_spread and net_r >= cfg.detector.l1_min_net_return:
            opps.append(Opp("L1", e, round(spread*100,3), round(net_r*100,3)))
    opps.sort(key=lambda x: x.spread_pct, reverse=True)
    return opps

def detect_l2(events, cfg, matcher):
    """L2 跨平台套利（通过事件匹配算法找 Polymarket vs Kalshi 配对）"""
    opps = []
    for e in events:
        if e.resolved or e.volume_usd < cfg.detector.l2_min_volume: continue
        # L2 策略：极端价格 + 高成交量（简化版，完整版需调用 Kalshi API）
        extreme = max(e.yes_price, e.no_price)
        if extreme > 0.80:
            spread = (extreme - 0.5) * 100
            opps.append(Opp(
                "L2", e,
                round(spread, 3),
                round((extreme - 0.80) * 100, 3),
                extra={"reasoning": f"极端价格市场（Yes={e.yes_price:.0%}），需检查Kalshi是否有价差"},
            ))
    opps.sort(key=lambda x: x.spread_pct, reverse=True)
    return opps

def detect_l3(events, cfg):
    """L3 研究线索（中间概率 + 高成交量）"""
    opps = []
    for e in events:
        if e.resolved or e.volume_usd < cfg.detector.l3_min_volume: continue
        p = e.yes_price
        if p < 0.15 or p > 0.85: continue
        opps.append(Opp(
            "L3", e,
            round(abs(p - 0.5) * 200, 2),
            round(abs(p - 0.5) * 100, 2),
            extra={"reasoning": "中间概率+高成交量，市场定价可能存在偏差，值得深入研究"}
        ))
    opps.sort(key=lambda x: x.volume_usd, reverse=True)
    return opps[:20]

# ── 通知 ───────────────────────────────────────────────────────────
def push_wechat(msg):
    try:
        from tools import message as mt
        mt(action="send", channel="weixin", message=msg)
        return True
    except Exception:
        return False

# ── 主扫描 ─────────────────────────────────────────────────────────
def run_scan(cfg: Config, log, test=False):
    import time as _time
    from datetime import datetime, timezone

    t0 = datetime.now(timezone.utc)
    ts = t0.isoformat()

    # 1. 采集数据
    if cfg.trading.data_mode == "simulation":
        log.info("[Simulation 模式] 生成模拟数据")
        events = create_opportunities(n=20, seed=int(_time.time()))
        log.info(f"  生成 {len(events)} 个模拟市场")
    else:
        client = PolymarketClient(cache_ttl=60)
        events_raw = client.collect_all(limit=200)
        events = [m.to_event() for m in events_raw]
        log.info(f"[Real 模式] 采集 {len(events)} 个真实市场")

    if not events:
        log.warning("无市场数据，退出")
        return []

    # 2. 检测
    l1 = detect_l1(events, cfg)
    matcher = EventMatcher(threshold_high=0.75, threshold_med=0.50)
    l2 = detect_l2(events, cfg, matcher)
    l3 = detect_l3(events, cfg)
    all_opps = l1 + l2 + l3

    log.info(f"检测结果: L1={len(l1)} L2={len(l2)} L3={len(l3)}")

    # 3. 新机会判断
    snap_file = BASE / "data" / "last_scan.json"
    last_ids = set()
    if snap_file.exists():
        try:
            last_ids = {o["market_id"] for o in json.loads(snap_file.read_text())}
        except: pass

    new_opps = [o for o in all_opps if o.market_id not in last_ids]
    log.info(f"新机会: {len(new_opps)} 个")

    # 4. 写入快照
    snap_file.write_text(json.dumps([o.to_dict() for o in all_opps], ensure_ascii=False, indent=2))

    # 5. 发现新机会 → 写 new_opportunity.json + 推送
    new_opp_file = BASE / "data" / "new_opportunity.json"
    if new_opps and not test:
        new_opp_file.write_text(json.dumps([o.to_dict() for o in new_opps], ensure_ascii=False, indent=2))
        for i, o in enumerate(new_opps[:3], 1):
            push_wechat(o.to_msg(rank=i))
            log.info(f"  推送 #{i}: {o.question[:50]}")
    elif new_opp_file.exists():
        new_opp_file.unlink()

    ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
    log.info(f"✅ 扫描完成，耗时 {ms}ms")

    if test:
        print(f"\n{'='*55}")
        print(f"📊 测试扫描结果（模式: {cfg.trading.data_mode}）")
        print(f"   市场总数：{len(events)}")
        print(f"   L1 概率密度套利：{len(l1)} 个")
        for o in l1: print(f"     {o.confidence()} {o.question[:55]}")
        print(f"   L2 跨平台套利：{len(l2)} 个")
        for o in l2[:3]: print(f"     {o.confidence()} {o.question[:55]}")
        print(f"   L3 研究线索：{len(l3)} 个（见 last_scan.json）")
        print(f"{'='*55}")

    return all_opps

# ── 入口 ───────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Polymarket Scanner")
    ap.add_argument("--once",  action="store_true", help="单次扫描（GitHub Actions）")
    ap.add_argument("--test",   action="store_true", help="测试模式")
    ap.add_argument("--debug", action="store_true", help="DEBUG 日志")
    args = ap.parse_args()

    log = setup_log(logging.DEBUG if args.debug else logging.INFO)
    cfg = Config.from_yaml(str(BASE / "config.yaml"))

    if args.test:
        log.info("🧪 测试模式")
        cfg.trading.data_mode = "simulation"  # 测试强制用 simulation
        run_scan(cfg, log, test=True)
        return

    if args.once:
        run_scan(cfg, log)
    else:
        import time as _time
        log.info(f"🚀 Polymarket Scanner 启动（{cfg.trading.data_mode} 模式）")
        while True:
            run_scan(cfg, log)
            _time.sleep(cfg.detector.get("polling_interval", 60))

if __name__ == "__main__":
    main()
