#!/usr/bin/env python3
"""
polymarket_scanner.py - Polymarket 套利监控系统
小木系统 / Edwardsamaxl / 2026-04-23

三层检测架构：
  L1 概率密度套利  Yes+No > 1.01 → 极低频但零风险
  L3 研究驱动      市场错误定价研究（我的专长）
"""

import os, sys, json, sqlite3, logging, argparse, time as _time
from dataclasses import dataclass, asdict
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Optional

import requests
import yaml

# ═══════════════════════════════════════════════════════════
# 路径配置
# ═══════════════════════════════════════════════════════════
BASE   = Path(__file__).parent.resolve()
DATA   = BASE / "data";  DATA.mkdir(exist_ok=True)
DB     = DATA / "opportunities.db"
LOG    = DATA / "scanner.log"
SNAP   = DATA / "last_scan.json"
NEWOPP = DATA / "new_opportunity.json"   # 触发通知标记
STATS  = DATA / "stats.json"

# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════
POLYMARKET_FEE = 0.01

@dataclass
class Config:
    # L1 概率密度
    l1_min_spread:      float = 0.02   # Yes+No > 1.02 才算有效
    l1_min_net_return:  float = 0.003  # 扣手续费后最低净收益 0.3%
    l1_min_volume:      float = 10_000 # 成交量门槛

    # L2 已禁用（用户不需要跨平台套利）

    # 通用
    min_volume:         float = 5_000
    polling_interval:    int   = 60

    @classmethod
    def from_yaml(cls, path= BASE/"config.yaml"):
        if not path.exists(): return cls()
        d = yaml.safe_load(open(path)) or {}
        c = cls()
        for k,v in d.items():
            if hasattr(c, k): setattr(c, k, v)
        return c

# ═══════════════════════════════════════════════════════════
# 日志
# ═══════════════════════════════════════════════════════════
def get_logger():
    l = logging.getLogger("scanner")
    l.setLevel(logging.INFO)
    if not l.handlers:
        h = logging.FileHandler(LOG, encoding="utf-8", mode="a")
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s","%m-%d %H:%M:%S"))
        l.addHandler(h)
        l.addHandler(logging.StreamHandler(sys.stdout))
    return l

