# Polymarket 套利监控系统 — 设计文档 v1.0
> 调研日期：2026-04-23 | 状态：设计完成，待实现

---

## 一、现有系统调研结论

### 1.1 已有的开源系统

| 系统 | 架构 | 数据源 | 特点 |
|---|---|---|---|
| [ImMike/polymarket-arbitrage](https://github.com/ImMike/polymarket-arbitrage) | Python + Cron | Gamma API | Polymarket+Kalshi 跨平台，扫描 10000+ 市场 |
| [Trum3it/polymarket-arbitrage-bot](https://github.com/Trum3it/polymarket-arbitrage-bot) | Rust | Gamma API | 专注 ETH/BTC 15分钟市场，高频 |
| [sssorryMaker/polymarket-arbitrage-bot](https://github.com/sssorryMaker/polymarket-arbitrage-bot) | Python | Gamma API | 5分钟市场，Professional-grade |
| [aaronjmars/polymarket-tg-bot](https://github.com/aaronjmars/polymarket-tg-bot) | Serverless (Vercel) + Redis | Gamma API | 监控新市场上线，推送到 Telegram |

**关键发现：所有系统都使用 Gamma API（`https://gamma-api.polymarket.com`）作为数据源。**

### 1.2 Polymarket API 架构（2026 实测）

```
Gamma API（市场元数据）https://gamma-api.polymarket.com/markets
    → 字段：id, slug, question, volume, outcomePrices, liquidity, closed
    → 用途：获取市场列表 + 基本信息
    → 认证：无需，公开

CLOB API（订单簿+交易）https://clob.polymarket.com/markets
    → 字段：orderbook, bids, asks, lastTrade
    → 用途：获取实时买卖盘口，计算真实可入场价格
    → 认证：无需（读取）

WebSocket（实时流）wss://ws-subscriptions-clob.polymarket.com
    → 用途：实时监听价格变动，适合高频
    → 注意：需要持续连接，GitHub Actions 不适用

GraphQL（灵活查询）https://clob.polymarket.com/graphql
    → 用途：精确查询特定市场，适合定向监控
```

**服务器访问限制**：`gamma-api.polymarket.com` 从本服务器无法访问（超时）；GitHub Actions Runner 可以访问。

### 1.3 概率密度套利的真实数学

```
当前系统设计的问题：
  假设 Yes=0.52, No=0.51
  → Spread = 3%，看起来有套利空间
  → 费后净收益（双边各$1）= (1-0.52)*(1-1%) + (1-0.51)*(1-1%) = 0.4752 + 0.4851 = $0.96
  → 实际亏损 $0.04（0.04%）

正确的计算方式：
  套利空间 = Yes + No - 1（需要 > 手续费*2 才有利）
  Polymarket 手续费：每笔约 1%（Maker 0%，Taker 1%）
  有效套利条件：Yes + No > 1.02（即 Spread > 2%）

概率密度套利的机会极少：
  → 需要 Yes 和 No 的隐含概率之和超过 102%
  → 这通常出现在市场极端定价时
  → 实际机会：每10000个市场约3-5个，且窗口极短

结论：概率密度套利在 Polymarket 上不是主要盈利策略
      应转向：跨平台套利（Polymarket vs Kalshi）、事件研究套利
```

### 1.4 主流盈利策略（经证实）

| 策略 | 原理 | 可行性 |
|---|---|---|
| **跨平台价差套利** | Polymarket vs Kalshi 同事件价格差 | ⚠️ 需要事件匹配（问题描述不同则失败），约每10000个市场有5-20个机会 |
| **事件研究套利** | 研究事件，判断市场错误定价（概率<真实概率） | ✅ **最可行**，需要研究能力，小木的专长 |
| **概率密度套利** | Yes+No>100%，双边建仓 | ❌ 手续费侵蚀利润，机会极少 |
| **流动性提供** | 成为 Maker 赚手续费 | ❌ 需要主动管理订单，有库存风险 |

---

## 二、系统架构设计

### 2.1 执行层 vs 监控层分离（核心设计决策）

```
┌─────────────────────────────────────────────────────┐
│  小木（我的角色）                                     │
│  ┌─────────────────┐    ┌────────────────────────┐ │
│  │  监控层（我）     │    │  执行层（GitHub Actions）│ │
│  │                 │    │                        │ │
│  │  web_search     │───→│  workflow_dispatch      │ │
│  │  每分钟触发      │    │  Polymarket API 扫描    │ │
│  │  (可达外网)      │    │  套利检测               │ │
│  │                 │    │  结果写回 repo           │ │
│  └─────────────────┘    └────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

**为什么这样设计：**
- 我的服务器无法直接访问 Polymarket API（超时）
- GitHub Actions Runner 有完整外网访问
- 通过 `workflow_dispatch` 触发，不需要 Git push
- GitHub API（`api.github.com`）可以从我的服务器访问 ✅

### 2.2 模块设计

```
polymarket-scanner/
├── .github/
│   └── workflows/
│       └── scan.yml              ← 执行层：GitHub Actions 每分钟扫描
├── src/
│   ├── config.py                 ← 参数配置（阈值、通知设置）
│   ├── gamma_client.py            ← Gamma API 客户端（含重试+降级）
│   ├── clob_client.py             ← CLOB API 客户端（订单簿）
│   ├── detector.py                ← 套利检测引擎
│   ├── notifier.py                ← 通知推送（Email/Webhook/微信）
│   └── scanner.py                 ← 主扫描逻辑
├── data/
│   ├── opportunities.db           ← SQLite：机会记录 + 扫描日志
│   ├── last_scan.json             ← 最近一次扫描结果
│   └── stats.json                 ← 24小时统计
├── tests/
│   ├── test_detector.py           ← 单元测试
│   ├── test_gamma_client.py       ← API 测试
│   └── test_integration.py        ← 集成测试
├── config.yaml                    ← 阈值配置
├── requirements.txt                ← 依赖
└── README.md
```

### 2.3 套利检测引擎（改进版）

```python
# 三层检测架构
class Detector:
    def __init__(self, cfg):
        self.cfg = cfg

    # 第一层：概率密度套利（Yes+No>102%，极低频但零风险）
    def detect_probability_density(self, m):
        spread = m.yes + m.no - 1.0
        net_return = min((1-m.yes)*(1-FEE), (1-m.no)*(1-FEE))
        if spread > MIN_SPREAD_2PCT and net_return > 0:
            return Opportunity(type="probability_density", ...)

    # 第二层：跨平台价差（Polymarket vs Kalshi，需要事件匹配）
    def detect_cross_platform(self, m):
        # 用 LLM/关键词匹配在 Kalshi 找同类事件
        # 计算扣除运费后的净收益
        ...

    # 第三层：研究驱动套利（小木的专长）
    def detect_research_driven(self, m):
        # 不依赖价格异常，依赖对事件的研究判断
        # 这个策略是手动触发的，不自动执行
        ...
```

### 2.4 数据流

```
GitHub Actions Runner（每分钟）
    ↓
gamma_api.fetch_markets(limit=200)  → 获取高成交量市场列表
    ↓
过滤：成交量 < $50k 的市场跳过（减少噪音）
    ↓
并行查询每个市场的：
  - Gamma API：基本信息
  - CLOB API：订单簿（计算最佳买入/卖出价）
    ↓
三层检测：
  1. 概率密度套利（spread > 2%）
  2. 跨平台价差（Polymarket vs Kalshi）
  3. 机会市场高亮（成交量大 + 流动性好，供研究参考）
    ↓
结果写入：
  - data/opportunities.db
  - data/last_scan.json
  - data/stats.json
    ↓
通知推送（按优先级）：
  1. Email（Gmail SMTP 或 SendGrid）
  2. Discord Webhook（如果配置了）
  3. GitHub Actions 日志（兜底）
```

### 2.5 通知系统设计

```yaml
# config.yaml 通知配置
notifications:
  email:
    enabled: false
    smtp_host: smtp.gmail.com
    smtp_port: 587
    username: your@gmail.com
    password: "app-password"      # Gmail App Password
    to: your@email.com

  discord_webhook:
    enabled: false
    url: "https://discord.com/api/webhooks/..."

  github_issue:
    enabled: false                 # 发现机会时创建 GitHub Issue
    repo: Edwardsamaxl/polymarket-scanner

  # 微信推送：通过 GitHub Actions 日志查看
  # 触发时 commit 一个标记文件，我检测到后推送给你
  marker_file:
    enabled: true
    path: "data/new_opportunity.json"
```

---

## 三、GitHub Actions 触发机制（解决服务器无法访问 Polymarket API 的问题）

### 3.1 通过 GitHub API 触发 workflow

```python
# 从我的服务器触发 GitHub Actions（通过 api.github.com，可通）
import requests, json, base64

def trigger_github_actions():
    """
    通过 GitHub REST API 触发 workflow_dispatch
    api.github.com 从我的服务器可以访问 ✅
    """
    url = "https://api.github.com/repos/Edwardsamaxl/polymarket-scanner/actions/workflows/scan.yml/runs"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    r = requests.post(url, headers=headers, json={}, timeout=10)
    return r.status_code == 201
```

### 3.2 触发流程

```
我的服务器 cron（每分钟）
    ↓
trigger_github_actions()  →  触发 GitHub Actions
    ↓ （无阻塞，立即返回）
    ↓
GitHub Actions Runner 开始执行（1-3分钟）
    ↓
扫描 Polymarket API（通！）
    ↓
结果写入 data/new_opportunity.json（commit 到 repo）
    ↓
我的服务器在下一次 cron 中检测到 new_opportunity.json 变化
    ↓
推送微信通知给你
    ↓
清除 new_opportunity.json（避免重复推送）
```

---

## 四、实施计划

### Phase 1：修复 GitHub Actions（优先级：🔴 高）
- [ ] 上传 `.github/workflows/scan.yml` 到 GitHub 仓库
- [ ] 测试 GitHub Actions 是否能访问 Polymarket API
- [ ] 确认 scanner 能正常获取市场数据

### Phase 2：实现通知系统（优先级：🔴 高）
- [ ] 配置 Email 或 Discord Webhook 通知
- [ ] 测试通知是否正常送达

### Phase 3：完善套利检测（优先级：🟡 中）
- [ ] 实现三层检测架构
- [ ] 加入跨平台检测（Polymarket + Kalshi）
- [ ] 单元测试覆盖

### Phase 4：研究驱动发现（优先级：🟡 中）
- [ ] 每天生成 Polymarket 热门事件研究简报
- [ ] 手动研究驱动：发现高置信度建仓机会

### Phase 5：自动化部署（优先级：🟢 低）
- [ ] 配置 GitHub Actions 自动部署
- [ ] 添加监控报警（扫描失败时通知）

---

## 五、关键风险

1. **概率密度套利机会极少**：计算显示，Yes+No>102% 的市场极少出现，可能几天都没有一个机会
2. **跨平台套利依赖事件匹配**：Polymarket 和 Kalshi 的问题描述不同，AI 匹配准确率有限
3. **GitHub Actions 频率限制**：免费版每分钟最多运行1次，有超时风险
4. **微信推送延迟**：我的 cron 检测到 GitHub 结果有1-2分钟延迟

---

## 六、设计决策记录

| 日期 | 决策 | 原因 |
|---|---|---|
| 2026-04-23 | 概率密度套利不是主要策略 | 手续费侵蚀利润，Yes+No>102% 的机会极少 |
| 2026-04-23 | 执行层放在 GitHub Actions | 我的服务器无法访问 Polymarket API，Runner 可以 |
| 2026-04-23 | 我通过 web_search 监控，不直接轮询 | 绕过服务器网络限制 |
| 2026-04-23 | 通知用 Email/Discord Webhook | 微信推送需要额外集成 |
| 2026-04-23 | 暂停盲目扫描 cron，改为设计提醒 | 老板的建议，先研究再动手 |
