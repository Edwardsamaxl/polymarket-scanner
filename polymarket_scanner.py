#!/usr/bin/env python3
"""
polymarket_scanner.py - Polymarket 套利监控系统（单文件版）
小木系统 / Edwardsamaxl / 2026-04-23

概率密度套利原理：
  当 Yes 价格 + No 价格 > 1.0 时，双边各买入 $1
  无论结果如何，扣除 Polymarket 1% 手续费后仍有正收益

使用方法：
  python3 polymarket_scanner.py              # 持续运行
  python3 polymarket_scanner.py --once        # 单次扫描（cron 模式）
  python3 polymarket_scanner.py --test        # 测试模式（不推送）
  python3 polymarket_scanner.py --stats       # 查看24小时统计
"""

import os
import sys
import json
import sqlite3
import logging
import argparse
import time as time_module
from dataclasses import dataclass, asdict
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Optional

import requests
import yaml

# ═══════════════════════════════════════════════════════════════
# 路径配置
# ═══════════════════════════════════════════════════════════════
BASE_DIR  = Path(__file__).parent.resolve()
DATA_DIR  = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH   = DATA_DIR / "opportunities.db"
LOG_PATH  = DATA_DIR / "scanner.log"
LAST_PATH = DATA_DIR / "last_opportunities.json"

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════
POLYMARKET_FEE = 0.01   # Polymarket 每笔约 1% 手续费

@dataclass
class Config:
    polling_interval:  int = 60
    min_volume_usd:    float = 5_000
    min_spread:        float = 0.02    # 2% 套利空间
    min_net_return:    float = 0.005   # 0.5% 费后净收益
    polymarket_fee:    float = 0.01

    @classmethod
    def from_yaml(cls, path: Path) -> "Config":
        if not path.exists():
            return cls()
        with open(path) as f:
            d = yaml.safe_load(f) or {}
        c = cls()
        for k, v in d.items():
            if hasattr(c, k):
                setattr(c, k, v)
        return c


def load_config() -> Config:
    return Config.from_yaml(BASE_DIR / "config.yaml")


# ═══════════════════════════════════════════════════════════════
# 日志
# ═══════════════════════════════════════════════════════════════
def setup_log():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8", mode="a"),
            logging.StreamHandler(sys.stdout),
        ]
    )
    return logging.getLogger("scanner")


