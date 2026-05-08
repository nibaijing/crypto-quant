#!/usr/bin/env python3
"""
OptimizedV6 策略回测 — 用 OptimizedStrategy.on_bar() 模拟实盘逻辑
"""
import sys, time
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from execution.executor_v2 import FuturesExecutor
from strategies.spot.optimized_v6 import OptimizedStrategy
import strategies.spot.optimized_v6 as strat_mod

DATA = Path(__file__).parent / "data" / "backtest_btc_15m.csv"

def run_backtest():
    df = pd.read_csv(DATA)
    # 添加 datetime 列 (extract_15m_data.py 不输出 datetime)
    df["datetime"] = pd.to_datetime(df["open_time"], unit="ms")
    print(f"📊 回测数据: {len(df)} 条 K线")
    print(f"   范围: {df['datetime'].min()} ~ {df['datetime'].max()}")
    print()

    # 预计算指标（OptimizedStrategy 内置）
    strat = OptimizedStrategy()
    df = strat.compute_indicators(df)
    print("✅ 指标计算完成")

    # 初始化 executor
    executor = FuturesExecutor()
    executor.set_leverage(10)

    trades = []
    signal_log = []
    equity_curve = []

    n = len(df)
    COOLDOWN = 4  # 冷却期: 1小时
    for i in range(100, n):
        row = df.iloc[i]
        close_price = float(row["close"])

        # 动态杠杆
        vol = float(row.get("volatility", 0.003))
        if not np.isnan(vol):
            lev = strat.get_dynamic_leverage(vol)
            executor.set_leverage(lev)

        # 构建 bar
        bar = {
            "timestamp": int(row.get("open_time", 0)),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": close_price,
            "volume": float(row["volume"]),
            "history": df.iloc[: i + 1].copy(),
            "index": i,
            "position": None,
        }

        for col in ["ma_7", "ma_25", "ma_99", "rsi", "adx", "macd_hist", "volatility"]:
            if col in df.columns:
                val = row.get(col)
                if pd.notna(val):
                    bar[col] = float(val)

        if executor.position:
            pos = executor.position
            bar["position"] = type("obj", (object,), {
                "size": pos.size,
                "side": pos.side,
                "avg_price": pos.entry_price,
                "leverage": pos.leverage,
                "bars_held": pos.bars_held,
            })()

        # 更新价格
        executor.update_price("BTC-USDT", close_price)

        # 执行策略
        signal = None
        try:
            raw = strat.on_bar(bar, executor)
            executor.update_bars_held()
            # on_bar() 返回 SignalReport（新）或 str（旧）
            if raw is not None:
                if hasattr(raw, 'raw_signal'):
                    signal = raw.raw_signal  # SignalReport → 提取信号字符串
                else:
                    signal = str(raw)
        except Exception as e:
            pass

        symbol = "BTC-USDT"

        if signal and signal != "HOLD":
            signal_log.append({
                "time": row["datetime"],
                "signal": signal,
                "price": close_price,
                "rsi": float(row.get("rsi", 50)) if pd.notna(row.get("rsi")) else 50,
                "adx": float(row.get("adx", 20)) if pd.notna(row.get("adx")) else 20,
                "macd_hist": float(row.get("macd_hist", 0)) if pd.notna(row.get("macd_hist")) else 0,
                "m7": float(row.get("ma_7", 0)) if pd.notna(row.get("ma_7")) else 0,
                "m25": float(row.get("ma_25", 0)) if pd.notna(row.get("ma_25")) else 0,
            })

        if signal == "LONG" and (not executor.position or executor.position.size == 0):
            bars_since_exit = i - strat.last_exit_bar if strat.last_exit_bar >= 0 else COOLDOWN + 1
            if bars_since_exit <= COOLDOWN:
                pass  # 冷却中
            else:
                executor.buy(symbol, price=close_price)
        elif signal == "SELL" and executor.position and executor.position.side == "long":
            pnl = (close_price - executor.position.entry_price) / executor.position.entry_price * 100 * executor.position.leverage
            executor.sell(symbol, price=close_price)
            trades.append(("LONG", close_price, pnl, row["datetime"]))
        elif signal == "SHORT" and (not executor.position or executor.position.size == 0):
            bars_since_exit = i - strat.last_exit_bar if strat.last_exit_bar >= 0 else COOLDOWN + 1
            if bars_since_exit <= COOLDOWN:
                pass  # 冷却中
            else:
                executor.short_sell(symbol, price=close_price)
        elif signal == "COVER" and executor.position and executor.position.side == "short":
            pnl = (executor.position.entry_price - close_price) / executor.position.entry_price * 100 * executor.position.leverage
            executor.short_cover(symbol, price=close_price)
            trades.append(("SHORT", close_price, pnl, row["datetime"]))
        elif signal == "ADD_LONG" and executor.position and executor.position.side == "long":
            add_size = executor.position.size * 0.5
            executor.add_to_long(symbol, add_size, close_price)
        elif signal == "ADD_SHORT" and executor.position and executor.position.side == "short":
            add_size = executor.position.size * 0.5
            executor.add_to_short(symbol, add_size, close_price)
        elif signal == "REDUCE" and executor.position:
            reduce_size = executor.position.size * 0.5
            if executor.position.side == "long":
                executor.sell(symbol, price=close_price, size=reduce_size)
            else:
                executor.short_cover(symbol, price=close_price, size=reduce_size)

        equity_curve.append((row["datetime"], executor.equity))

    # 最终清算
    if executor.position:
        close_price = float(df.iloc[-1]["close"])
        if executor.position.side == "long":
            pnl = (close_price - executor.position.entry_price) / executor.position.entry_price * 100 * executor.position.leverage
        else:
            pnl = (executor.position.entry_price - close_price) / executor.position.entry_price * 100 * executor.position.leverage
        trades.append((executor.position.side.upper(), close_price, pnl, df.iloc[-1]["datetime"]))

    # 报告
    print()
    print("=" * 70)
    print("📊 回测结果")
    print("=" * 70)
    print(f"总交易: {len(trades)}")
    long_trades = [t for t in trades if t[0] == "LONG"]
    short_trades = [t for t in trades if t[0] == "SHORT"]
    print(f"LONG: {len(long_trades)} | SHORT: {len(short_trades)}")

    for t in trades:
        emoji = "🟢" if t[2] > 0 else "🔴"
        print(f"  {t[3]} | {t[0]:6s} | PnL={t[2]:+.2f}% {emoji}")

    total_ret = (executor.equity - 1000) / 1000 * 100
    wins = sum(1 for t in trades if t[2] > 0)
    wr = wins / max(len(trades), 1) * 100
    avg_win = sum(t[2] for t in trades if t[2] > 0) / max(sum(1 for t in trades if t[2] > 0), 1)
    avg_loss = sum(t[2] for t in trades if t[2] < 0) / max(sum(1 for t in trades if t[2] < 0), 1)

    print()
    print(f"最终权益: ${executor.equity:,.0f} ({total_ret:+.2f}%)")
    print(f"胜率: {wr:.1f}% ({wins}/{len(trades)})")
    print(f"平均盈利: {avg_win:+.2f}% | 平均亏损: {avg_loss:+.2f}% | 盈亏比: {abs(avg_win/avg_loss):.2f}" if avg_loss != 0 else "")

    long_wr = sum(1 for t in long_trades if t[2] > 0) / max(len(long_trades), 1) * 100
    short_wr = sum(1 for t in short_trades if t[2] > 0) / max(len(short_trades), 1) * 100
    print(f"LONG 胜率: {long_wr:.1f}% | SHORT 胜率: {short_wr:.1f}%")

    # 最大回撤
    eq = [e[1] for e in equity_curve]
    peak = eq[0]
    max_dd = 0
    for v in eq:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        max_dd = max(max_dd, dd)
    print(f"最大回撤: {max_dd:.2f}%")

    # 信号统计
    long_sigs = sum(1 for s in signal_log if s["signal"] == "LONG")
    short_sigs = sum(1 for s in signal_log if s["signal"] == "SHORT")
    sell_sigs = sum(1 for s in signal_log if s["signal"] == "SELL")
    cover_sigs = sum(1 for s in signal_log if s["signal"] == "COVER")
    print(f"信号: LONG={long_sigs} SHORT={short_sigs} SELL={sell_sigs} COVER={cover_sigs}")

    return trades, signal_log, equity_curve


if __name__ == "__main__":
    trades, signals, equity = run_backtest()