# ═══════════════════════════════════════════════════════════
# 数据库
# ═══════════════════════════════════════════════════════════
def init_db():
    c = sqlite3.connect(str(DB), timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""
        CREATE TABLE IF NOT EXISTS opportunities (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            market_id       TEXT NOT NULL,
            question        TEXT,
            layer           TEXT,        -- L1/L3
            yes_price       REAL,
            no_price        REAL,
            spread_pct      REAL,
            net_return_pct   REAL,
            volume_usd      REAL,
            source          TEXT,
            alerted         INTEGER DEFAULT 0,
            resolved        INTEGER DEFAULT 0,
            result          TEXT,
            UNIQUE(market_id, ts, layer)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS scans (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL,
            checked INTEGER,
            l1_opps INTEGER,
            
            l3_opps INTEGER,
            ms      INTEGER,
            error   TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS i_ts  ON opportunities(ts)")
    c.execute("CREATE INDEX IF NOT EXISTS i_lyr ON opportunities(layer)")
    c.commit()
    return c

def load_last_ids():
    if not SNAP.exists(): return set()
    try:
        return {o["market_id"] for o in json.loads(SNAP.read_text())}
    except: return set()

def save_snapshot(opps):
    SNAP.write_text(json.dumps([o.to_dict() for o in opps], ensure_ascii=False, indent=2))

def save_opp(c, o, layer):
    c.execute("""
        INSERT OR IGNORE INTO opportunities
            (ts,market_id,question,layer,yes_price,no_price,
             spread_pct,net_return_pct,volume_usd,source,alerted)
        VALUES (?,?,?,?,?,?,?,?,?,?,0)
    """, (o.ts,o.market_id,o.question,layer,o.yes_price,o.no_price,
          o.spread_pct,o.net_return_pct,o.volume_usd,o.source))
    c.commit()

def save_scan(c, ts, n, l1,l2,l3, ms, err=None):
    c.execute("INSERT INTO scans (ts,checked,l1_opps,l3_opps,ms,error) VALUES (?,?,?,?,?,?,?)",
              (ts,n,l1,l2,l3,ms,err)); c.commit()

def load_stats():
    if not STATS.exists(): return {}
    try: return json.loads(STATS.read_text())
    except: return {}

def save_stats(s):
    STATS.write_text(json.dumps(s, ensure_ascii=False, indent=2))

def calc_stats(c):
    r = c.execute("""
        SELECT COUNT(*),SUM(l1_opps),SUM(l2_opps),SUM(l3_opps),AVG(ms)
        FROM scans WHERE ts>datetime('now','-24hours')
    """).fetchone()
    a = c.execute("SELECT COUNT(*) FROM opportunities WHERE alerted=1 AND ts>datetime('now','-24hours')").fetchone()
    return {"scans_24h":r[0] or 0,"l1_total":r[1] or 0,"l3_total":r[3] or 0,
            "avg_ms":round(r[4] or 0),"alerts_24h":a[0] or 0}

# ═══════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════
@dataclass
class Market:
    market_id:   str = "";  question: str = ""
    yes_price:   float = 0.0;  no_price: float = 0.0
    volume_usd:   float = 0.0;  liquidity: float = 0.0
    slug:         str = "";     source: str = "";  ts: str = ""; resolved: bool = False

    def url(self):
        s = self.slug or self.market_id
        return f"https://polymarket.com/market/{s}"

@dataclass
class Opp:
    layer:       str   # "L1"/"L2"/"L3"
    market_id:   str;  question: str;  yes_price: float;  no_price: float
    spread_pct:  float;  net_return_pct: float;  volume_usd: float
    source:      str;   url: str;  ts: str;  extra: dict = None

    def confidence(self) -> str:
        if   self.layer=="L1" and self.spread_pct>=5: return "🔴 高（L1零风险）"
        elif self.layer=="L3": return "🟡 中（L3研究）"
        return "🟢 低"

    def to_dict(self):
        d = asdict(self)
        d["extra"] = self.extra or {}
        return d

    def to_msg(self, rank=1) -> str:
        layer_desc = {"L1":"概率密度套利","L2":"跨平台套利","L3":"研究驱动机会"}
        extra_msg = ""
        if self.extra:
            if self.layer == "L2" and "kalshi_price" in self.extra:
                extra_msg = f"\nKalshi 价格：{self.extra['kalshi_price']:.2%}"
            if self.layer == "L3" and "reasoning" in self.extra:
                extra_msg = f"\n📝 小木判断：{self.extra['reasoning'][:80]}"
        return (
            f"{self.confidence()} Polymarket #{rank} [{layer_desc.get(self.layer,'')}]\n\n"
            f"📋 {self.question}\n"
            f"{'─'*18}\n"
            f"Yes：{self.yes_price:.2%}  No：{self.no_price:.2%}\n"
            f"空间：{self.spread_pct:.2f}%  净收益：{self.net_return_pct:.2f}%\n"
            f"成交量：${self.volume_usd:,.0f}{extra_msg}\n"
            f"{'─'*18}\n"
            f"⚠️ 手动确认后操作，机会随时消失\n"
            f"🔗 {self.url}"
        )

# ═══════════════════════════════════════════════════════════
# 数据采集（三层降级）
# ═══════════════════════════════════════════════════════════
S = requests.Session()
S.headers.update({"User-Agent":"Mozilla/5.0 (compatible; PolymarketScanner/1.0)","Accept":"application/json"})

def _parse_prices(raw) -> List[float]:
    if isinstance(raw,list): return [float(x) for x in raw]
    return [float(x) for x in str(raw).strip('[]" \n').split(",") if x.strip()]

def _make(slug, m, src) -> Optional[Market]:
    try:
        prices = _parse_prices(m.get("outcomePrices",""))
        if len(prices)<2: return None
        yes_p,no_p = prices[0],prices[1]
        if yes_p<=0 or no_p<=0: return None
        return Market(
            market_id=m.get("id",""),
            question=m.get("question","")[:200],
            yes_price=round(yes_p,4), no_price=round(no_p,4),
            volume_usd=round(float(m.get("volume") or 0),0),
            liquidity=round(float(m.get("liquidity") or 0),0),
            slug=m.get("slug",""), source=src,
            ts=datetime.now(timezone.utc).isoformat(),
            resolved=bool(m.get("closed") or m.get("resolved"))
        )
    except: return None

def _fetch_clob() -> List[Market]:
    """CLOB REST API（主要数据源）"""
    try:
        r = S.get("https://clob.polymarket.com/markets",params={"limit":200},timeout=12)
        r.raise_for_status()
        data = r.json()
        markets = data if isinstance(data,list) else data.get("data",[])
        result = [_make(m.get("slug",""),m,"clob") for m in markets]
        get_logger().info(f"[CLOB] {len([x for x in result if x])} 个有效市场")
        return [x for x in result if x]
    except Exception as e:
        get_logger().warning(f"[CLOB] 失败: {e}"); return []

def _fetch_gamma() -> List[Market]:
    """Gamma REST API（备用数据源）"""
    try:
        r = S.get("https://gamma-api.polymarket.com/markets",
                  params={"limit":200,"closed":"false"},timeout=12)
        r.raise_for_status()
        data = r.json()
        markets = data if isinstance(data,list) else data.get("data",[])
        result = [_make(m.get("slug",""),m,"gamma") for m in markets]
        get_logger().info(f"[Gamma] {len([x for x in result if x])} 个有效市场")
        return [x for x in result if x]
    except Exception as e:
        get_logger().warning(f"[Gamma] 失败: {e}"); return []

def _fetch_graphql() -> List[Market]:
    """CLOB GraphQL（第三数据源）"""
    try:
        q = """{markets(limit:200,closed:false,archived:false,orderBy:"volume",order:"desc")
               {id slug question volume liquidity outcomePrices closed resolved}}"""
        r = S.post("https://clob.polymarket.com/graphql",
                   json={"query":q},timeout=12)
        r.raise_for_status()
        markets = r.json().get("data",{}).get("markets",[]) or []
        result = [_make(m.get("slug",""),m,"graphql") for m in markets]
        get_logger().info(f"[GraphQL] {len([x for x in result if x])} 个有效市场")
        return [x for x in result if x]
    except Exception as e:
        get_logger().warning(f"[GraphQL] 失败: {e}"); return []

def collect_all() -> List[Market]:
    for fn in [_fetch_clob, _fetch_gamma, _fetch_graphql]:
        ms = fn()
        if ms: return ms
    get_logger().warning("[Collector] 所有 API 均不可用")
    return []

# ═══════════════════════════════════════════════════════════
# L1 检测器：概率密度套利
# ═══════════════════════════════════════════════════════════
class L1Detector:
    """Yes + No > 1.0 + 手续费 = 零风险套利"""
    def __init__(self, cfg: Config):
        self.cfg = cfg
    def check(self, m: Market) -> Optional[Opp]:
        if m.resolved or m.yes_price<=0 or m.no_price<=0: return None
        total  = m.yes_price + m.no_price
        spread = total - 1.0
        net_y  = (1.0 - m.yes_price) * (1 - POLYMARKET_FEE)
        net_n  = (1.0 - m.no_price)  * (1 - POLYMARKET_FEE)
        net_r  = min(net_y, net_n)
        if (spread >= self.cfg.l1_min_spread
                and net_r >= self.cfg.l1_min_net_return
                and m.volume_usd >= self.cfg.l1_min_volume):
            return Opp(
                layer="L1", market_id=m.market_id, question=m.question,
                yes_price=m.yes_price, no_price=m.no_price,
                spread_pct=round(spread*100,3), net_return_pct=round(net_r*100,3),
                volume_usd=m.volume_usd, source=m.source,
                url=m.url(), ts=m.ts)
        return None
    def scan(self, markets: List[Market]) -> List[Opp]:
        return sorted([o for m in markets if (o:=self.check(m))],
                      key=lambda x: x.spread_pct, reverse=True)

# ═══════════════════════════════════════════════════════════
# L2 检测器已移除（用户不需要跨平台套利）
# ═══════════════════════════════════════════════════════════
class L2Detector:
    """
    通过关键词匹配在 Kalshi 找同事件市场
    原理：同类事件在两个平台价格不一致 → 套利空间
    """
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _keywords(self, q: str) -> List[str]:
        """从问题中提取关键词用于匹配"""
        # 去除常见词，保留核心词
        stop = {"will","what","is","the","a","an","of","to","in","on","at",
                "be","are","was","were","does","did","can","could","should",
                "how","when","which","who","that","this","it","for","by"}
        words = [w.lower().strip("?.,!") for w in q.split()]
        return [w for w in words if len(w)>3 and w not in stop]

    def _match(self, p_question: str, k_question: str) -> float:
        """简单词重叠匹配，返回 0.0~1.0"""
        p_kw = set(self._keywords(p_question))
        k_kw = set(self._keywords(k_question))
        if not p_kw or not k_kw: return 0.0
        overlap = len(p_kw & k_kw)
        return overlap / min(len(p_kw), len(k_kw))

    def check_market(self, m: Market) -> Optional[Opp]:
        """检测单个 Polymarket 市场是否有跨平台套利机会"""
        if m.resolved or m.volume_usd < self.cfg.l2_min_volume: return None

        # 注意：Kalshi API 需要认证，这里演示匹配逻辑
        # 实际实现需要调用 Kalshi API（见 README）
        # 暂时只标记"高成交量+极端价格"的市场供人工研究
        extreme = max(m.yes_price, m.no_price)
        if extreme > 0.85 and m.volume_usd > 100_000:
            return Opp(
                layer="L2", market_id=m.market_id, question=m.question,
                yes_price=m.yes_price, no_price=m.no_price,
                spread_pct=round((extreme-0.5)*100, 3),  # 简化：价差估算
                net_return_pct=round((extreme-0.85)*100, 3),
                volume_usd=m.volume_usd, source=m.source,
                url=m.url(), ts=m.ts,
                extra={"note": "高成交量极端价格市场，需人工检查Kalshi是否有价差"})
        return None

    def scan(self, markets: List[Market]) -> List[Opp]:
        return sorted([o for m in markets if (o:=self.check_market(m))],
                      key=lambda x: x.volume_usd, reverse=True)

# ═══════════════════════════════════════════════════════════
# L3 标记器：研究驱动机会（我的专长）
# ═══════════════════════════════════════════════════════════
class L3Marker:
    """
    标记需要人工研究的高价值市场
    L3 不自动执行，只提供研究线索，供我深度分析
    """
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def mark(self, markets: List[Market]) -> List[Opp]:
        opps = []
        for m in markets:
            if m.resolved: continue
            if m.volume_usd < 50_000: continue   # 只看高流动性
            if m.yes_price < 0.15: continue        # 概率太低，不研究
            if m.yes_price > 0.85: continue         # 概率太高，不研究

            # 符合研究价值的市场
            opp = Opp(
                layer="L3", market_id=m.market_id, question=m.question,
                yes_price=m.yes_price, no_price=m.no_price,
                spread_pct=round(abs(m.yes_price-0.5)*100, 2),
                net_return_pct=round(abs(m.yes_price-0.5)*50, 2),  # 粗估
                volume_usd=m.volume_usd, source=m.source,
                url=m.url(), ts=m.ts,
                extra={"reasoning": "高成交量+中间概率，市场定价可能存在偏差，值得深入研究"}
            )
            opps.append(opp)

        # 按成交量排序
        opps.sort(key=lambda x: x.volume_usd, reverse=True)
        return opps[:20]  # 最多返回20个研究线索

# ═══════════════════════════════════════════════════════════
# 通知推送
# ═══════════════════════════════════════════════════════════
def push_notify(msg: str) -> bool:
    """推送通知（当前通过 OpenClaw 路由到微信）"""
    try:
        from tools import message as mt
        mt(action="send", channel="weixin", message=msg)
        get_logger().info("✅ 微信推送成功")
        return True
    except Exception as e:
        get_logger().warning(f"微信推送失败: {e}")
        return False

def push_email(subject: str, body: str, cfg: dict) -> bool:
    """Email 通知（需要配置 SMTP）"""
    import smtplib
    from email.mime.text import MIMEText
    if not cfg.get("enabled"): return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = cfg["username"]
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=10) as s:
            s.starttls()
            s.login(cfg["username"], cfg["password"])
            s.sendmail(cfg["username"], [cfg["to"]], msg.as_string())
        get_logger().info(f"📧 Email 发送成功: {subject}")
        return True
    except Exception as e:
        get_logger().warning(f"📧 Email 发送失败: {e}")
        return False

def push_discord(msg: str, webhook_url: str) -> bool:
    """Discord Webhook 通知"""
    if not webhook_url: return False
    try:
        r = requests.post(webhook_url, json={"content": msg}, timeout=10)
        if r.status_code in (200,204):
            get_logger().info("💬 Discord 推送成功")
            return True
        return False
    except Exception as e:
        get_logger().warning(f"💬 Discord 推送失败: {e}")
        return False

def notify(opps: List[Opp], all_opps: List[Opp], cfg: dict):
    """统一通知入口"""
    if not opps: return

    # 1. 微信（通过 OpenClaw）
    for i, o in enumerate(opps[:3], 1):   # 最多推送前3个
        push_notify(o.to_msg(rank=i))

    # 2. Email
    body = f"小木 Polymarket 监控报告\n{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
    for i, o in enumerate(opps, 1):
        body += f"[{o.confidence()}] {o.question[:80]}\n"
        body += f"  Yes={o.yes_price:.2%} No={o.no_price:.2%} 空间={o.spread_pct:.2f}%\n\n"
    push_email("Polymarket 套利机会提醒", body, cfg.get("email",{}))

    # 3. Discord
    lines = [f"**Polymarket 套利机会 {datetime.now().strftime('%H:%M')}**"]
    for i, o in enumerate(opps[:5], 1):
        lines.append(f"`{i}` {o.confidence()} {o.question[:60]}")
        lines.append(f"   Yes={o.yes_price:.2%} No={o.no_price:.2%} 空间={o.spread_pct:.2f}%")
    push_discord("\n".join(lines), cfg.get("discord_webhook",""))

# ═══════════════════════════════════════════════════════════
# 核心扫描逻辑
# ═══════════════════════════════════════════════════════════
def run_scan(conn, cfg: Config, test=False):
    t0 = datetime.now(timezone.utc)
    ts = t0.isoformat()

    markets = collect_all()
    get_logger().info(f"采集 {len(markets)} 个市场")

    l1 = L1Detector(cfg).scan(markets)
    l3 = L3Marker(cfg).mark(markets)
    all_opps = l1 + l3
    get_logger().info(f"检测: L1={len(l1)} L3={len(l3)}")

    # 新机会判断（对比上次快照）
    last_ids = load_last_ids()
    new_opps = [o for o in all_opps if o.market_id not in last_ids]

    # 记录到 DB
    for o in l1: save_opp(conn, o, "L1")
    for o in l3: save_opp(conn, o, "L3")

    # 新机会 → 通知 + 写标记文件
    if new_opps and not test:
        notify(new_opps, all_opps, cfg.get("notifications",{}))
        NEWOPP.write_text(json.dumps([o.to_dict() for o in new_opps], ensure_ascii=False, indent=2))
        get_logger().info(f"🎯 新机会 {len(new_opps)} 个，已推送")
    else:
        if NEWOPP.exists(): NEWOPP.unlink()

    save_snapshot(all_opps)
    ms = int((datetime.now(timezone.utc)-t0).total_seconds()*1000)
    save_scan(conn, ts, len(markets), len(l1), 0, len(l3), ms)

    if test:
        print(f"\n📊 测试扫描结果:")
        print(f"  L1 概率密度套利：{len(l1)} 个")
        for o in l1: print(f"    [{o.confidence()}] {o.question[:55]}")
        print(f"  L3 研究线索：{len(l3)} 个（详见 last_scan.json）")
        s = calc_stats(conn)
        print(f"\n📈 24小时统计: 扫描{s['scans_24h']}次 发现{s['l1_total']+s.get('l3_total',0)}个机会")

    return all_opps

# ═══════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once",  action="store_true", help="单次扫描（GitHub Actions）")
    ap.add_argument("--test",   action="store_true", help="测试模式（不推送）")
    ap.add_argument("--stats", action="store_true", help="查看24小时统计")
    args = ap.parse_args()

    cfg = Config.from_yaml()
    conn = init_db()

    if args.stats:
        s = calc_stats(conn)
        print(f"\n📊 Polymarket Scanner 24小时统计")
        print(f"   扫描次数：{s['scans_24h']} 次")
        print(f"   L1 概率密度套利：{s['l1_total']} 个")        print(f"   L3 研究线索：{s['l3_total']} 个")
        print(f"   推送告警：{s['alerts_24h']} 次")
        print(f"   平均耗时：{s['avg_ms']:.0f} ms")
        return

    log = get_logger()

    if args.test:
        log.info("🧪 测试模式")
        run_scan(conn, cfg, test=True)
        return

    if args.once:
        run_scan(conn, cfg)
    else:
        log.info(f"🚀 Polymarket Scanner 启动，间隔 {cfg.polling_interval}s")
        while True:
            run_scan(conn, cfg)
            _time.sleep(cfg.polling_interval)

if __name__ == "__main__":
    main()
