"""
event_matcher.py — 事件匹配算法
基于 TopTrenDev/polymarket-kalshi-arbitrage-bot 的 5 维度评分算法
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# ── 字符串相似度（纯 Python 实现，无需第三方库）────────────────────

def jaro_winkler(s1: str, s2: str) -> float:
    """
    Jaro-Winkler 相似度算法（纯 Python 实现）
    返回 0.0~1.0，1.0 表示完全相同
    """
    if s1 == s2:
        return 1.0
    if not s1 or not s2:
        return 0.0

    len1, len2 = len(s1), len(s2)
    match_dist = max(len1, len2) // 2 - 1
    if match_dist < 0:
        match_dist = 0

    s1_matches = [False] * len1
    s2_matches = [False] * len2
    matches = 0
    transpositions = 0

    # 找匹配
    for i in range(len1):
        start = max(0, i - match_dist)
        end   = min(i + match_dist + 1, len2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    # 统计转置
    k = 0
    for i in range(len1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1

    jaro = (matches/len1 + matches/len2 + (matches - transpositions/2)/matches) / 3

    # Winkler 前缀奖励（相同前缀越长，相似度越高）
    prefix = 0
    for i in range(min(4, len1, len2)):
        if s1[i] == s2[i]:
            prefix += 1
        else:
            break

    return jaro + prefix * 0.1 * (1 - jaro)


# ── 停用词 ────────────────────────────────────────────────────────
STOP_WORDS = {
    "will", "would", "could", "should", "may", "might", "must",
    "shall", "can", "need", "dare", "ought", "used",
    "be", "been", "being", "is", "are", "was", "were", "have", "has",
    "had", "do", "does", "did", "doing", "done",
    "the", "a", "an", "and", "or", "but", "if", "then", "else", "when",
    "at", "by", "for", "with", "about", "against", "between", "into",
    "through", "during", "before", "after", "above", "below", "to",
    "from", "up", "down", "in", "out", "on", "off", "over", "under",
    "again", "further", "once", "here", "there", "all", "each", "few",
    "more", "most", "other", "some", "such", "no", "nor", "not", "only",
    "own", "same", "so", "than", "too", "very", "just", "also",
}


# ── 数据结构 ──────────────────────────────────────────────────────
@dataclass
class MatchResult:
    """匹配结果"""
    text_sim:    float   # Jaro-Winkler 文本相似度
    kw_overlap:  float   # Jaccard 关键词重叠系数
    date_match:  bool    # 日期是否匹配
    cat_match:   bool    # 类别是否相同
    num_match:   bool    # 数字是否匹配（金额/年份等）
    overall:     float   # 综合评分 0.0~1.0

    @property
    def confidence(self) -> str:
        if self.overall >= 0.75: return "HIGH"
        if self.overall >= 0.50: return "MED"
        return "LOW"

    def to_dict(self):
        return {
            "text_sim":   self.text_sim,
            "kw_overlap": self.kw_overlap,
            "date_match": self.date_match,
            "cat_match":  self.cat_match,
            "num_match":  self.num_match,
            "overall":    self.overall,
            "confidence": self.confidence,
        }


@dataclass
class Event:
    """事件数据模型"""
    market_id:    str
    question:     str
    description:  str = ""
    end_date:     str = ""    # ISO 格式日期字符串
    category:     str = ""
    yes_price:    float = 0.0
    no_price:      float = 0.0
    volume_usd:    float = 0.0
    source:        str = ""    # "polymarket" / "kalshi" / "simulation"
    url:          str = ""
    ts:           str = ""
    resolved:      bool = False  # 是否已结算


# ── 核心匹配器 ─────────────────────────────────────────────────────
class EventMatcher:
    """
    5 维度事件匹配器
    借鉴：TopTrenDev/polymarket-kalshi-arbitrage-bot/src/event_matcher.rs
    """

    def __init__(self, threshold_high: float = 0.75, threshold_med: float = 0.50):
        self.threshold_high = threshold_high  # ≥0.75：高置信度（自动执行）
        self.threshold_med   = threshold_med   # ≥0.50：中置信度（人工确认）
        # 日期解析缓存（避免重复解析）
        self._date_cache: dict[str, Optional[int]] = {}

    # ── 文本标准化 ──────────────────────────────────────────────
    def normalize(self, text: str) -> str:
        """转小写 + 去除标点"""
        return text.lower().translate(str.maketrans("", "", ".,!?\"'()[]{}"))

    # ── 关键词提取 ──────────────────────────────────────────────
    def keywords(self, text: str) -> set:
        words = self.normalize(text).split()
        return {
            w for w in words
            if len(w) > 2 and w not in STOP_WORDS
        }

    # ── Jaccard 系数 ────────────────────────────────────────────
    def jaccard(self, a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    # ── 日期解析 ────────────────────────────────────────────────
    DATE_PATTERNS = [
        # (正则, 格式说明)
        (r"\b(\d{4})-(\d{2})-(\d{2})\b", "%Y-%m-%d"),
        (r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", "%m/%d/%Y"),
        (r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b", "%m/%d/%y"),
        (r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{1,2}),?\s+(\d{4})\b", "%b %d %Y"),
    ]

    def _parse_ts(self, date_str: str) -> Optional[int]:
        """解析日期字符串为 Unix 时间戳（秒）"""
        if not date_str:
            return None
        if date_str in self._date_cache:
            return self._date_cache[date_str]

        import time
        result = None
        for pat, fmt in self.DATE_PATTERNS:
            m = re.search(pat, date_str, re.IGNORECASE)
            if m:
                try:
                    if fmt == "%b %d %Y":
                        months = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                                  "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
                        month_str = m.group(1)[:3].lower()
                        month = months.get(month_str, 1)
                        day   = int(m.group(2))
                        year  = int(m.group(3))
                        result = int(time.mktime((year, month, day, 0, 0, 0, 0, 0)))
                    else:
                        result = int(time.mktime(time.strptime(m.group(0), fmt)))
                    break
                except (ValueError, OSError):
                    pass

        self._date_cache[date_str] = result
        return result

    def dates_match(self, d1: str, d2: str) -> bool:
        """两个日期相差 ≤ 1 天"""
        t1 = self._parse_ts(d1)
        t2 = self._parse_ts(d2)
        if t1 is None or t2 is None:
            return False
        return abs(t1 - t2) <= 86400

    # ── 数字匹配 ────────────────────────────────────────────────
    def extract_numbers(self, text: str) -> set:
        """提取金额、百分比、年份等数字"""
        return set(re.findall(r"\$[\d,]+(?:\.\d+)?|\d+%|\b\d{4}\b|\b\d{1,3}(?:,\d{3})+\b", text))

    def numbers_match(self, t1: str, t2: str) -> bool:
        n1 = self.extract_numbers(t1)
        n2 = self.extract_numbers(t2)
        return bool(n1 and n2 and n1 & n2)

    # ── 核心评分 ────────────────────────────────────────────────
    def score(self, e1: Event, e2: Event) -> MatchResult:
        """
        计算两个事件的相似度评分（5 维度加权）
        权重：text_sim×0.40 + kw×0.25 + date×0.15 + cat×0.10 + num×0.10
        """
        q1 = e1.question + " " + e1.description
        q2 = e2.question + " " + e2.description

        # 1. Jaro-Winkler 文本相似度 × 0.40
        text_sim = jaro_winkler(self.normalize(e1.question), self.normalize(e2.question))

        # 2. Jaccard 关键词重叠 × 0.25
        kw1 = self.keywords(q1)
        kw2 = self.keywords(q2)
        kw_overlap = self.jaccard(kw1, kw2)

        # 3. 日期匹配 × 0.15
        date_match = self.dates_match(e1.end_date, e2.end_date)

        # 4. 类别匹配 × 0.10
        cat_match = bool(
            e1.category and e2.category
            and e1.category.lower() == e2.category.lower()
        )

        # 5. 数字匹配 × 0.10
        num_match = self.numbers_match(q1, q2)

        # 加权综合评分
        overall = (
            text_sim  * 0.40
            + kw_overlap * 0.25
            + (1.0 if date_match else 0.0) * 0.15
            + (1.0 if cat_match  else 0.0) * 0.10
            + (1.0 if num_match  else 0.0) * 0.10
        )

        return MatchResult(
            text_sim   = round(text_sim, 3),
            kw_overlap = round(kw_overlap, 3),
            date_match = date_match,
            cat_match  = cat_match,
            num_match  = num_match,
            overall    = round(overall, 3),
        )

    # ── 批量匹配 ────────────────────────────────────────────────
    def find_best_match(
        self, target: Event, candidates: List[Event]
    ) -> Tuple[Optional[Event], Optional[MatchResult]]:
        """在候选列表中找最佳匹配"""
        best: Optional[MatchResult] = None
        best_event: Optional[Event] = None

        for cand in candidates:
            result = self.score(target, cand)
            if result.overall >= self.threshold_med and (best is None or result.overall > best.overall):
                best = result
                best_event = cand

        return best_event, best

    def find_all_matches(
        self, targets: List[Event], candidates: List[Event]
    ) -> List[Tuple[Event, Event, MatchResult]]:
        """找所有匹配对（用于跨平台套利检测）"""
        results = []
        for t in targets:
            for c in candidates:
                r = self.score(t, c)
                if r.overall >= self.threshold_med:
                    results.append((t, c, r))

        # 按评分从高到低排序
        results.sort(key=lambda x: x[2].overall, reverse=True)
        return results


# ── 单元测试 ──────────────────────────────────────────────────────
if __name__ == "__main__":
    matcher = EventMatcher()

    # 测试用例
    tests = [
        (
            Event("1", "Will the Fed cut rates in June 2026?", end_date="2026-06-15"),
            Event("2", "Will Fed cut interest rates by June 2026?", end_date="2026-06-15"),
            0.75,  # 期望：高置信度
        ),
        (
            Event("3", "Will BTC be above $100000 in 2026?", end_date="2026-12-31"),
            Event("4", "Will Bitcoin exceed $100k by Dec 2026?", end_date="2026-12-31"),
            0.75,
        ),
        (
            Event("5", "Will unemployment rise above 5%?", end_date="2026-09-30"),
            Event("6", "Will the Lakers win the NBA Finals?", end_date="2026-06-15"),
            0.0,   # 期望：低置信度
        ),
    ]

    print("EventMatcher 单元测试")
    print("=" * 50)
    for i, (e1, e2, min_expected) in enumerate(tests, 1):
        r = matcher.score(e1, e2)
        status = "✅" if r.overall >= min_expected else "❌"
        print(f"  [{i}] {status} 评分={r.overall:.3f} (期望≥{min_expected})")
        print(f"      text_sim={r.text_sim:.3f} kw={r.kw_overlap:.3f} "
              f"date={r.date_match} cat={r.cat_match} num={r.num_match}")
        print(f"      confidence={r.confidence}")
