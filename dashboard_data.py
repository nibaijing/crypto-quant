#!/usr/bin/env python3
"""Dashboard 数据 API — 增量日志解析 + 缓存加速

数据源:
  1. live_futures_state.json — 模拟盘executor 每5秒写入
  2. ws_price_snapshot.json — WS 模块每秒写入当前价格
  3. live_trading.log — 交易历史 + 信号推断
"""

import json
import re
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

# ===== 价格缓存 (去重, 最多200点) =====
_price_cache = deque(maxlen=200)
_last_price_ts = None  # (time_str, price_str) dedup
_price_cache_mtime = 0

# ===== 状态文件缓存 =====
_state_cache = {"data": None, "mtime": 0, "last_check": 0}
_STATE_CACHE_TTL = 0.5

# ===== 日志增量缓存 =====
_log_cache = {"trades": [], "signals": [], "pos": 0, "mtime": 0, "size": 0}

# ---- 编译正则 (交易历史) ----
_long_pat = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约做多.*?@ ([\d.]+).*?\|\s*([\d.]+)x')
_short_pat = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约做空.*?@ ([\d.]+).*?\|\s*([\d.]+)x')
_sell_new = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约平[仓多].*?@ ([\d.]+).*?手续费=\$?([\d.]+).*?净利=\$?([+-]?[\d.]+).*?余额=\$?([\d.]+)')
_cover_new = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约平空.*?@ ([\d.]+).*?手续费=\$?([\d.]+).*?净利=\$?([+-]?[\d.]+).*?余额=\$?([\d.]+)')
_sell_mid = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约平[仓多].*?@ ([\d.]+).*?PnL=\$?([+-]?[\d.]+).*?手续费=\$?([\d.]+)')
_cover_mid = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约平空.*?@ ([\d.]+).*?PnL=\$?([+-]?[\d.]+).*?手续费=\$?([\d.]+)')
_sell_old = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约平[仓多].*?@ ([\d.]+).*?(?:PnL=\$?|盈亏: )([+-]?[\d.]+)')
_cover_old = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约平空.*?@ ([\d.]+).*?(?:PnL=\$?|盈亏: )([+-]?[\d.]+)')

_trade_rules = [
    (_long_pat,   "做多", "open"),
    (_short_pat,  "做空", "open"),
    (_sell_new,   "平多", "close_new"),
    (_cover_new,  "平空", "close_new"),
    (_sell_mid,   "平多", "close_mid"),
    (_cover_mid,  "平空", "close_mid"),
    (_sell_old,   "平多", "close_old"),
    (_cover_old,  "平空", "close_old"),
]

# ---- 编译正则 (信号行) ----
_sig_main = re.compile(
    r'(\d{2}:\d{2}:\d{2}).*📊\s*\$\s*([\d,]+).*Eq=\$?([\d,]+)')
_sig_signal = re.compile(r'SIG=(\w+)')
_sig_kline = re.compile(r'K#(\d+)')


# ===== 共享工具函数 =====

def _parse_trade_entry(m, action, fmt):
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
    return entry


def _parse_signal_from_line(line):
    m = _sig_main.search(line)
    if not m:
        return None
    sig_m = _sig_signal.search(line)
    k_m = _sig_kline.search(line)
    return {
        "time": m.group(1),
        "price": int(float(m.group(2).replace(",", ""))),
        "equity": int(float(m.group(3).replace(",", ""))),
        "signal": sig_m.group(1) if sig_m else "?",
        "kline": int(k_m.group(1)) if k_m else 0,
    }


def _update_log_cache():
    """增量读取日志尾部, 追加新交易/信号到内存缓存"""
    if not LOG_FILE.exists():
        return

    try:
        stat = LOG_FILE.stat()
    except OSError:
        return

    size = stat.st_size
    mtime = stat.st_mtime
    cache = _log_cache

    # 文件被截断 -> 重置
    if size < cache["pos"]:
        cache["trades"] = []
        cache["signals"] = []
        cache["pos"] = 0
        cache["mtime"] = 0
        cache["size"] = 0

    # 文件未变化
    if size == cache["size"] and mtime == cache["mtime"]:
        return

    try:
        with open(LOG_FILE) as f:
            f.seek(cache["pos"])
            lines = f.readlines()
            cache["pos"] = f.tell()
    except Exception:
        return

    cache["size"] = size
    cache["mtime"] = mtime

    for line in lines:
        for pat, action, fmt in _trade_rules:
            m = pat.search(line)
            if m:
                cache["trades"].append(_parse_trade_entry(m, action, fmt))
                break
        sig = _parse_signal_from_line(line)
        if sig:
            cache["signals"].append(sig)

    # 内存上限
    if len(cache["trades"]) > 500:
        cache["trades"] = cache["trades"][-500:]
    if len(cache["signals"]) > 500:
        cache["signals"] = cache["signals"][-500:]


def _read_snapshot():
    """读取 WS 价格快照 (带 mtime 缓存)"""
    global _last_price_ts, _price_cache_mtime
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

    price = snap.get("price", 0)
    ts = snap.get("time", "")
    pair = (ts, int(price))

    if pair != _last_price_ts:
        _last_price_ts = pair
        _price_cache.append({
            "time": ts, "price": price,
            "change_pct": snap.get("change_pct", 0),
            "high_24h": snap.get("high_24h", 0),
            "low_24h": snap.get("low_24h", 0),
            "indicators": snap.get("indicators", {}),
            "kline": snap.get("kline", {}),
        })
    return snap


