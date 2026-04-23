#!/usr/bin/env python3
"""
scanner.py - Polymarket 套利监控主程序
小木系统 / 2026-04-23

使用方法:
  python3 scanner.py           # 持续运行（cron 驱动模式）
  python3 scanner.py --once    # 单次扫描（GitHub Actions / 手动测试）
  python3 scanner.py --test    # 测试模式（不推送，不写 DB）
"""

import os
import sys
import json
import sqlite3
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path

from collector import collect_markets, MarketSnapshot, POLYMARKET_FEE
from detector import ArbDetector, evaluate_opportunities, ArbOpportunity

# ── 路径配置 ────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent.parent.resolve()
DATA_DIR   = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH    = DATA_DIR / "opportunities.db"
LOG_PATH   = DATA_DIR / "scanner.log"
LAST_PATH  = DATA_DIR / "last_opportunities.json"

# ── 日志配置 ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("scanner")


# ── 数据库 ───────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")   # 写入锁优化
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
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_opp_ts       ON opportunities(ts)
        """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_opp_market   ON opportunities(market_id)
        """)
    conn.commit()
    return conn


def load_last_opportunities() -> set:
    if not LAST_PATH.exists():
        return set()
    with open(LAST_PATH) as f:
        opps = json.load(f)
    return {o["market_id"] for o in opps}


def save_last_opportunities(opps: list):
    with open(LAST_PATH, "w") as f:
        json.dump([o.to_dict() for o in opps], f, ensure_ascii=False, indent=2)


def save_opportunity(conn: sqlite3.Connection, opp: ArbOpportunity, alerted: int = 0):
    conn.execute("""
        INSERT OR IGNORE INTO opportunities
            (ts, market_id, question, yes_price, no_price, spread_pct,
             net_return_pct, volume_usd, liquidity_usd, source, alerted)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        opp.ts, opp.market_id, opp.question,
        opp.yes_price, opp.no_price, opp.spread_pct,
        opp.net_return_pct, opp.volume_usd, opp.liquidity_usd,
        opp.source, alerted
    ))
    conn.commit()


def save_alert_history(conn: sqlite3.Connection, opp: ArbOpportunity):
    conn.execute("""
        INSERT INTO alerted_history
            (ts, market_id, question, yes_price, no_price, spread_pct, net_return, volume_usd, url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        opp.ts, opp.market_id, opp.question,
        opp.yes_price, opp.no_price, opp.spread_pct,
        opp.net_return_pct, opp.volume_usd, opp.url
    ))
    conn.commit()


def record_scan(conn: sqlite3.Connection, ts: str, checked: int, found: int, ms: int, error: str = None):
    conn.execute("""
        INSERT INTO scans (ts, markets_checked, opportunities_found, scan_time_ms, error)
        VALUES (?, ?, ?, ?, ?)
    """, (ts, checked, found, ms, error))
    conn.commit()


# ── 通知推送 ─────────────────────────────────────────────
def push_wechat(message: str) -> bool:
    """
    通过 OpenClaw message 工具推送到微信
    必须在子 agent 或主 agent 上下文中调用（需要 message 工具）
    """
    try:
        from tools import message as msg_tool
        msg_tool(action="send", channel="weixin", message=message)
        logger.info("✅ 微信推送成功")
        return True
    except Exception as e:
        logger.warning(f"微信推送失败（正常，无需处理）: {e}")
        return False


# ── 核心扫描逻辑 ─────────────────────────────────────────
def run_scan(conn: sqlite3.Connection, detector: ArbDetector, test_mode: bool = False) -> list:
    t0 = datetime.now(timezone.utc)
    ts = t0.isoformat()
    error_msg = None
    opps_found = 0

    # 1. 采集数据
    try:
        markets = collect_markets(limit=100)
        logger.info(f"采集 {len(markets)} 个市场")
    except Exception as e:
        logger.error(f"采集失败: {e}")
        error_msg = str(e)
        markets = []

    # 2. 检测套利
    opps = detector.scan(markets)
    logger.info(f"检测到 {len(opps)} 个潜在套利机会")

    # 3. 二次过滤
    filtered = evaluate_opportunities(opps)
    logger.info(f"过滤后剩余 {len(filtered)} 个有效机会")

    # 4. 新机会判断
    last_ids = load_last_opportunities()
    new_opps = [o for o in filtered if o.market_id not in last_ids]

    if new_opps:
        logger.info(f"🎯 新机会 {len(new_opps)} 个!")
    else:
        logger.info("本轮无新机会")

    # 5. 记录 + 通知
    for i, opp in enumerate(filtered, 1):
        save_opportunity(conn, opp, alerted=1 if opp in new_opps else 0)

        if opp in new_opps and not test_mode:
            save_alert_history(conn, opp)
            push_wechat(opp.to_message(rank=i))

    # 6. 保存快照
    save_last_opportunities(filtered)

    # 7. 记录扫描
    scan_ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
    record_scan(conn, ts, len(markets), len(opps), scan_ms, error_msg)

    return filtered


# ── 统计报告 ─────────────────────────────────────────────
def print_stats(conn: sqlite3.Connection):
    """打印最近扫描统计"""
    cursor = conn.execute("""
        SELECT COUNT(*), SUM(opportunities_found), AVG(scan_time_ms)
        FROM scans WHERE ts > datetime('now', '-24 hours')
    """)
    row = cursor.fetchone()
    count, total_opps, avg_ms = row

    cursor2 = conn.execute("""
        SELECT COUNT(*) FROM opportunities
        WHERE alerted = 1 AND ts > datetime('now', '-24 hours')
    """)
    alerted = cursor2.fetchone()[0]

    print(f"\n📊 最近24小时统计")
    print(f"   扫描次数：{count} 次")
    print(f"   发现机会：{total_opps or 0} 个")
    print(f"   推送告警：{alerted} 次")
    print(f"   平均耗时：{avg_ms or 0:.0f} ms")


# ── 主入口 ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Polymarket 套利扫描器")
    parser.add_argument("--once",   action="store_true", help="单次扫描（cron 模式）")
    parser.add_argument("--test",   action="store_true", help="测试模式（不推送，不写 DB）")
    parser.add_argument("--debug",  action="store_true", help="开启 debug 日志")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    conn = init_db()
    detector = ArbDetector()

    if args.test:
        logger.info("🧪 测试模式")
        opps = run_scan(conn, detector, test_mode=True)
        print(f"\n测试扫描完成，发现 {len(opps)} 个机会")
        for o in opps:
            print(f"  [{o.confidence()}] {o.question[:60]}")
        return

    if args.once:
        opps = run_scan(conn, detector)
        print(f"完成: {len(opps)} 个机会")
    else:
        import time
        interval = 60   # 每分钟一次（由 cron 驱动）
        logger.info("🚀 Polymarket Scanner 启动（持续模式）")
        while True:
            run_scan(conn, detector)
            time.sleep(interval)


if __name__ == "__main__":
    main()
