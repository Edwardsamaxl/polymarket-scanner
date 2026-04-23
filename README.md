# Polymarket 套利监控系统

Polymarket 概率密度套利自动监控，发现机会推送到微信。

## 架构
- **GitHub Actions Runner**（完整外网访问）执行扫描
- **微信通知**：发现机会推送到微信
- **数据库**：SQLite 记录每次机会

## 使用
1. 手动触发 `workflow_dispatch` 或等每分钟自动运行
2. 发现机会时，GitHub Actions 日志会显示推送结果
3. `python3 polymarket_scanner.py --stats` 查看24小时统计

## 配置
修改 `config.yaml` 中的参数。

by 小木 / Edwardsamaxl / 2026-04-23
