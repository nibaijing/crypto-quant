#!/usr/bin/env python3
"""Dashboard 数据 API — 完全重写，从可靠数据源读取。

数据源（按优先级）:
  1. ws_price_snapshot.json — 引擎每5秒写入，始终有最新价格+指标+K线
  2. live_trading.log — 所有信号/交易/心跳记录
  3. live_futures_state.json — 辅助，仅当有持仓时可靠

核心设计：
  - 无持仓时 equity 从心跳日志取（每5分钟写一次 Eq=$XXX）
  - 有持仓时从 state.json + 当前价格计算
  - 信号/交易从日志正则解析（已适配最新日志格式）
"""

import json
import re
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Any, Dict, List

PROJECT_ROOT = Path(__file__).parent
STATE_FILE = PROJECT_ROOT / "data" / "live_futures_state.json"
PRICE_SNAPSHOT = PROJECT_ROOT / "data" / "ws_price_snapshot.json"
LOG_FILE = PROJECT_ROOT / "data" / "live_trading.log"

# ===== 缓存 =====
_price_cache: List[dict] = []
_price_cache_mtime = 0
_last_snapshot_key: Any = None
_last_valid_equity: Optional[float] = None
_last_valid_position: Optional[dict] = None
_last_equity_from_log: Optional[float] = None  # 从心跳日志提取的最近权益


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
        # 限制缓存大小
        if len(_price_cache) > 200:
            _price_cache[:] = _price_cache[-200:]
    return snap


def get_recent_prices(limit=60):
    _read_price_snapshot()
    if _price_cache:
        return list(_price_cache)[-limit:]
    return []


# ===== 从日志提取最新权益 =====

def _extract_equity_from_log() -> Optional[float]:
    """从最近的心跳/状态日志中提取 equity 值。
    
    匹配格式:
      - 📊 $76,955 | Eq=$571 | SIG=HOLD | K#1
      - 💓 心跳 | 运行: 0d 01:00:04 | 权益: $571 | 交易: 0次 | K线: 4
    """
    global _last_equity_from_log

    if not LOG_FILE.exists():
        return _last_equity_from_log

    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
        lines = text.strip().split("\n")
        today = date.today().strftime("%Y-%m-%d")
        today_lines = [l for l in lines if l.startswith(today)]
    except Exception:
        return _last_equity_from_log

    if not today_lines:
        return _last_equity_from_log

    # 从后往前找最新的
    for line in reversed(today_lines):
        # 格式1: 📊 ... | Eq=$571 | ...
        m = re.search(r'Eq=\$?([\d,]+)', line)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                _last_equity_from_log = val
                return val
            except ValueError:
                pass

        # 格式2: 💓 心跳 ... 权益: $571 ...
        m = re.search(r'权益:\s*\$?([\d,]+)', line)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                _last_equity_from_log = val
                return val
            except ValueError:
                pass

    return _last_equity_from_log


# ===== 状态文件读取 =====

def _read_state_file() -> dict:
    """读取 executor 写入的最新状态。文件可能陈旧（无持仓时不更新）。"""
    if not STATE_FILE.exists():
        return {"cash": 571.0, "total_trades": 0, "winning_trades": 0, "position": None}

    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"cash": 571.0, "total_trades": 0, "winning_trades": 0, "position": None}


# ===== 核心 equity 计算（增强版，支持日志回退）=====

def _calc_equity_core(state_raw, current_price):
    """从状态文件+当前价格计算准确 equity。
    
    增强：
      - 无持仓时从日志提取 equity（不再依赖 state.json 更新）
      - 有持仓+有效价格时精确计算
      - 价格无效时回退到最近有效值
    """
    global _last_valid_equity, _last_valid_position

    cash = float(state_raw.get("cash", 1000.0))
    pos_raw = state_raw.get("position")

    if not pos_raw or float(pos_raw.get("size", 0)) <= 0:
        # 无持仓：尝试从日志取最新 equity，优于 stale cash
        log_equity = _extract_equity_from_log()
        if log_equity is not None and log_equity > 0:
            _last_valid_equity = log_equity
            _last_valid_position = None
            return log_equity, None
        # 回退到 state.json 的 cash
        _last_valid_equity = cash
        _last_valid_position = None
        return cash, None

    entry = float(pos_raw["entry_price"])
    size = float(pos_raw["size"])
    side = str(pos_raw.get("side", "long"))
    lev = float(pos_raw.get("leverage", 10))

    if current_price <= 0:
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

    _last_valid_equity = round(equity, 2)
    _last_valid_position = pos_info

    return round(equity, 2), pos_info


# ===== 指标标准化 =====

_MA_MAP = {"ma_7": "ma7", "ma_25": "ma25", "ma_99": "ma99"}


def _normalize_indicators(indicators):
    return {_MA_MAP.get(k, k): v for k, v in indicators.items()}


