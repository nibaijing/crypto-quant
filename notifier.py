"""Telegram 通知模块 - 直接通过 send_message 工具发送

run_live.py 调用 notify_trade() 把信号写入日志并保存到通知队列
Hermes agent 检测到通知队列有新内容时通过 send_message 推送到 Telegram
"""

import json
from datetime import datetime
from pathlib import Path

NOTIFY_QUEUE = Path(__file__).parent / "data" / "pending_notifications.json"


def notify_trade(signal_type: str, price: float, details: str = ""):
    """写入交易信号通知队列。

    Args:
        signal_type: "LONG" / "SELL" / "SHORT" / "COVER" / "STOP_LOSS" / "TAKE_PROFIT"
        price: 成交价格
        details: 额外描述
    """
    signal = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "type": signal_type,
        "price": f"${price:,.2f}",
        "details": details,
    }

    NOTIFY_QUEUE.parent.mkdir(parents=True, exist_ok=True)

    # 追加到队列
    existing = []
    if NOTIFY_QUEUE.exists():
        try:
            existing = json.loads(NOTIFY_QUEUE.read_text())
        except:
            pass

    existing.append(signal)
    # 只保留最近 20 条
    if len(existing) > 20:
        existing = existing[-20:]

    NOTIFY_QUEUE.write_text(json.dumps(existing, ensure_ascii=False, indent=2))


def drain_notifications() -> list[dict]:
    """取出并清空所有待发通知"""
    if not NOTIFY_QUEUE.exists():
        return []

    try:
        existing = json.loads(NOTIFY_QUEUE.read_text())
    except:
        return []

    # 清空文件
    NOTIFY_QUEUE.write_text("[]")
    return existing


def notify_status(equity: float, return_pct: float, trades: int, win_rate: float):
    """发送定期状态更新"""
    notify_trade("STATUS", equity, f"回报={return_pct:+.2f}% 交易={trades} 胜率={win_rate:.1f}%")