def get_state():
    """读取当前持仓和账户 (500ms 缓存)"""
    now = time.time()
    cache = _state_cache
    if now - cache["last_check"] < _STATE_CACHE_TTL and cache["data"] is not None:
        return cache["data"]

    data = {"cash": 1000, "total_trades": 0, "winning_trades": 0, "position": None}
    if STATE_FILE.exists():
        try:
            mtime = STATE_FILE.stat().st_mtime
            if mtime == cache["mtime"] and cache["data"] is not None:
                cache["last_check"] = now
                return cache["data"]
            with open(STATE_FILE) as f:
                data = json.load(f)
            cache["mtime"] = mtime
        except Exception:
            pass

    cache["data"] = data
    cache["last_check"] = now
    return data


def get_recent_prices(limit=60):
    """从内存缓存获取最近价格点"""
    _read_snapshot()
    return list(_price_cache)[-limit:]


def get_trade_history():
    """获取交易历史 (从增量缓读取)"""
    _update_log_cache()
    return _log_cache["trades"]


# ===== 共享计算 =====

_MA_MAP = {"ma_7": "ma7", "ma_25": "ma25", "ma_99": "ma99"}


def _normalize_indicators(indicators):
    return {_MA_MAP.get(k, k): v for k, v in indicators.items()}


def _compute_position(pos, current_price, cash):
    """计算持仓信息, 返回 (position_info_dict_or_None, total_equity)"""
    if not pos or pos.get("size", 0) <= 0:
        return None, cash
    entry = pos["entry_price"]
    size = pos["size"]
    side = pos["side"]
    lev = pos.get("leverage", 1)
    margin = size * entry / lev
    if current_price > 0:
        if side == "long":
            pnl = (current_price - entry) * size
            pnl_pct = (current_price - entry) / entry * 100 * lev
        else:
            pnl = (entry - current_price) * size
            pnl_pct = (entry - current_price) / entry * 100 * lev
        total_equity = cash + margin + pnl
    else:
        pnl = 0
        pnl_pct = 0
        total_equity = cash + margin
    return {
        "side": side, "size": round(size, 6), "entry_price": round(entry, 2),
        "current_price": round(current_price, 2), "leverage": lev,
        "margin": round(margin, 2), "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
    }, total_equity


def get_okx_state():
    if OKX_STATE_FILE.exists():
        try:
            with open(OKX_STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"total_trades": 0, "winning_trades": 0, "consecutive_losses": 0, "leverage": 3}


def get_okx_orders():
    if OKX_ORDERS_FILE.exists():
        try:
            with open(OKX_ORDERS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def get_all_data():
    """聚合所有数据 (单次调用, 包含所有字段)"""
    _update_log_cache()
    state = get_state()
    prices = get_recent_prices(60)
    trades = _log_cache["trades"]
    signals = _log_cache["signals"]

    is_okx_live = OKX_STATE_FILE.exists()
    initial_capital = 10000 if is_okx_live else 1000
    cash = state.get("cash", initial_capital)

    current_snap = prices[-1] if prices else {
        "price": 0, "change_pct": 0, "high_24h": 0, "low_24h": 0,
        "indicators": {}, "kline": {},
    }
    current_price = current_snap["price"]

    position_info, total_equity = _compute_position(
        state.get("position"), current_price, cash
    )
    total_return = ((total_equity - initial_capital) / initial_capital * 100
                    if total_equity > 0 else 0)

    result = {
        "mode": "LIVE" if is_okx_live else "SIMULATION",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "initial_capital": initial_capital,
        "total_return_pct": round(total_return, 2),
        "cash": round(cash, 2),
        "equity": round(total_equity, 2),
        "current_price": round(current_price, 2),
        "change_pct": round(current_snap.get("change_pct", 0), 2),
        "high_24h": round(current_snap.get("high_24h", 0), 2),
        "low_24h": round(current_snap.get("low_24h", 0), 2),
        "indicators": _normalize_indicators(current_snap.get("indicators", {})),
        "position": position_info,
        "price_history": [{"time": p["time"], "price": p["price"]}
                          for p in prices[-40:]],
        "signals": signals[-50:],
    }

    if is_okx_live:
        okx_state = get_okx_state()
        okx_orders = get_okx_orders()
        result["stats"] = {
            "total_trades": okx_state.get("total_trades", 0),
            "winning_trades": okx_state.get("winning_trades", 0),
            "win_rate": round(
                okx_state.get("winning_trades", 0) /
                max(okx_state.get("total_trades", 1), 1) * 100, 1),
            "consecutive_losses": okx_state.get("consecutive_losses", 0),
        }
        result["trades"] = okx_orders[-20:]
    else:
        closed_trades = [t for t in trades if t["action"] in ("平多", "平空")]
        won = [t for t in closed_trades if t.get("net_pnl", 0) > 0]
        total_cnt = max(state.get("total_trades", 0), len(closed_trades))
        won_cnt = max(state.get("winning_trades", 0), len(won))
        result["stats"] = {
            "total_trades": total_cnt,
            "winning_trades": won_cnt,
            "win_rate": round(won_cnt / max(total_cnt, 1) * 100, 1),
        }
        result["trades"] = trades[-30:]
        result["kline"] = current_snap.get("kline", {})

    return result


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
