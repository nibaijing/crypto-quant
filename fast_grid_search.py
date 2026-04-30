#!/usr/bin/env python3
"""快速向量化回测 — 直接在 DataFrame 上跑，无事件循环开销

支持多空双向策略，一次回测 < 1秒。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import numpy as np
import logging
logging.basicConfig(level=logging.WARNING)

from core.config import init_config
init_config()


def fast_backtest(df: pd.DataFrame, initial_capital: float = 10000,
                  rsi_lo: float = 40, rsi_hi: float = 60,
                  atr_mult: float = 2.0, min_bars: int = 4,
                  allow_short: bool = True, vol_mult: float = 1.2,
                  commission: float = 0.0005) -> dict:
    """向量化快速回测
    
    直接在 DataFrame 上运行策略，无事件循环，极快。
    """
    n = len(df)
    if n < 100:
        return {"total_return_pct": 0, "max_drawdown_pct": 0, "sharpe_ratio": 0,
                "total_trades": 0, "win_rate_pct": 0, "profit_factor": 0}
    
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    volumes = df["volume"].values
    
    # === 预计算所有指标 (向量化) ===
    # MA
    ma7 = pd.Series(closes).rolling(7).mean().values
    ma25 = pd.Series(closes).rolling(25).mean().values
    ma99 = pd.Series(closes).rolling(99).mean().values
    
    # RSI
    delta = np.diff(closes, prepend=closes[0])
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gain).rolling(14).mean().values
    avg_loss = pd.Series(loss).rolling(14).mean().values
    rs = np.divide(avg_gain, avg_loss, out=np.zeros_like(avg_gain), where=avg_loss != 0)
    rsi = 100 - 100 / (1 + rs)
    rsi[avg_loss == 0] = 100
    
    # MACD
    ema12 = pd.Series(closes).ewm(span=12, adjust=False).mean().values
    ema26 = pd.Series(closes).ewm(span=26, adjust=False).mean().values
    macd = ema12 - ema26
    macd_signal = pd.Series(macd).ewm(span=9, adjust=False).mean().values
    macd_hist = macd - macd_signal
    
    # ATR
    tr = np.maximum(
        highs - lows,
        np.maximum(abs(highs - np.roll(closes, 1)), abs(lows - np.roll(closes, 1)))
    )
    tr[0] = highs[0] - lows[0]
    atr = pd.Series(tr).rolling(14).mean().values
    
    # 成交量比率
    vol_ma = pd.Series(volumes).rolling(20).mean().values
    vol_ratio = volumes / np.where(vol_ma > 0, vol_ma, 1)
    
    # MA99趋势
    regime = np.full(n, "neutral", dtype=object)
    for i in range(99, n):
        if not np.isnan(ma99[i]):
            if closes[i] > ma99[i] * 1.02:
                regime[i] = "bull"
            elif closes[i] < ma99[i] * 0.98:
                regime[i] = "bear"
    
    # === 信号向量化 ===
    long_ok = np.isin(regime, ["bull", "neutral"])
    short_ok = np.isin(regime, ["bear", "neutral"])
    
    long_signal = (
        long_ok &
        (ma7 > ma25) &
        (rsi < rsi_lo) &
        (macd_hist > 0) &
        (vol_ratio > vol_mult)
    )
    
    short_signal = (
        allow_short &
        short_ok &
        (ma7 < ma25) &
        (rsi > 55) &
        (macd_hist < 0) &
        (vol_ratio > vol_mult)
    )
    
    # === 模拟逐笔交易 ===
    cash = initial_capital
    position = 0          # >0: long size, <0: short size
    entry_price = 0
    entry_atr = 0
    bars_held = 0
    position_side = None  # 'long' / 'short' / None
    
    equity_curve = []
    trades = []
    
    for i in range(99, n):
        close = closes[i]
        
        # 持仓更新
        if position != 0:
            bars_held += 1
        
        # ATR 止损
        if position > 0 and entry_atr > 0:
            stop = entry_price - entry_atr * atr_mult
            if close <= stop and bars_held >= min_bars:
                pnl = (close - entry_price) * position
                cash += position * close * (1 - commission)
                trades.append(("long_stop", entry_price, close, pnl))
                position, entry_price, bars_held, position_side = 0, 0, 0, None
        
        elif position < 0 and entry_atr > 0:
            stop = entry_price + entry_atr * atr_mult
            if close >= stop and bars_held >= min_bars:
                size = abs(position)
                pnl = (entry_price - close) * size
                cash -= size * close * (1 - commission)
                # 平空: 返还保证金+盈亏
                cash += pnl
                trades.append(("short_stop", entry_price, close, pnl))
                position, entry_price, bars_held, position_side = 0, 0, 0, None
        
        # 多空信号执行
        if position == 0:
            if long_signal[i] and not np.isnan(atr[i]):
                size = (cash * 0.2) / close
                cost = size * close
                cash -= cost * (1 + commission)
                position = size
                entry_price = close
                entry_atr = atr[i] if not np.isnan(atr[i]) else close * 0.02
                bars_held = 0
                position_side = "long"
            
            elif short_signal[i] and not np.isnan(atr[i]) and allow_short:
                size = (cash * 0.2) / close
                cash -= size * close * commission  # 只扣手续费
                position = -size
                entry_price = close
                entry_atr = atr[i] if not np.isnan(atr[i]) else close * 0.02
                bars_held = 0
                position_side = "short"
        
        # 平仓信号
        elif position > 0 and bars_held >= min_bars:
            sell_cond = (ma7[i] < ma25[i]) or (rsi[i] > rsi_hi)
            if sell_cond and not np.isnan(ma7[i]):
                pnl = (close - entry_price) * position
                cash += position * close * (1 - commission)
                trades.append(("long_exit", entry_price, close, pnl))
                position, entry_price, bars_held, position_side = 0, 0, 0, None
        
        elif position < 0 and bars_held >= min_bars:
            cover_cond = (ma7[i] > ma25[i]) or (rsi[i] < rsi_lo)
            if cover_cond and not np.isnan(ma7[i]):
                size = abs(position)
                pnl = (entry_price - close) * size
                cash -= size * close * commission
                cash += pnl
                trades.append(("short_exit", entry_price, close, pnl))
                position, entry_price, bars_held, position_side = 0, 0, 0, None
        
        # 记录权益
        eq = cash
        if position > 0:
            eq = cash + position * close
        elif position < 0:
            eq = cash + (entry_price - close) * abs(position)
        equity_curve.append(eq)
    
    # === 统计 ===
    eq_arr = np.array(equity_curve)
    peak = np.maximum.accumulate(eq_arr)
    dd = (eq_arr - peak) / np.where(peak > 0, peak, 1)
    max_dd = dd.min() * 100
    
    final_eq = eq_arr[-1] if len(eq_arr) > 0 else initial_capital
    total_ret = (final_eq - initial_capital) / initial_capital * 100
    
    # 夏普
    rets = np.diff(eq_arr) / eq_arr[:-1]
    sharpe = (np.mean(rets) / np.std(rets)) * np.sqrt(365 * 96) if len(rets) > 1 and np.std(rets) > 0 else 0
    
    # 交易统计
    wins = sum(1 for t in trades if t[3] > 0)
    loss_count = sum(1 for t in trades if t[3] <= 0)
    total_t = len(trades)
    win_rate = wins / max(total_t, 1) * 100
    
    avg_win_pnl = np.mean([t[3] for t in trades if t[3] > 0]) if wins > 0 else 0
    avg_loss_pnl = abs(np.mean([t[3] for t in trades if t[3] <= 0])) if loss_count > 0 else 0.01
    profit_factor = avg_win_pnl / avg_loss_pnl if avg_loss_pnl > 0 else 0
    
    return {
        "total_return_pct": round(total_ret, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": round(sharpe, 2),
        "total_trades": total_t,
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
    }


if __name__ == "__main__":
    import time
    
    df = pd.read_csv("data/klines/BTC_USDT_15m.csv")
    print(f"数据: {len(df)} 根K线 (15m)")
    
    # 基准
    t0 = time.time()
    r = fast_backtest(df, rsi_lo=40, rsi_hi=60, atr_mult=2.0, min_bars=4)
    t1 = time.time()
    print(f"\nV3 基准 (RSI 40-60, ATR 2x, Hold 4):")
    print(f"  收益: {r['total_return_pct']:+.2f}% | 回撤: {r['max_drawdown_pct']:+.2f}%")
    print(f"  交易: {r['total_trades']} | 胜率: {r['win_rate_pct']:.1f}% | 盈亏比: {r['profit_factor']:.2f}")
    print(f"  耗时: {t1-t0:.2f}s")
    
    # 网格搜索
    print("\n" + "="*60)
    print("网格搜索 (全量90天)...")
    print("="*60)
    
    t0 = time.time()
    results = []
    combos = []
    
    for rsi_lo in [35, 40, 45]:
        for rsi_hi in [55, 60, 65]:
            for atr_m in [1.5, 2.0, 2.5, 3.0]:
                for mb in [2, 4, 6]:
                    r = fast_backtest(df, rsi_lo=rsi_lo, rsi_hi=rsi_hi, atr_mult=atr_m, min_bars=mb)
                    r["rsi_lo"] = rsi_lo
                    r["rsi_hi"] = rsi_hi
                    r["atr_m"] = atr_m
                    r["min_bars"] = mb
                    results.append(r)
    
    results.sort(key=lambda x: x["total_return_pct"], reverse=True)
    t1 = time.time()
    
    print(f"\n{'排名':<5} {'RSI':<8} {'ATR':<6} {'Hold':<6} {'收益':>8} {'回撤':>8} {'夏普':>7} {'交易':>5} {'胜率':>7} {'盈亏比':>7}")
    print("-"*75)
    for i, x in enumerate(results[:15]):
        rsi_s = f"{x['rsi_lo']}-{x['rsi_hi']}"
        print(f"{i+1:<5} {rsi_s:<8} {x['atr_m']:<6.1f} {x['min_bars']:<6} {x['total_return_pct']:>+7.2f}% {x['max_drawdown_pct']:>+7.2f}% {x['sharpe_ratio']:>6.2f} {x['total_trades']:>5} {x['win_rate_pct']:>5.1f}% {x['profit_factor']:>6.2f}")
    
    print(f"\n⏱ 总耗时: {t1-t0:.2f}s (108组)")
    
    # 最佳参数
    best = results[0]
    print(f"\n🏆 最佳参数: RSI={best['rsi_lo']}-{best['rsi_hi']} | ATR={best['atr_m']}x | Hold={best['min_bars']}")
    print(f"   收益: {best['total_return_pct']:+.2f}% | 回撤: {best['max_drawdown_pct']:+.2f}% | 夏普: {best['sharpe_ratio']:.2f}")