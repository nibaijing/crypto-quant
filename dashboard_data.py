#!/usr/bin/env python3
"""Dashboard 数据 API — 直接从引擎核心数据结构读取，不再解析日志。

数据源:
  1. live_futures_state.json — executor 每5秒写入精确账户数据 (cash, position, trades)
  2. ws_price_snapshot.json — WS 模块每秒写入当前价格 (price, indicators, kline)

设计原则:
  - equity 永远来自 executor 写入的完整数据，不做外部计算
  - 价格0或异常时保持最近一次有效 equity 不变
  - 所有数据一条路径，不混用多种数据源相互覆盖
"""

import json
import time
import os
from pathlib import Path
from datetime import datetime
from collections import deque

PROJECT_ROOT = Path(__file__).parent
STATE_FILE = PROJECT_ROOT / "data" / "live_futures_state.json"
PRICE_SNAPSHOT = PROJECT_ROOT / "data" / "ws_price_snapshot.json"

# ===== 缓存 =====
_price_cache = deque(maxlen=200)
_price_cache_mtime = 0
_last_snapshot_key = None

# equity 粘滞缓存：价格0时不降级
_last_valid_equity = None
_last_valid_position = None

_MA_MAP = {"ma_7": "ma7", "ma_25": "ma25", "ma_99": "ma99"}


# ===== 价格快照读取 =====

def _read_price_snapshot():
    """读取最新价格快照，维护价格历史。"""
    global _price_cache_mtime, _last_snapshot_key

    if not PRICE_SNAPSHOT.exists():
        return None

    try:
        cur_mtime = PRICE_SNAPSHOT.stat().st_mtime
        if cur_mtime == _price_cache_mtime and _price_cache_mtime > 0:
            return _price_cache[-1] if _price_cache else None

        with open(PRICE_SNAPSHOT) as f:
            snap = json.load(f)
        _price_cache_mtime = cur_mtime
    except Exception:
        return None

    key = (snap.get("time", ""), int(snap.get("price", 0)))
    if key != _last_snapshot_key:
        _last_snapshot_key = key
        _price_cache.append({
            "time": snap.get("time", ""),
            "price": snap.get("price", 0),
            "change_pct": snap.get("change_pct", 0),
            "high_24h": snap.get("high_24h", 0),
            "low_24h": snap.get("low_24h", 0),
            "indicators": snap.get("indicators", {}),
            "kline": snap.get("kline", {}),
        })
    return snap


def get_recent_prices(limit=60):
    _read_price_snapshot()
    return list(_price_cache)[-limit:]


# ===== 状态读取 =====

def _read_state_file():
    """读取 executor 写入的最新状态，单一路径。"""
    if not STATE_FILE.exists():
        return {"cash": 1000.0, "total_trades": 0, "winning_trades": 0, "position": None}

    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"cash": 1000.0, "total_trades": 0, "winning_trades": 0, "position": None}


# ===== 核心 equity 计算 =====