# ===== 从日志解析信号（精确匹配最新格式）=====

def _dedup_signals(signals: list) -> list:
    """去除重复信号行（相同time+price+signal的只保留第一个）。"""
    seen = set()
    result = []
    for s in signals:
        key = (s["time"], s["price"], s["signal"], s["kline"])
        if key not in seen:
            seen.add(key)
            result.append(s)
    return result


def _parse_signals_from_log() -> list:
    """从日志解析信号行 — 适配 v7.x 最新格式。
    
    最新日志格式:
      📊 $76,955 | Eq=$571 | Pos=-1.66% | SIG=HOLD | K#4
      📏 Kline(signal) | RSI=22 ADX=77 MACDh=-16.4✗ | regime=... | SHORT=6/8✓ LONG=7/9
    
    Returns:
        [{time, price, equity, signal, kline, long_score, short_score, rsi, adx}, ...]
    """
    if not LOG_FILE.exists():
        return []

    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
        lines = text.strip().split("\n")
        today = date.today().strftime("%Y-%m-%d")
        lines = [l for l in lines if l.startswith(today)]
    except Exception:
        return []

    if not lines:
        return []

    signals = []
    # 解析 📊 行 — 主信号行
    pat_main = re.compile(
        r'📊\s*\$?([\d,]+).*?Eq=\$?([\d,]+).*?SIG=(\w+).*?K#(\d+)'
    )
    # 解析 📏 Kline(signal) 行 — 评分详情
    pat_kline = re.compile(
        r'📏\s+Kline\(signal\).*?RSI=([\d.]+)\s+ADX=([\d.]+).*?SHORT=([\d.]+)/\d+.*?LONG=([\d.]+)/\d+'
    )
    # 解析时间
    pat_time = re.compile(r'(\d{2}:\d{2}:\d{2})')

    i = 0
    while i < len(lines):
        line = lines[i]
        m = pat_main.search(line)
        if not m:
            i += 1
            continue

        t = pat_time.search(line)
        m_time = t.group(1) if t else ""

        try:
            price = int(float(m.group(1).replace(",", "")))
            equity = int(float(m.group(2).replace(",", "")))
        except ValueError:
            i += 1
            continue

        sig = {
            "time": m_time,
            "price": price,
            "equity": equity,
            "signal": m.group(3),
            "kline": int(m.group(4)),
            "long_score": None,
            "short_score": None,
            "rsi": None,
            "adx": None,
            "is_entry": None,  # 是否有入场信号
        }

        # 向后搜索最多5行找 Kline(signal) 评分详情
        for back in range(1, min(i, 5) + 1):
            prev_line = lines[i - back]
            d = pat_kline.search(prev_line)
            if d:
                try:
                    sig["rsi"] = float(d.group(1))
                    sig["adx"] = float(d.group(2))
                    sig["short_score"] = float(d.group(3))
                    sig["long_score"] = float(d.group(4))
                except ValueError:
                    pass
                break

            # 也检查是否有 ✅ LIMIT FILLED (入场)
            if "LIMIT FILLED" in prev_line:
                sig["is_entry"] = True

        signals.append(sig)
        i += 1

    return _dedup_signals(signals)


# ===== 从日志解析交易 =====

