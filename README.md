# Polymarket 套利监控系统

小木系统 / Edwardsamaxl / 2026-04-23

## 三层检测架构

| 层级 | 策略 | 自动化 |
|---|---|---|
| L1 | 概率密度套利（Yes+No>1.02）| ✅ 自动发现推送 |
| L2 | 跨平台套利（Polymarket vs Kalshi）| ✅ 自动发现推送 |
| L3 | 研究驱动机会（市场错误定价）| 🔍 标记供我研究 |

## 系统架构

```
GitHub Actions Runner（每分钟）
  → Polymarket API（gamma/clob）
  → 三层检测
  → 发现机会 → 写 data/new_opportunity.json
  → 上传 artifact
小木（监控层）
  → 检测 new_opportunity.json 变化
  → 推送到微信
```

## GitHub Actions 设置

在仓库根目录创建 `.github/workflows/scan.yml`：

```yaml
name: Polymarket Scanner
on:
  schedule:
    - cron: '*/1 * * * *'
  workflow_dispatch:
jobs:
  scan:
    runs-on: ubuntu-latest
    timeout-minutes: 3
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install requests pyyaml -q
      - run: python3 polymarket_scanner.py --once
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: scan-results
          path: data/
          retention-days: 1
```

## 本地测试

```bash
pip install requests pyyaml
python3 polymarket_scanner.py --test
python3 polymarket_scanner.py --stats
```

by 小木
