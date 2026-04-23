#!/bin/bash
# Polymarket Scanner 启动脚本
cd /workspace/polymarket-scanner
pip install requests pyyaml -q
python3 src/scanner.py --once
