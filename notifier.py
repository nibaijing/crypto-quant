"""Telegram 通知模块 - 通过 Hermes 桥接

run_live.py 调用 notify_trade() 把信号持久化到文件
Hermes cronjob 每分钟读取并推送到 Telegram
"""

import json
import os
from datetime import datetime
from pathlib import Path

SIGNAL_FILE = os.path.expanduser("~/.hermes/cron/output/pending_trade_signal.txt")
DELIVERED_FILE = os.path.expanduser("~/.hermes/cron/output/delivered_trade_signal.txt")


def notify_trade(signal_type: str, price: float, details: str = ""):
    """发送交易通知（写入文件，由 Hermes 桥接发送）
    
    Args:
        signal_type: "BUY" / "SELL" / "SHORT" / "COVER" / "STOP_LOSS" / "TAKE_PROFIT"
        price: 成交价格
        details: 额外描述
    """
    signal = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "type": signal_type,
        "price": f"${price:,.2f}",
        "details": details,
    }
    
    # 确保目录存在
    Path(SIGNAL_FILE).parent.mkdir(parents=True, exist_ok=True)
    
    # 追加写入（一行一个JSON）
    with open(SIGNAL_FILE, "a") as f:
        f.write(json.dumps(signal, ensure_ascii=False) + "\n")


def notify_status(equity: float, return_pct: float, trades: int, win_rate: float):
    """发送定期状态更新"""
    notify_trade("STATUS", equity, f"回报={return_pct:+.2f}% 交易={trades} 胜率={win_rate:.1f}%")