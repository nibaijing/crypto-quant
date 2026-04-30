#!/usr/bin/env python3
"""Dashboard 数据 API — 实时读取状态和价格

数据源:
  1. live_futures_state.json — 模拟盘executor 每5秒写入 (持仓/账户)
  2. okx_live_state.json — 实盘executor 每5秒写入 (交易统计)
  3. okx_orders.json — 实盘executor 订单历史
  4. ws_price_snapshot.json — WS 模块每秒写入当前价格
  5. live_trading.log — 仅用于交易历史回溯
"""

import json
import time
from pathlib import Path
from datetime import datetime
from collections import deque

PROJECT_ROOT = Path(__file__).parent
STATE_FILE = PROJECT_ROOT / "data" / "live_futures_state.json"
OKX_STATE_FILE = PROJECT_ROOT / "data" / "okx_live_state.json"
OKX_ORDERS_FILE = PROJECT_ROOT / "data" / "okx_orders.json"
PRICE_SNAPSHOT = PROJECT_ROOT / "data" / "ws_price_snapshot.json"
LOG_FILE = PROJECT_ROOT / "data" / "live_trading.log"

# 内存价格缓存 (最近 200 个点，去重价格+时间戳)
_price_cache = deque(maxlen=200)
_last_price_ts = None  # (time_str, price) 元组，用于去重


def _read_snapshot():
    """读取 WS 价格快照，去重后缓存到内存"""
    global _last_price_ts
    if not PRICE_SNAPSHOT.exists():
        return None

    try:
        with open(PRICE_SNAPSHOT) as f:
            snap = json.load(f)
    except:
        return None

    price = snap.get("price", 0)
    ts = snap.get("time", "")
    pair = (ts, int(price))  # 取整去重——每秒内多次读取不重复

    if pair != _last_price_ts:
        _last_price_ts = pair
        _price_cache.append({
            "time": ts,
            "price": price,
            "change_pct": snap.get("change_pct", 0),
            "high_24h": snap.get("high_24h", 0),
            "low_24h": snap.get("low_24h", 0),
            "indicators": snap.get("indicators", {}),
        })

    return snap


def get_state():
    """读取当前持仓和账户"""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {"cash": 1000, "total_trades": 0, "winning_trades": 0, "position": None}


def get_recent_prices(limit=60):
    """从缓存获取最近价格点"""
    snap = _read_snapshot()  # 触发可能的缓存追加
    return list(_price_cache)[-limit:]


