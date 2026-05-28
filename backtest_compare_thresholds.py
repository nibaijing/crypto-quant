#!/usr/bin/env python3
"""
新旧阈值对比回测。
策略代码当前已经是新阈值（已在 optimized_v6.py 中修改），所以回测一次。
要得到"旧阈值"的结果，需要热修策略模块的参数再跑一次。
"""
import sys, os, copy
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from execution.executor_v2 import FuturesExecutor
import strategies.spot.optimized_v6 as strat_mod
from strategies.spot.optimized_v6 import OptimizedStrategy
from execution.ai_override import DecisionEngine

DATA = Path(__file__).parent / "data" / "backtest_btc_15m.csv"

# Old threshold parameters (before changes)
OLD_PARAMS = {
    "SIGNAL_THRESHOLD": 0.60,
    "ADX_THRESHOLD": 23,
    "ADX_NO_TRADE": 0,  # did not exist, equivalent to 0
}

# New threshold parameters (current code)
NEW_PARAMS = {
    "SIGNAL_THRESHOLD": 0.65,
    "ADX_THRESHOLD": 23,
    "ADX_NO_TRADE": 15,
}

def run_backtest_with_params(params, label="backtest"):
    """用指定参数跑回测（热修模块级常量）"""
    # 保存旧值并热修
    old_sig = strat_mod.SIGNAL_THRESHOLD
    old_adx = strat_mod.ADX_THRESHOLD
    old_no_trade = getattr(strat_mod, "ADX_NO_TRADE", 0)

    strat_mod.SIGNAL_THRESHOLD = params["SIGNAL_THRESHOLD"]
    strat_mod.ADX_THRESHOLD = params["ADX_THRESHOLD"]
    strat_mod.ADX_NO_TRADE = params["ADX_NO_TRADE"]

    try:
        df = pd.read_csv(DATA)
        df["datetime"] = pd.to_datetime(df["open_time"], unit="ms")
        print(f"\n{'='*60}")
        print(f"📊 {label}")
        print(f"   SIGNAL_THRESHOLD={params['SIGNAL_THRESHOLD']}  ADX_NO_TRADE={params['ADX_NO_TRADE']}")
        print(f"   数据: {len(df)} K线")
        print(f"   范围: {df['datetime'].min()} ~ {df['datetime'].max()}")

        strat = OptimizedStrategy()
        df = strat.compute_indicators(df)

        executor = FuturesExecutor()
        executor.set_leverage(10)
        os.environ["AI_OVERRIDE_DRY_RUN"] = "1"
        engine = DecisionEngine()

        trades = []
        signal_log = []
        equity_curve = []

        n = len(df)
        COOLDOWN = 4
        for i in range(100, n):
            row = df.iloc[i]
            close_price = float(row["close"])

            vol = float(row.get("volatility", 0.003))
            if not np.isnan(vol):
                lev = strat.get_dynamic_leverage(vol)
                executor.set_leverage(lev)

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

            if executor.position:
                pos = executor.position
                bar["position"] = type("obj", (object,), {
                    "size": pos.size,
                    "side": pos.side,
                    "avg_price": pos.entry_price,
                    "leverage": pos.leverage,
                    "bars_held": pos.bars_held,
                })()

            executor.update_price("BTC-USDT", close_price)
        signal = None
        try:
            report = strat.on_bar(bar, executor)
            executor.update_bars_held()
            if report is not None and hasattr(report, 'raw_signal'):
                # 直接使用策略的原始信号
                raw = report.raw_signal
                if report.exit_signal:
                    exit_map = {"EXIT_ATR": "SELL", "EXIT_TIME": "SELL",
                                "EXIT_RSI": "SELL", "EXIT_TREND": "SELL",
                                "SELL": "SELL", "COVER": "COVER"}
                    signal = exit_map.get(report.exit_signal, "SELL")
                    if report.exit_signal in ("EXIT_TIME", "EXIT_RSI", "EXIT_TREND"):
                        signal = "SELL" if report.raw_signal == "SELL" else "COVER"
                elif raw in ("LONG", "SHORT"):
                    signal = raw
                elif raw == "HOLD":
                    use_w = report.long_score_w >= 0.50 and report.long_score_w > report.short_score_w
                    use_s = report.short_score_w >= 0.50 and report.short_score_w > report.long_score_w
                    if use_w:
                        signal = "LONG"
                    elif use_s:
                        signal = "SHORT"
                elif raw in ("ADD_LONG", "ADD_SHORT", "REDUCE"):
                    signal = raw
        except Exception:
            pass

        symbol = "BTC-USDT"

        if signal and signal != "HOLD":
            signal_log.append({
                "time": row["datetime"],
                "signal": signal,
                "price": close_price,
                "rsi": float(row.get("rsi", 50)) if pd.notna(row.get("rsi")) else 50,
                "adx": float(row.get("adx", 20)) if pd.notna(row.get("adx")) else 20,
            })

            if signal == "LONG" and (not executor.position or executor.position.size == 0):
                bars_since_exit = i - strat.last_exit_bar if strat.last_exit_bar >= 0 else COOLDOWN + 1
                if bars_since_exit > COOLDOWN:
                    executor.buy(symbol, price=close_price)
            elif signal == "SELL" and executor.position and executor.position.side == "long":
                pnl = (close_price - executor.position.entry_price) / executor.position.entry_price * 100 * executor.position.leverage
                executor.sell(symbol, price=close_price)
                trades.append(("LONG", close_price, pnl, row["datetime"]))
            elif signal == "SHORT" and (not executor.position or executor.position.size == 0):
                bars_since_exit = i - strat.last_exit_bar if strat.last_exit_bar >= 0 else COOLDOWN + 1
                if bars_since_exit > COOLDOWN:
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

        # 计算指标
        total_ret = (executor.equity - 1000) / 1000 * 100
        wins = sum(1 for t in trades if t[2] > 0)
        wr = wins / max(len(trades), 1) * 100
        avg_win = sum(t[2] for t in trades if t[2] > 0) / max(wins, 1)
        avg_loss = sum(t[2] for t in trades if t[2] < 0) / max(len(trades) - wins, 1)

        eq = [e[1] for e in equity_curve]
        peak = eq[0]
        max_dd = 0
        for v in eq:
            if v > peak:
                peak = v
            dd = (peak - v) / peak * 100
            max_dd = max(max_dd, dd)

        long_trades = [t for t in trades if t[0] == "LONG"]
        short_trades = [t for t in trades if t[0] == "SHORT"]

        long_sigs = sum(1 for s in signal_log if s["signal"] == "LONG")
        short_sigs = sum(1 for s in signal_log if s["signal"] == "SHORT")
        sell_sigs = sum(1 for s in signal_log if s["signal"] == "SELL")
        cover_sigs = sum(1 for s in signal_log if s["signal"] == "COVER")

        print(f"\n结果:")
        print(f"  总交易: {len(trades)} (LONG={len(long_trades)} SHORT={len(short_trades)})")
        print(f"  最终权益: ${executor.equity:,.0f} ({total_ret:+.2f}%)")
        print(f"  胜率: {wr:.1f}% ({wins}/{len(trades)})")
        print(f"  平均盈利: {avg_win:+.2f}% | 平均亏损: {avg_loss:+.2f}%")
        print(f"  盈亏比: {abs(avg_win/avg_loss):.2f}" if avg_loss != 0 else "  N/A")
        print(f"  最大回撤: {max_dd:.2f}%")
        print(f"  信号: LONG={long_sigs} SHORT={short_sigs} SELL={sell_sigs} COVER={cover_sigs}")

        return {
            "trades": len(trades),
            "long_trades": len(long_trades),
            "short_trades": len(short_trades),
            "final_equity": executor.equity,
            "total_return_pct": total_ret,
            "win_rate": wr,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": abs(avg_win/avg_loss) if avg_loss != 0 else 0,
            "max_drawdown": max_dd,
            "long_signals": long_sigs,
            "short_signals": short_sigs,
        }

    finally:
        # 恢复参数
        strat_mod.SIGNAL_THRESHOLD = old_sig
        strat_mod.ADX_THRESHOLD = old_adx
        strat_mod.ADX_NO_TRADE = old_no_trade