# ═══════════════════════════════════════════════════════════════
# 数据库
# ═══════════════════════════════════════════════════════════════
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS opportunities (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            market_id       TEXT NOT NULL,
            question        TEXT,
            yes_price       REAL,
            no_price        REAL,
            spread_pct      REAL,
            net_return_pct  REAL,
            volume_usd      REAL,
            liquidity_usd   REAL,
            source          TEXT,
            alerted         INTEGER DEFAULT 0,
            resolved        INTEGER DEFAULT 0,
            result          TEXT,
            UNIQUE(market_id, ts)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scans (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                  TEXT NOT NULL,
            markets_checked     INTEGER,
            opportunities_found INTEGER,
            scan_time_ms        INTEGER,
            error               TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerted_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            market_id   TEXT,
            question    TEXT,
            yes_price   REAL,
            no_price    REAL,
            spread_pct  REAL,
            net_return  REAL,
            volume_usd  REAL,
            url         TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_opp_ts     ON opportunities(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_opp_market ON opportunities(market_id)")
    conn.commit()
    return conn


def load_last_ids() -> set:
    if not LAST_PATH.exists():
        return set()
    try:
        return {o["market_id"] for o in json.loads(LAST_PATH.read_text())}
    except Exception:
        return set()


def save_last(opps: list):
    LAST_PATH.write_text(json.dumps([o.to_dict() for o in opps], ensure_ascii=False, indent=2))


def save_opp(conn: sqlite3.Connection, opp, alerted: int):
    conn.execute("""
        INSERT OR IGNORE INTO opportunities
            (ts, market_id, question, yes_price, no_price, spread_pct,
             net_return_pct, volume_usd, liquidity_usd, source, alerted)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (opp.ts, opp.market_id, opp.question, opp.yes_price, opp.no_price,
          opp.spread_pct, opp.net_return_pct, opp.volume_usd, opp.liquidity_usd,
          opp.source, alerted))
    conn.commit()


def save_alert(conn: sqlite3.Connection, opp):
    conn.execute("""
        INSERT INTO alerted_history
            (ts, market_id, question, yes_price, no_price, spread_pct, net_return, volume_usd, url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (opp.ts, opp.market_id, opp.question, opp.yes_price, opp.no_price,
          opp.spread_pct, opp.net_return_pct, opp.volume_usd, opp.url))
    conn.commit()


def record_scan(conn: sqlite3.Connection, ts: str, n: int, found: int, ms: int, err: str = None):
    conn.execute("INSERT INTO scans (ts, markets_checked, opportunities_found, scan_time_ms, error) VALUES (?,?,?,?,?)",
                 (ts, n, found, ms, err))
    conn.commit()


# ═══════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════
@dataclass
class Market:
    market_id:    str = ""
    question:     str = ""
    yes_price:    float = 0.0
    no_price:     float = 0.0
    volume_usd:   float = 0.0
    liquidity:    float = 0.0
    source:       str = ""
    url:          str = ""
    ts:           str = ""
    resolved:     bool = False

    def to_dict(self):
        d = asdict(self)
        d["ts"] = self.ts or datetime.now(timezone.utc).isoformat()
        return d


@dataclass
class Opp:
    market_id:       str
    question:        str
    yes_price:       float
    no_price:        float
    spread_pct:      float   # Yes+No-1（%）
    net_return_pct:  float   # 费后净收益（%）
    volume_usd:     float
    liquidity_usd:   float
    url:             str
    source:          str
    ts:              str

    def confidence(self) -> str:
        if self.spread_pct >= 5 and self.net_return_pct >= 2: return "🔴 高"
        if self.spread_pct >= 3 and self.net_return_pct >= 1: return "🟡 中"
        return "🟢 低"

    def to_dict(self):
        return {
            "market_id": self.market_id, "question": self.question,
            "yes_price": self.yes_price, "no_price": self.no_price,
            "spread_pct": self.spread_pct, "net_return_pct": self.net_return_pct,
            "volume_usd": self.volume_usd, "liquidity_usd": self.liquidity_usd,
            "url": self.url, "source": self.source, "ts": self.ts,
        }

    def to_msg(self, rank: int = 1) -> str:
        return (
            f"{self.confidence()} Polymarket 套利 #{rank}\n\n"
            f"📋 {self.question}\n"
            f"─────────────────────\n"
            f"Yes：{self.yes_price:.2%}  No：{self.no_price:.2%}\n"
            f"空间：{self.spread_pct:.2f}%  净收益：{self.net_return_pct:.2f}%\n"
            f"成交量：${self.volume_usd:,.0f}\n"
            f"─────────────────────\n"
            f"⚠️ 手动确认后操作，机会随时消失\n"
            f"🔗 {self.url}"
        )


# ═══════════════════════════════════════════════════════════════
# 数据采集层（自动降级：REST → GraphQL → Web Scraping）
# ═══════════════════════════════════════════════════════════════
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html",
    "Accept-Language": "en-US,en;q=0.9",
})


def _parse_prices(raw) -> List[float]:
    if isinstance(raw, list):
        return [float(x) for x in raw]
    return [float(x) for x in str(raw).strip('[]" \n').split(",") if x.strip()]


def _make_market(m: dict, src: str) -> Optional[Market]:
    try:
        vol = float(m.get("volume") or 0)
        prices = _parse_prices(m.get("outcomePrices", ""))
        if len(prices) < 2 or vol < 100:
            return None
        yes_p, no_p = prices[0], prices[1]
        if yes_p <= 0 or no_p <= 0:
            return None
        sid = m.get("id", "")
        slug = m.get("slug", sid)
        return Market(
            market_id  = sid,
            question   = m.get("question", "")[:200],
            yes_price  = round(yes_p, 4),
            no_price   = round(no_p, 4),
            volume_usd = round(vol, 0),
            liquidity  = round(float(m.get("liquidity") or 0), 0),
            source     = src,
            url        = f"https://polymarket.com/market/{slug}",
            ts         = datetime.now(timezone.utc).isoformat(),
            resolved   = bool(m.get("closed") or m.get("resolved")),
        )
    except Exception:
        return None


def collect_gamma() -> List[Market]:
    """Gamma REST API"""
    try:
        resp = SESSION.get("https://gamma-api.polymarket.com/markets",
                           params={"limit": 100, "closed": "false"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        markets = data if isinstance(data, list) else data.get("data", [])
        result = [_make_market(m, "gamma") for m in markets]
        logging.getLogger("scanner").info(f"[Gamma] {len([x for x in result if x])} 个有效市场")
        return [x for x in result if x]
    except Exception as e:
        logging.getLogger("scanner").warning(f"[Gamma] 失败: {e}")
        return []


def collect_clob() -> List[Market]:
    """CLOB REST API"""
    try:
        resp = SESSION.get("https://clob.polymarket.com/markets",
                           params={"limit": 100}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        markets = data if isinstance(data, list) else data.get("data", [])
        result = [_make_market(m, "clob") for m in markets]
        logging.getLogger("scanner").info(f"[CLOB] {len([x for x in result if x])} 个有效市场")
        return [x for x in result if x]
    except Exception as e:
        logging.getLogger("scanner").warning(f"[CLOB] 失败: {e}")
        return []


def collect_graphql() -> List[Market]:
    """CLOB GraphQL API"""
    try:
        query = """
        {
          markets(limit:100, closed:false, archived:false, orderBy:"volume", order:"desc") {
            id slug question volume liquidity outcomePrices closed resolved
          }
        }
        """
        resp = SESSION.post("https://clob.polymarket.com/graphql",
                            json={"query": query}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        markets = data.get("data", {}).get("markets", []) or []
        result = [_make_market(m, "graphql") for m in markets]
        logging.getLogger("scanner").info(f"[GraphQL] {len([x for x in result if x])} 个有效市场")
        return [x for x in result if x]
    except Exception as e:
        logging.getLogger("scanner").warning(f"[GraphQL] 失败: {e}")
        return []


def collect_all(limit: int = 100) -> List[Market]:
    """统一采集入口，按优先级降级"""
    for fn in [collect_clob, collect_gamma, collect_graphql]:
        markets = fn()
        if markets:
            return markets
    logging.getLogger("scanner").warning("[Collector] 所有 API 均不可用")
    return []


# ═══════════════════════════════════════════════════════════════
# 检测层
# ═══════════════════════════════════════════════════════════════
class Detector:
    def __init__(self, cfg: Config):
        self.min_spread     = cfg.min_spread
        self.min_net_return = cfg.min_net_return
        self.min_volume     = cfg.min_volume_usd
        self.fee            = cfg.polymarket_fee

    def check(self, m: Market) -> Optional[Opp]:
        if m.resolved or m.volume_usd < self.min_volume:
            return None
        if m.yes_price <= 0 or m.no_price <= 0:
            return None

        total  = m.yes_price + m.no_price
        spread = total - 1.0

        net_yes    = (1.0 - m.yes_price) * (1 - self.fee)
        net_no     = (1.0 - m.no_price)  * (1 - self.fee)
        net_return = min(net_yes, net_no)

        if spread >= self.min_spread and net_return >= self.min_net_return:
            return Opp(
                market_id       = m.market_id,
                question        = m.question,
                yes_price       = m.yes_price,
                no_price        = m.no_price,
                spread_pct      = round(spread * 100, 3),
                net_return_pct  = round(net_return * 100, 3),
                volume_usd      = m.volume_usd,
                liquidity_usd   = m.liquidity,
                url             = m.url,
                source          = m.source,
                ts              = m.ts,
            )
        return None

    def scan(self, markets: List[Market]) -> List[Opp]:
        opps = [o for m in markets if (o := self.check(m))]
        opps.sort(key=lambda x: x.spread_pct, reverse=True)
        return opps


def filter_opps(opps: List[Opp]) -> List[Opp]:
    """二次过滤"""
    if not opps:
        return []
    max_spread = opps[0].spread_pct
    return [
        o for o in opps
        if o.volume_usd >= 10_000
        and o.spread_pct >= max_spread * 0.2
    ]


# ═══════════════════════════════════════════════════════════════
# 推送层
# ═══════════════════════════════════════════════════════════════
def push_wechat(msg: str):
    try:
        from tools import message as mt
        mt(action="send", channel="weixin", message=msg)
        logging.getLogger("scanner").info("✅ 微信推送成功")
    except Exception as e:
        logging.getLogger("scanner").warning(f"微信推送失败: {e}")


# ═══════════════════════════════════════════════════════════════
# 主扫描
# ═══════════════════════════════════════════════════════════════
def run_scan(conn: sqlite3.Connection, detector: Detector, test: bool = False) -> List[Opp]:
    t0 = datetime.now(timezone.utc)
    ts = t0.isoformat()

    markets = collect_all(limit=100)
    logging.getLogger("scanner").info(f"采集 {len(markets)} 个市场")

    opps = detector.scan(markets)
    logging.getLogger("scanner").info(f"检测 {len(opps)} 个潜在机会")

    filtered = filter_opps(opps)
    logging.getLogger("scanner").info(f"过滤后 {len(filtered)} 个有效机会")

    last_ids = load_last_ids()
    new_opps = [o for o in filtered if o.market_id not in last_ids]

    for i, opp in enumerate(filtered, 1):
        save_opp(conn, opp, alerted=1 if opp in new_opps else 0)
        if opp in new_opps and not test:
            save_alert(conn, opp)
            push_wechat(opp.to_msg(rank=i))

    save_last(filtered)

    ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
    record_scan(conn, ts, len(markets), len(opps), ms)

    if new_opps:
        logging.getLogger("scanner").info(f"🎯 新机会 {len(new_opps)} 个，已推送")
    else:
        logging.getLogger("scanner").info("本轮无新机会")

    return filtered


def print_stats(conn: sqlite3.Connection):
    c = conn.execute("""
        SELECT COUNT(*), SUM(opportunities_found), AVG(scan_time_ms)
        FROM scans WHERE ts > datetime('now', '-24 hours')
    """).fetchone()
    a = conn.execute("SELECT COUNT(*) FROM alerted_history WHERE ts > datetime('now', '-24 hours')").fetchone()
    print(f"\n📊 最近24小时  扫描{c[0] or 0}次  发现{c[1] or 0}个机会  推送{a[0] or 0}次  均耗时{c[2] or 0:.0f}ms")


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once",  action="store_true", help="单次扫描")
    ap.add_argument("--test",  action="store_true", help="测试模式（不推送）")
    ap.add_argument("--stats", action="store_true", help="查看统计")
    args = ap.parse_args()

    log = setup_log()
    cfg = load_config()
    conn = init_db()

    if args.stats:
        print_stats(conn)
        return

    detector = Detector(cfg)

    if args.test:
        log.info("🧪 测试模式")
        opps = run_scan(conn, detector, test=True)
        print(f"\n发现 {len(opps)} 个机会：")
        for o in opps:
            print(f"  [{o.confidence()}] {o.question[:60]}")
            print(f"    Yes={o.yes_price:.2%} No={o.no_price:.2%} 空间={o.spread_pct:.2f}% 净={o.net_return_pct:.2f}% Vol=${o.volume_usd:,.0f}")
        print_stats(conn)
        return

    if args.once:
        run_scan(conn, detector)
    else:
        log.info(f"🚀 Polymarket Scanner 启动，轮询间隔 {cfg.polling_interval}s")
        while True:
            run_scan(conn, detector)
            time_module.sleep(cfg.polling_interval)


if __name__ == "__main__":
    main()
