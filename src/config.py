"""
config.py — 配置加载 + 验证
借鉴 ImMike 的 config.yaml 结构
"""

import os
import yaml
from dataclasses import dataclass, field
from typing import List

# ── 数据类定义 ────────────────────────────────────────────────────
@dataclass
class APIConfig:
    polymarket_rest:   str = "https://clob.polymarket.com"
    gamma_api:          str = "https://gamma-api.polymarket.com"
    kalshi_api:         str = "https://api.elections.kalshi.com/trade-api/v2"
    timeout:            int = 30
    max_retries:        int = 3
    retry_delay:         int = 1

@dataclass
class TradingConfig:
    data_mode:          str = "real"          # "real" / "simulation"
    trading_mode:       str = "dry_run"        # "live" / "dry_run"
    cross_platform:     bool = True
    min_match_similarity: float = 0.60

    # 订单参数
    default_order_size: float = 5.0           # 默认每单 $5
    min_order_size:     float = 2.0
    max_order_size:     float = 10.0
    slippage_tolerance: float = 0.02          # 2%
    order_timeout:       int = 60             # 秒

    # 手续费（Polymarket）
    maker_fee_bps:      int = 0               # 0%（Maker 费率）
    taker_fee_bps:      int = 150             # 1.5%（Taker 费率）

@dataclass
class RiskConfig:
    max_position_per_market: float = 15.0      # 单市场 $15
    max_global_exposure:     float = 50.0     # 全局总敞口 $50
    max_daily_loss:          float = 10.0      # 日亏损上限 $10
    max_drawdown_pct:        float = 0.15     # 15% 回撤上限
    kill_switch_enabled:     bool = True
    auto_unwind_on_breach:   bool = False

@dataclass
class DetectorConfig:
    # L1 概率密度套利
    l1_enabled:         bool = True
    l1_min_spread:       float = 0.02         # Yes+No > 1.02
    l1_min_net_return:   float = 0.003        # 费后净收益 > 0.3%
    l1_min_volume:       float = 10_000

    # L2 跨平台套利
    l2_enabled:         bool = True
    l2_min_spread:       float = 0.02         # 两平台价差 > 2%
    l2_min_volume:       float = 50_000
    l2_min_similarity:   float = 0.60         # 事件匹配最低相似度

    # L3 研究线索
    l3_enabled:         bool = True
    l3_min_volume:       float = 50_000

    # 通用
    global_min_volume:  float = 5_000

@dataclass
class NotifyConfig:
    # 微信（OpenClaw）
    wechat_enabled:     bool = True
    # Email
    email_enabled:       bool = False
    smtp_host:          str = "smtp.gmail.com"
    smtp_port:          int = 587
    email_username:     str = ""
    email_password:     str = ""
    email_to:           str = ""
    # Discord
    discord_enabled:     bool = False
    discord_webhook:     str = ""
    # 冷静期（秒）
    l1_cooldown:        int = 0               # 立即通知
    l2_cooldown:        int = 0
    l3_cooldown:        int = 14400            # 4小时