def _calc_equity_core(state_raw, current_price):
    """从状态文件+当前价格计算准确 equity。

    核心逻辑：
      - 无持仓 → equity = cash
      - 有持仓 + 有效价格 → equity = cash + margin + unrealized_pnl
      - 有持仓 + 无效价格(0) → 返回 None，调用方用粘滞值
    """
    global _last_valid_equity, _last_valid_position

    cash = float(state_raw.get("cash", 1000.0))
    pos_raw = state_raw.get("position")

    if not pos_raw or float(pos_raw.get("size", 0)) <= 0:
        # 无持仓：直接用 cash
        _last_valid_equity = cash
        _last_valid_position = None
        return cash, None

    entry = float(pos_raw["entry_price"])
    size = float(pos_raw["size"])
    side = str(pos_raw.get("side", "long"))
    lev = float(pos_raw.get("leverage", 10))

    if current_price <= 0:
        # 价格无效：如果之前有有效值则保持，否则 fallback 到 cash
        if _last_valid_equity is not None:
            return _last_valid_equity, _last_valid_position
        return cash, None

    if side == "long":
        pnl = (current_price - entry) * size
        pnl_pct = (current_price - entry) / entry * 100 * lev
    else:
        pnl = (entry - current_price) * size
        pnl_pct = (entry - current_price) / entry * 100 * lev

    margin = size * entry / lev
    equity = cash + margin + pnl

    pos_info = {
        "side": side,
        "size": round(size, 6),
        "entry_price": round(entry, 2),
        "current_price": round(current_price, 2),
        "leverage": lev,
        "margin": round(margin, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
    }

    # 更新粘滞缓存
    _last_valid_equity = round(equity, 2)
    _last_valid_position = pos_info

    return round(equity, 2), pos_info


# ===== 指标标准化 =====

def _normalize_indicators(indicators):
    return {_MA_MAP.get(k, k): v for k, v in indicators.items()}


# ===== 聚合 API =====

def get_all_data():
    """聚合所有数据 — 单次调用返回完整 Dashboard 数据。"""
    state_raw = _read_state_file()
    snap = _read_price_snapshot()
    prices = get_recent_prices(60)

    current_snap = prices[-1] if prices else {
        "price": 0, "change_pct": 0, "high_24h": 0, "low_24h": 0,
        "indicators": {}, "kline": {},
    }
    current_price = current_snap.get("price", 0)

    # 模式: 判断是否有 OKX 文件 (实盘模式)
    okx_exists = (PROJECT_ROOT / "data" / "okx_live_state.json").exists()
    initial_capital = 10000 if okx_exists else 1000.0

    cash = float(state_raw.get("cash", initial_capital))
    total_trades = int(state_raw.get("total_trades", 0))
    winning_trades = int(state_raw.get("winning_trades", 0))

    # ---- equity + position ----
    equity, pos_info = _calc_equity_core(state_raw, current_price)

    initial_cap_used = initial_capital if initial_capital > 0 else 1000.0
    total_return_pct = ((equity - initial_cap_used) / initial_cap_used * 100) if equity > 0 else 0.0

    result = {
        "mode": "LIVE" if okx_exists else "SIMULATION",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "initial_capital": round(initial_cap_used, 2),
        "total_return_pct": round(total_return_pct, 2),
        "cash": round(cash, 2),
        "equity": equity,
        "current_price": round(current_price, 2),
        "change_pct": round(current_snap.get("change_pct", 0), 2),
        "high_24h": round(current_snap.get("high_24h", 0), 2),
        "low_24h": round(current_snap.get("low_24h", 0), 2),
        "indicators": _normalize_indicators(current_snap.get("indicators", {})),
        "position": pos_info,
        "price_history": [
            {"time": p["time"], "price": p["price"]}
            for p in prices[-40:]
        ],
        "kline": current_snap.get("kline", {}),
        "stats": {
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "win_rate": round(winning_trades / max(total_trades, 1) * 100, 1),
        },
    }

    # 信号 & 交易历史: 仍从日志解析（保持向后兼容）
    result["signals"] = _parse_signals_from_log()
    result["trades"] = _parse_trades_from_log()

    return result


# ===== 日志信号/交易解析（简化版）=====

_LOG_FILE = PROJECT_ROOT / "data" / "live_trading.log"


def _parse_signals_from_log():
    """从日志解析信号行 — 按今日日期过滤，覆盖当天所有信号。"""
    import re
    from datetime import datetime

    pat_main = re.compile(r'📊\s*\$?\s*([\d,]+).*Eq=\$?([\d,]+)')
    pat_sig = re.compile(r'SIG=(\w+)')
    pat_k = re.compile(r'K#(\d+)')
    pat_time = re.compile(r'(\d{2}:\d{2}:\d{2})')

    if not _LOG_FILE.exists():
        return []

    try:
        text = _LOG_FILE.read_text()
        lines = text.strip().split("\n")
        today = datetime.now().strftime("%Y-%m-%d")
        lines = [l for l in lines if l.startswith(today)]
    except Exception:
        return []

    signals = []
    for line in lines:
        m = pat_main.search(line)
        if not m:
            continue
        t = pat_time.search(line)
        sig_m = pat_sig.search(line)
        k_m = pat_k.search(line)
        try:
            price = int(float(m.group(1).replace(",", "")))
            equity = int(float(m.group(2).replace(",", "")))
        except ValueError:
            continue
        signals.append({
            "time": t.group(1) if t else "",
            "price": price,
            "equity": equity,
            "signal": sig_m.group(1) if sig_m else "?",
            "kline": int(k_m.group(1)) if k_m else 0,
        })
    return signals


def _parse_trades_from_log():
    """从日志解析交易行 — 按今日日期过滤。"""
    import re
    from datetime import datetime

    patterns = [
        re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约做多.*?@ ([\d.]+).*?\|\s*([\d.]+)x'),
        re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约做空.*?@ ([\d.]+).*?\|\s*([\d.]+)x'),
        re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约平[仓多].*?@ ([\d.]+).*?(?:PnL=\$?|净利=\$?)([+-]?[\d.]+)'),
        re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约平空.*?@ ([\d.]+).*?(?:PnL=\$?|净利=\$?)([+-]?[\d.]+)'),
    ]
    actions = ["做多", "做空", "平多", "平空"]

    if not _LOG_FILE.exists():
        return []

    try:
        text = _LOG_FILE.read_text()
        lines = text.strip().split("\n")
        today = datetime.now().strftime("%Y-%m-%d")
        lines = [l for l in lines if l.startswith(today)]
    except Exception:
        return []

    trades = []
    for line in lines:
        for pat, action in zip(patterns, actions):
            m = pat.search(line)
            if m:
                trades.append({
                    "time": m.group(1),
                    "action": action,
                    "price": float(m.group(2)),
                    "pnl": float(m.group(3)) if action in ("平多", "平空") else 0,
                    "leverage": float(m.group(3)) if action in ("做多", "做空") else 0,
                })
                break
    return trades


if __name__ == "__main__":
    import sys
    data = get_all_data()
    if len(sys.argv) > 1 and sys.argv[1] == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"Time: {data['updated_at']}")
        print(f"Price: ${data['current_price']:,.0f} ({data['change_pct']:+.2f}%)")
        if data["position"]:
            p = data["position"]
            print(f"Position: {p['side'].upper()} {p['size']} @ ${p['entry_price']:,.0f} | PnL: ${p['pnl']:+.2f} ({p['pnl_pct']:+.2f}%)")
        else:
            print("Position: FLAT")
        print(f"Cash: ${data['cash']:,.2f} | Equity: ${data['equity']:,.2f} | Return: {data['total_return_pct']:+.2f}%")
        print(f"Trades: {data['stats']['total_trades']} | Win: {data['stats']['win_rate']}%")