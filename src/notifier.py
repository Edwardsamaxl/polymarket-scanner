#!/usr/bin/env python3
"""
notifier.py - 监听扫描结果，推送微信通知
配合 GitHub Actions 使用：扫描结果写入 data/last_opportunities.json
本地 cron 每分钟检查一次，有新机会则推送微信
"""

import os
import json
from datetime import datetime
import requests

PREV_FILE = "data/last_opportunities.json"
ALERTS_FILE = "data/last_opportunities.json"

def load_previous():
    if not os.path.exists(PREV_FILE):
        return []
    with open(PREV_FILE) as f:
        return json.load(f)

def save_current(opps):
    os.makedirs("data", exist_ok=True)
    with open(PREV_FILE, "w") as f:
        json.dump(opps, f, ensure_ascii=False, indent=2)

def check_new_opportunities():
    prev = load_previous()
    prev_ids = {o["market_id"] for o in prev}

    if not os.path.exists(ALERTS_FILE):
        return []

    with open(ALERTS_FILE) as f:
        current = json.load(f)

    new = [o for o in current if o["market_id"] not in prev_ids]
    return new

def format_alert(opp: dict) -> str:
    return (
        f"🎯 Polymarket 套利机会\n\n"
        f"问题：{opp['question']}\n"
        f"Yes：{opp['yes_price']:.2%} | No：{opp['no_price']:.2%}\n"
        f"套利空间：{opp['spread']}% | 净收益：{opp['net_return']}%\n"
        f"成交量：${opp['volume_usd']:,.0f}\n\n"
        f"⚠️ 机会可能随时消失，请立即确认"
    )

if __name__ == "__main__":
    new_opps = check_new_opportunities()
    if new_opps:
        print(f"发现 {len(new_opps)} 个新机会")
        for o in new_opps:
            print(format_alert(o))
    else:
        print("无新机会")