@dataclass
class Config:
    api:       APIConfig    = field(default_factory=APIConfig)
    trading:   TradingConfig = field(default_factory=TradingConfig)
    risk:      RiskConfig   = field(default_factory=RiskConfig)
    detector:  DetectorConfig = field(default_factory=DetectorConfig)
    notify:    NotifyConfig  = field(default_factory=NotifyConfig)

    # 日志
    log_level: str = "INFO"
    log_dir:    str = "logs"

    @classmethod
    def from_yaml(cls, path: str = "config.yaml") -> "Config":
        if not os.path.exists(path):
            return cls()

        with open(path) as f:
            d = yaml.safe_load(f) or {}

        cfg = cls()

        # API 配置
        if "api" in d:
            for k, v in d["api"].items():
                if hasattr(cfg.api, k): setattr(cfg.api, k, v)

        # 交易配置
        if "trading" in d:
            for k, v in d["trading"].items():
                if hasattr(cfg.trading, k): setattr(cfg.trading, k, v)

        # 风控配置
        if "risk" in d:
            for k, v in d["risk"].items():
                if hasattr(cfg.risk, k): setattr(cfg.risk, k, v)

        # 检测器配置
        if "detector" in d:
            for k, v in d["detector"].items():
                if hasattr(cfg.detector, k): setattr(cfg.detector, k, v)

        # 通知配置
        if "notifications" in d:
            n = d["notifications"]
            cfg.notify.wechat_enabled = n.get("wechat", {}).get("enabled", True)
            if "email" in n:
                cfg.notify.email_enabled = n["email"].get("enabled", False)
                cfg.notify.smtp_host     = n["email"].get("smtp_host", "smtp.gmail.com")
                cfg.notify.smtp_port     = n["email"].get("smtp_port", 587)
                cfg.notify.email_username = n["email"].get("username", "")
                cfg.notify.email_password = n["email"].get("password", "")
                cfg.notify.email_to       = n["email"].get("to", "")
            if "discord_webhook" in n:
                cfg.notify.discord_enabled = n["discord_webhook"].get("enabled", False)
                cfg.notify.discord_webhook = n["discord_webhook"].get("url", "")

        # 日志
        if "logging" in d:
            cfg.log_level = d["logging"].get("level", "INFO")
            cfg.log_dir    = d["logging"].get("dir", "logs")

        return cfg

    def validate(self) -> List[str]:
        """验证配置合法性，返回警告列表"""
        warnings = []

        if self.trading.default_order_size > self.risk.max_position_per_market:
            warnings.append(f"警告: default_order_size ({self.trading.default_order_size}) > max_position_per_market ({self.risk.max_position_per_market})")

        if self.trading.slippage_tolerance > 0.05:
            warnings.append(f"警告: slippage_tolerance ({self.trading.slippage_tolerance:.1%}) > 5%，可能导致订单无法成交")

        if self.detector.l1_min_spread < 0.01:
            warnings.append("提示: l1_min_spread < 1%，可能产生大量噪音信号")

        if self.risk.max_global_exposure < self.risk.max_position_per_market * 2:
            warnings.append(f"提示: max_global_exposure 低于单市场上限的2倍，限制了分散投资")

        return warnings


# ── 配置文件模板（写入 config.yaml.example）────────────────────
CONFIG_TEMPLATE = """
# Polymarket Scanner 配置
# ========================
# 完整配置参考：https://github.com/Edwardsamaxl/polymarket-scanner

api:
  polymarket_rest: "https://clob.polymarket.com"
  gamma_api:        "https://gamma-api.polymarket.com"
  kalshi_api:       "https://api.elections.kalshi.com/trade-api/v2"
  timeout:          30
  max_retries:       3

trading:
  data_mode:         "real"          # real / simulation
  trading_mode:      "dry_run"       # live / dry_run
  cross_platform:     true
  min_match_similarity: 0.60
  default_order_size: 5.0
  min_order_size:     2.0
  max_order_size:     10.0
  slippage_tolerance: 0.02
  order_timeout:       60
  maker_fee_bps:       0
  taker_fee_bps:       150

risk:
  max_position_per_market: 15.0
  max_global_exposure:     50.0
  max_daily_loss:          10.0
  max_drawdown_pct:        0.15
  kill_switch_enabled:      true
  auto_unwind_on_breach:  false

detector:
  # L1 概率密度套利
  l1_enabled:         true
  l1_min_spread:       0.02
  l1_min_net_return:   0.003
  l1_min_volume:       10000

  # L2 跨平台套利
  l2_enabled:         true
  l2_min_spread:       0.02
  l2_min_volume:       50000
  l2_min_similarity:   0.60

  # L3 研究线索
  l3_enabled:         true
  l3_min_volume:       50000

  global_min_volume:   5000

notifications:
  wechat:
    enabled: true

  email:
    enabled: false
    smtp_host: smtp.gmail.com
    smtp_port: 587
    username: your@gmail.com
    password: "your-app-password"
    to: your@email.com

  discord_webhook:
    enabled: false
    url: "https://discord.com/api/webhooks/..."

logging:
  level: INFO
  dir: logs
"""