def _parse_trades_from_log() -> list:
    """从日志解析交易行 — 适配 v7.x 限价单格式。
    
    最新格式 (v7.x):
      ✅ LIMIT FILLED: SHORT_FILLED @ $76,578
      ✅ LIMIT FILLED: LONG_FILLED @ $76,500
      ⚡ Tick 止损: COVER PnL=-200bp
      ⚡ Tick 止损: SELL PnL=-180bp
      ⚡ 追踪止盈: COVER @76500 PnL=+50bp
    
    v6 旧格式 (兼容):
      ✅ 合约做空: 0.0014 BTC-USDT @ 74698.00 | 10x
      ✅ 合约平空: 0.0014 @ 74350.00 | PnL=+$0.49 | 净利=+$0.49
    """
    if not LOG_FILE.exists():
        return []

    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
        lines = text.strip().split("\n")
        today = date.today().strftime("%Y-%m-%d")
        lines = [l for l in lines if l.startswith(today)]
    except Exception:
        return []

    trades = []

    for line in lines:
        # === v7.x 限价单格式 ===
        # LIMIT FILLED: SHORT_FILLED
        m_filled = re.search(r'✅\s*LIMIT\s+FILLED:\s*(\w+_FILLED)\s*@\s*\$?([\d,.]+)', line)
        if m_filled:
            action_raw = m_filled.group(1)
            price = float(m_filled.group(2).replace(",", ""))
            action_map = {
                "SHORT_FILLED": "做空",
                "LONG_FILLED": "做多",
                "SELL_FILLED": "平多",
                "COVER_FILLED": "平空",
            }
            action = action_map.get(action_raw, action_raw)
            trades.append({
                "time": line[:19],
                "action": action,
                "price": price,
                "size": 0,
                "pnl": 0.0,
            })
            continue

        # Tick 止损/止盈 (exit events)
        m_sl = re.search(r'⚡\s*(?:Tick\s+)?(止损|追踪止损(?:失效)?|追踪止盈|ATR止盈|Tick\s+RSI)\S*\s*[→:]\s*(SELL|COVER)\s*@?\$?([\d.,]+).*?PnL=([+-]?[\d.]+)bp', line)
        if m_sl:
            exit_action = m_sl.group(2)
            price = float(m_sl.group(3).replace(",", ""))
            pnl_bp = float(m_sl.group(4))
            exit_map = {"SELL": "平多", "COVER": "平空"}
            trades.append({
                "time": line[:19],
                "action": exit_map.get(exit_action, exit_action),
                "price": price,
                "size": 0,
                "pnl": round(pnl_bp / 100 * 0.571, 2),  # 估算USD PnL
                "pnl_bp": pnl_bp,
            })
            continue

        # === v6 旧格式兼容 ===
        m_open_long = re.search(r'✅\s*合约做多:\s*([\d.]+)\s+\S+\s+@\s*([\d.]+)\s*\|\s*([\d.]+)x', line)
        if m_open_long:
            trades.append({
                "time": line[:19],
                "action": "做多",
                "price": float(m_open_long.group(2)),
                "size": float(m_open_long.group(1)),
                "leverage": float(m_open_long.group(3)),
                "pnl": 0.0,
            })
            continue

        m_open_short = re.search(r'✅\s*合约做空:\s*([\d.]+)\s+\S+\s+@\s*([\d.]+)\s*\|\s*([\d.]+)x', line)
        if m_open_short:
            trades.append({
                "time": line[:19],
                "action": "做空",
                "price": float(m_open_short.group(2)),
                "size": float(m_open_short.group(1)),
                "leverage": float(m_open_short.group(3)),
                "pnl": 0.0,
            })
            continue

        m_close_long = re.search(r'✅\s*合约平多:.*?@\s*([\d.]+).*?净利=\$?([+-]?[\d.]+)', line)
        if m_close_long:
            trades.append({
                "time": line[:19],
                "action": "平多",
                "price": float(m_close_long.group(1)),
                "pnl": float(m_close_long.group(2)),
            })
            continue

        m_close_short = re.search(r'✅\s*合约平空:.*?@\s*([\d.]+).*?净利=\$?([+-]?[\d.]+)', line)
        if m_close_short:
            trades.append({
                "time": line[:19],
                "action": "平空",
                "price": float(m_close_short.group(1)),
                "pnl": float(m_close_short.group(2)),
            })
            continue

    return trades


# ===== 聚合 API =====

def get_all_data() -> dict:
    """聚合所有数据 — 单次调用返回完整 Dashboard 数据。"""
    state_raw = _read_state_file()
    snap = _read_price_snapshot()
    prices = get_recent_prices(60)

    current_snap = prices[-1] if prices else {
        "price": 0, "change_pct": 0, "high_24h": 0, "low_24h": 0,
        "indicators": {}, "kline": {},
    }
    current_price = current_snap.get("price", 0)
    initial_capital = 571.0  # 实际入金

    cash = float(state_raw.get("cash", initial_capital))
    total_trades = int(state_raw.get("total_trades", 0))
    winning_trades = int(state_raw.get("winning_trades", 0))

    # ---- equity + position ----
    equity, pos_info = _calc_equity_core(state_raw, current_price)
    total_return_pct = ((equity - initial_capital) / initial_capital * 100) if equity and equity > 0 else 0.0

    result = {
        "mode": "SIMULATION",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "initial_capital": round(initial_capital, 2),
        "total_return_pct": round(total_return_pct, 2),
        "cash": round(cash, 2),
        "equity": round(equity, 2) if equity else cash,
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

    # 信号 & 交易历史 — 从日志解析
    result["signals"] = _parse_signals_from_log()
    result["trades"] = _parse_trades_from_log()

    # 更新 stats：如果日志中有交易数据，从日志取更准确的值
    if result["trades"]:
        trade_actions = [t for t in result["trades"] if t["action"] in ("平多", "平空")]
        if trade_actions:
            win_trades = sum(1 for t in trade_actions if t.get("pnl", 0) > 0)
            if win_trades > winning_trades:
                result["stats"]["winning_trades"] = win_trades
                result["stats"]["win_rate"] = round(win_trades / max(len(trade_actions), 1) * 100, 1)
                result["stats"]["total_trades"] = len(trade_actions)

    return result


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
        print(f"Signals: {len(data['signals'])} | Trades: {len(data['trades'])}")