def get_trade_history():
    """提取交易记录 (从日志 — 兼容多种历史格式)"""
    import re
    trades = []
    if not LOG_FILE.exists():
        return trades

    with open(LOG_FILE) as f:
        lines = f.readlines()

    # 开仓: 做多 / 做空
    long_pattern = re.compile(
        r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约做多.*?@ ([\d.]+).*?\|\s*([\d.]+)x')
    short_pattern = re.compile(
        r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约做空.*?@ ([\d.]+).*?\|\s*([\d.]+)x')

    # 平仓 — 三层格式兼容:
    # L1 新版: 有 净利 + 余额
    sell_new = re.compile(
        r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约平[仓多].*?@ ([\d.]+).*?手续费=\$?([\d.]+).*?净利=\$?([+-]?[\d.]+).*?余额=\$?([\d.]+)')
    cover_new = re.compile(
        r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约平空.*?@ ([\d.]+).*?手续费=\$?([\d.]+).*?净利=\$?([+-]?[\d.]+).*?余额=\$?([\d.]+)')
    # L2 中间版: 有 PnL + 手续费但无净利
    sell_mid = re.compile(
        r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约平[仓多].*?@ ([\d.]+).*?PnL=\$?([+-]?[\d.]+).*?手续费=\$?([\d.]+)')
    cover_mid = re.compile(
        r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约平空.*?@ ([\d.]+).*?PnL=\$?([+-]?[\d.]+).*?手续费=\$?([\d.]+)')
    # L3 最旧版: 只有 PnL
    sell_old = re.compile(
        r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约平[仓多].*?@ ([\d.]+).*?(?:PnL=\$?|盈亏: )([+-]?[\d.]+)')
    cover_old = re.compile(
        r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约平空.*?@ ([\d.]+).*?(?:PnL=\$?|盈亏: )([+-]?[\d.]+)')

    # 按优先级: 新版 → 中间 → 最旧
    rule_set = [
        (long_pattern,   "做多", "open"),
        (short_pattern,  "做空", "open"),
        (sell_new,       "平多", "close_new"),
        (cover_new,      "平空", "close_new"),
        (sell_mid,       "平多", "close_mid"),
        (cover_mid,      "平空", "close_mid"),
        (sell_old,       "平多", "close_old"),
        (cover_old,      "平空", "close_old"),
    ]

    for line in lines:
        for pat, action, fmt in rule_set:
            m = pat.search(line)
            if not m:
                continue
            entry = {"time": m.group(1), "action": action, "price": float(m.group(2)),
                     "leverage": 0, "commission": 0, "net_pnl": 0, "balance": 0, "pnl": 0}

            if fmt == "open":
                entry["leverage"] = float(m.group(3))
            elif fmt == "close_new":
                entry["commission"] = float(m.group(3))
                entry["net_pnl"] = float(m.group(4))
                entry["balance"] = float(m.group(5))
            elif fmt == "close_mid":
                entry["net_pnl"] = float(m.group(3))
                entry["commission"] = float(m.group(4))
            else:  # close_old
                entry["net_pnl"] = float(m.group(3))

            trades.append(entry)
            break  # first match wins

    return trades


def get_okx_state():
    """读取OKX实盘状态"""
    if OKX_STATE_FILE.exists():
        try:
            with open(OKX_STATE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {"total_trades": 0, "winning_trades": 0, "consecutive_losses": 0, "leverage": 3}


def get_okx_orders():
    """读取OKX订单历史"""
    if OKX_ORDERS_FILE.exists():
        try:
            with open(OKX_ORDERS_FILE) as f:
                return json.load(f)
        except:
            pass
    return []


def get_all_data():
    """聚合所有数据"""
    state = get_state()
    prices = get_recent_prices(60)
    trades = get_trade_history()

    # 检查是否为实盘模式
    is_okx_live = OKX_STATE_FILE.exists()

    if is_okx_live:
        # 实盘模式
        okx_state = get_okx_state()
        okx_orders = get_okx_orders()
        pos = state.get("position")
        cash = state.get("cash", 0)
        current_snap = prices[-1] if prices else {"price": 0, "change_pct": 0, "high_24h": 0, "low_24h": 0, "indicators": {}}
        current_price = current_snap["price"]
        change_pct = current_snap.get("change_pct", 0)
        high_24h = current_snap.get("high_24h", 0)
        low_24h = current_snap.get("low_24h", 0)
        indicators = current_snap.get("indicators", {})
        position_info = None
        total_equity = cash
        if pos and pos.get("size", 0) > 0:
            entry = pos["entry_price"]
            size = pos["size"]
            side = pos["side"]
            lev = pos.get("leverage", 1)
            margin = size * entry / lev
            if current_price > 0:
                pnl = (current_price - entry) * size if side == "long" else (entry - current_price) * size
                pnl_pct = (current_price - entry) / entry * 100 * lev if side == "long" else (entry - current_price) / entry * 100 * lev
                total_equity = cash + margin + pnl
            else:
                pnl = 0; pnl_pct = 0; total_equity = cash + margin
            position_info = {"side": side, "size": round(size, 6), "entry_price": round(entry, 2),
                        "current_price": round(current_price, 2), "leverage": lev, "margin": round(margin, 2),
                        "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2), "equity": round(total_equity, 2)}
        total_return = (total_equity - 10000) / 10000 * 100 if total_equity > 0 else 0
        return {
            "mode": "LIVE", "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "initial_capital": 10000, "total_return_pct": round(total_return, 2),
            "cash": round(cash, 2), "equity": round(total_equity, 2),
            "current_price": round(current_price, 2), "change_pct": round(change_pct, 2),
            "high_24h": round(high_24h, 2), "low_24h": round(low_24h, 2), "indicators": indicators,
            "position": position_info,
            "stats": {"total_trades": okx_state.get("total_trades", 0), "winning_trades": okx_state.get("winning_trades", 0),
                       "win_rate": round(okx_state.get("winning_trades", 0) / max(okx_state.get("total_trades", 1), 1) * 100, 1),
                       "consecutive_losses": okx_state.get("consecutive_losses", 0)},
            "price_history": [{"time": p["time"], "price": p["price"]} for p in prices[-40:]],
            "trades": okx_orders[-20:],
        }
    else:
        # 模拟盘模式
        pos = state.get("position")
        cash = state["cash"]

        # Stats: merge state + log
        closed_trades = [t for t in trades if t["action"] in ("平多", "平空")]
        won = [t for t in closed_trades if t.get("net_pnl", 0) > 0]
        total_trades = max(state.get("total_trades", 0), len(closed_trades))
        winning_trades = max(state.get("winning_trades", 0), len(won))
        win_rate = winning_trades / max(total_trades, 1) * 100

        current_snap = prices[-1] if prices else {"price": 0, "change_pct": 0, "high_24h": 0, "low_24h": 0, "indicators": {}}
        current_price = current_snap["price"]
        change_pct = current_snap.get("change_pct", 0)
        high_24h = current_snap.get("high_24h", 0)
        low_24h = current_snap.get("low_24h", 0)
        indicators = current_snap.get("indicators", {})

        position_info = None
        total_equity = cash
        if pos and pos.get("size", 0) > 0:
            entry = pos["entry_price"]
            size = pos["size"]
            side = pos["side"]
            lev = pos.get("leverage", 1)
            margin = size * entry / lev
            if current_price > 0:
                pnl = (current_price - entry) * size if side == "long" else (entry - current_price) * size
                pnl_pct = (current_price - entry) / entry * 100 * lev if side == "long" else (entry - current_price) / entry * 100 * lev
                total_equity = cash + margin + pnl
            else:
                pnl = 0; pnl_pct = 0; total_equity = cash + margin
            position_info = {"side": side, "size": round(size, 6), "entry_price": round(entry, 2),
                        "current_price": round(current_price, 2), "leverage": lev, "margin": round(margin, 2),
                        "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2), "equity": round(total_equity, 2)}

        total_return = (total_equity - 1000) / 1000 * 100

        return {
            "mode": "SIMULATION", "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "initial_capital": 1000, "total_return_pct": round(total_return, 2),
            "cash": round(cash, 2), "equity": round(total_equity, 2),
            "current_price": round(current_price, 2), "change_pct": round(change_pct, 2),
            "high_24h": round(high_24h, 2), "low_24h": round(low_24h, 2), "indicators": indicators,
            "position": position_info,
            "stats": {"total_trades": total_trades, "winning_trades": winning_trades, "win_rate": round(win_rate, 1)},
            "price_history": [{"time": p["time"], "price": p["price"]} for p in prices[-40:]],
            "trades": trades[-30:],
        }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "json":
        print(json.dumps(get_all_data(), ensure_ascii=False, indent=2))
    else:
        data = get_all_data()
        print(f"更新时间: {data['updated_at']}")
        print(f"价格: ${data['current_price']:,.0f} ({data['change_pct']:+.2f}%)")
        if data["position"]:
            p = data["position"]
            print(f"持仓: {p['side'].upper()} {p['size']} @ ${p['entry_price']:,.0f}")
        else:
            print("无持仓")
        print(f"权益: ${data['equity']:,.2f} ({data['total_return_pct']:+.2f}%)")