def print_comparison(old, new):
    print(f"\n{'='*70}")
    print("📊 阈值对比总结")
    print("=" * 70)
    metrics = [
        ("总交易", "trades", "d", "减少"),
        ("LONG 交易", "long_trades", "d", ""),
        ("SHORT 交易", "short_trades", "d", ""),
        ("最终权益", "final_equity", ".0f", ""),
        ("总收益率", "total_return_pct", "+.2f", "%"),
        ("胜率", "win_rate", ".1f", "%"),
        ("平均盈利", "avg_win", "+.2f", "%"),
        ("平均亏损", "avg_loss", "+.2f", "%"),
        ("盈亏比", "profit_factor", ".2f", ""),
        ("最大回撤", "max_drawdown", ".2f", "%"),
        ("LONG 信号", "long_signals", "d", ""),
        ("SHORT 信号", "short_signals", "d", ""),
    ]
    for name, key, fmt, suffix in metrics:
        ov = old[key]
        nv = new[key]
        fmt_s = f"{ov:{fmt}}{suffix}" if isinstance(ov, float) else str(ov)
        fmt_n = f"{nv:{fmt}}{suffix}" if isinstance(nv, float) else str(nv)
        arrow = "→"
        if name in ("总交易", "LONG 交易", "SHORT 交易", "最大回撤", "平均亏损"):
            better = nv < ov if isinstance(nv, (int, float)) else False
            sign = "✅" if better else ""
        else:
            better = nv > ov if isinstance(nv, (int, float)) else False
            sign = "✅" if better else ""
        print(f"  {name:12s}: {fmt_s:>10s} {arrow} {fmt_n:>10s}  {sign}")


if __name__ == "__main__":
    print("🔥 回测对比: 旧阈值 vs 新阈值")
    print("   使用 backtest_btc_15m.csv (30天数据)")

    old_results = run_backtest_with_params(OLD_PARAMS, "旧阈值 (SIG=0.60, NO_TRADE=0)")
    new_results = run_backtest_with_params(NEW_PARAMS, "新阈值 (SIG=0.65, NO_TRADE=15)")

    print_comparison(old_results, new_results)