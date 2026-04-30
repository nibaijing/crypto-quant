#!/usr/bin/env python3
"""V5 — 多种策略框架并行搜索

不再局限 MA 交叉，同时测试:
1. RSI 均值回归 (超卖买、超买卖，不依赖趋势)
2. 布林带突破 (价格触及下轨买、上轨卖)
3. MACD 背离策略
4. 混合信号 (多个指标投票)

搜索所有策略×所有参数，找出收益最高的。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import numpy as np
import time
import logging
logging.basicConfig(level=logging.WARNING)
from core.config import init_config; init_config()


def compute_indicators(df):
    n = len(df)
    c = df["close"].values
    h = df["high"].values
    l = df["low"].values
    v = df["volume"].values
    
    ind = {}
    
    # MAs
    ind["ma7"] = pd.Series(c).rolling(7).mean().values
    ind["ma25"] = pd.Series(c).rolling(25).mean().values
    ind["ma99"] = pd.Series(c).rolling(99).mean().values
    
    # RSI
    delta = np.diff(c, prepend=c[0])
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    ag = pd.Series(gain).rolling(14).mean().values
    al = pd.Series(loss).rolling(14).mean().values
    rs = np.divide(ag, al, out=np.zeros_like(ag), where=al != 0)
    ind["rsi"] = 100 - 100 / (1 + rs)
    ind["rsi"][al == 0] = 100
    
    # MACD
    e12 = pd.Series(c).ewm(span=12, adjust=False).mean().values
    e26 = pd.Series(c).ewm(span=26, adjust=False).mean().values
    macd = e12 - e26
    ms = pd.Series(macd).ewm(span=9, adjust=False).mean().values
    ind["macd_hist"] = macd - ms
    
    # Bollinger Bands
    sma20 = pd.Series(c).rolling(20).mean().values
    std20 = pd.Series(c).rolling(20).std().values
    ind["bb_upper"] = sma20 + 2 * std20
    ind["bb_lower"] = sma20 - 2 * std20
    ind["bb_mid"] = sma20
    ind["bb_width"] = (ind["bb_upper"] - ind["bb_lower"]) / sma20
    
    # ATR
    tr = np.maximum(h - l, np.maximum(abs(h - np.roll(c, 1)), abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    ind["atr"] = pd.Series(tr).rolling(14).mean().values
    
    # Stochastic
    low14 = pd.Series(l).rolling(14).min().values
    high14 = pd.Series(h).rolling(14).max().values
    ind["stoch_k"] = (c - low14) / np.maximum(high14 - low14, 0.01) * 100
    
    # 成交量
    ind["vol_ma20"] = pd.Series(v).rolling(20).mean().values
    ind["vol_ratio"] = v / np.maximum(ind["vol_ma20"], 0.01)
    
    # 波动率
    rets = np.abs(np.diff(c) / np.maximum(c[:-1], 1e-6))
    rets = np.insert(rets, 0, 0)
    ind["volatility"] = pd.Series(rets).rolling(20).mean().values
    
    return ind, c


def backtest_strat(df, ind, c, signal_fn, params):
    """通用回测框架
    
    signal_fn(px, ind_values, i, pos) -> "LONG"/"SHORT"/"EXIT_LONG"/"EXIT_SHORT"/None
    """
    n = len(df)
    cash = 10000.0
    pos = 0.0
    entry_px = 0.0
    entry_atr = 0.0
    held = 0
    side = None
    trades = []
    eq = []
    
    min_bars = params.get("min_bars", 2)
    atr_mult = params.get("atr_mult", 2.0)
    
    start = max(params.get("warmup", 99), 99)
    
    for i in range(start, n):
        px = c[i]
        if np.isnan(px):
            eq.append(cash if pos == 0 else cash + pos * px)
            continue
        
        a = ind["atr"][i]
        if np.isnan(a) or a <= 0:
            a = px * 0.01
        
        if pos != 0:
            held += 1
        
        # ATR 止损
        if pos > 0 and entry_atr > 0 and held >= min_bars:
            if px <= entry_px - entry_atr * atr_mult:
                pnl = (px - entry_px) * pos
                cash += pos * px * 0.9995
                trades.append(pnl)
                pos, held, side = 0, 0, None
        
        elif pos < 0 and entry_atr > 0 and held >= min_bars:
            if px >= entry_px + entry_atr * atr_mult:
                sz = abs(pos)
                pnl = (entry_px - px) * sz
                cash += pnl - sz * px * 0.0005
                trades.append(pnl)
                pos, held, side = 0, 0, None
        
        # 指标值
        ind_vals = {k: ind[k][i] for k in ind}
        
        # 信号生成
        sig = signal_fn(px, ind_vals, held, side)
        
        if sig == "LONG" and pos == 0:
            size = (cash * params.get("size_ratio", 0.2)) / px
            cash -= size * px * 1.0005
            pos = size
            entry_px = px
            entry_atr = a
            held = 0
            side = "long"
        
        elif sig == "SHORT" and pos == 0 and params.get("allow_short", True):
            size = (cash * params.get("size_ratio", 0.2)) / px
            cash -= size * px * 0.0005
            pos = -size
            entry_px = px
            entry_atr = a
            held = 0
            side = "short"
        
        elif sig == "EXIT_LONG" and pos > 0 and held >= min_bars:
            pnl = (px - entry_px) * pos
            cash += pos * px * 0.9995
            trades.append(pnl)
            pos, held, side = 0, 0, None
        
        elif sig == "EXIT_SHORT" and pos < 0 and held >= min_bars:
            sz = abs(pos)
            pnl = (entry_px - px) * sz
            cash += pnl - sz * px * 0.0005
            trades.append(pnl)
            pos, held, side = 0, 0, None
        
        eqv = cash + (pos * px if pos > 0 else (0 if pos >= 0 else cash + (entry_px - px) * abs(pos)))
        eq.append(eqv)
    
    if not eq:
        return {"ret": 0, "dd": 0, "sharpe": 0, "trades": 0, "win": 0, "pf": 0}
    
    eq_arr = np.array(eq)
    peak = np.maximum.accumulate(eq_arr)
    dd = ((eq_arr - peak) / np.maximum(peak, 1e-6)).min() * 100
    ret = (eq_arr[-1] - 10000) / 10000 * 100
    rets_arr = np.diff(eq_arr) / np.maximum(eq_arr[:-1], 1e-6)
    sharpe = (np.mean(rets_arr) / np.std(rets_arr)) * np.sqrt(365 * 96) if len(rets_arr) > 1 and np.std(rets_arr) > 0 else 0
    wins = sum(1 for t in trades if t > 0)
    tot = len(trades)
    win_rate = wins / max(tot, 1) * 100
    pf = np.mean([t for t in trades if t > 0]) / abs(np.mean([t for t in trades if t <= 0])) if wins > 0 and tot > wins else 0
    
    return {"ret": round(ret, 2), "dd": round(dd, 2), "sharpe": round(sharpe, 2),
            "trades": tot, "win": round(win_rate, 1), "pf": round(pf, 2)}


# ===== 不同策略信号函数 =====

def signal_rsi_reversal(px, ind, held, side):
    """RSI 均值回归: 超卖买、超买卖"""
    rsi = ind.get("rsi", 50)
    rsi_lo = ind.get("_rsi_lo", 30)
    rsi_hi = ind.get("_rsi_hi", 70)
    if np.isnan(rsi): return None
    if rsi < rsi_lo: return "LONG"
    if rsi > rsi_hi: return "EXIT_LONG"
    return None


def signal_rsi_reversal_short(px, ind, held, side):
    """RSI 均值回归: 多空双向"""
    rsi = ind.get("rsi", 50)
    rsi_lo = ind.get("_rsi_lo", 35)
    rsi_hi = ind.get("_rsi_hi", 65)
    if np.isnan(rsi): return None
    if rsi < rsi_lo and side != "long": return "LONG"
    if rsi > rsi_hi and side != "short": return "SHORT"
    if side == "long" and rsi > 50: return "EXIT_LONG"
    if side == "short" and rsi < 50: return "EXIT_SHORT"
    return None


def signal_bb_break(px, ind, held, side):
    """布林带突破: 跌穿下轨买，突破上轨卖"""
    lb = ind.get("bb_lower", px)
    ub = ind.get("bb_upper", px)
    if np.isnan(lb) or np.isnan(ub): return None
    if px <= lb * 1.005 and side != "long": return "LONG"
    if px >= ub * 0.995: return "EXIT_LONG"
    return None


def signal_bb_break_short(px, ind, held, side):
    """布林带多空双向"""
    lb = ind.get("bb_lower", px)
    ub = ind.get("bb_upper", px)
    mb = ind.get("bb_mid", px)
    if np.isnan(lb): return None
    if px <= lb * 1.005 and side != "long": return "LONG"
    if px >= ub * 0.995 and side != "short": return "SHORT"
    if side == "long" and px > mb: return "EXIT_LONG"
    if side == "short" and px < mb: return "EXIT_SHORT"
    return None


def signal_stoch(px, ind, held, side):
    """Stochastic 超卖超买"""
    k = ind.get("stoch_k", 50)
    if np.isnan(k): return None
    if k < 20 and side != "long": return "LONG"
    if k > 80: return "EXIT_LONG"
    return None


def signal_triple_confirm(px, ind, held, side):
    """三指标确认: RSI+MACD+BB"""
    rsi = ind.get("rsi", 50)
    macd = ind.get("macd_hist", 0)
    lb = ind.get("bb_lower", px)
    if any(np.isnan(v) for v in [rsi, macd, lb]): return None
    
    long_score = (rsi < 40) + (macd > 0) + (px < lb * 1.02)
    short_score = (rsi > 60) + (macd < 0) + (px > ind.get("bb_upper", px) * 0.98)
    
    if long_score >= 2 and side != "long": return "LONG"
    if short_score >= 2 and side != "short": return "SHORT"
    if side == "long" and long_score == 0: return "EXIT_LONG"
    if side == "short" and short_score == 0: return "EXIT_SHORT"
    return None


def signal_volatility_breakout(px, ind, held, side):
    """波动率突破: 低波动后放量突破"""
    vol = ind.get("volatility", 0)
    vol_ratio = ind.get("vol_ratio", 1)
    ma7 = ind.get("ma7", px)
    ma25 = ind.get("ma25", px)
    bb_w = ind.get("bb_width", 0.1)
    
    if any(np.isnan(v) for v in [vol, vol_ratio, ma7, ma25, bb_w]): return None
    
    # BB收缩 + 放量 = 即将突破
    squeeze = bb_w < 0.05
    breakout = vol_ratio > 1.5
    
    if squeeze and breakout and side != "long" and ma7 > ma25: return "LONG"
    if squeeze and breakout and side != "short" and ma7 < ma25: return "SHORT"
    if side == "long" and vol_ratio < 0.8: return "EXIT_LONG"
    if side == "short" and vol_ratio < 0.8: return "EXIT_SHORT"
    return None


if __name__ == "__main__":
    df = pd.read_csv("data/klines/BTC_USDT_15m.csv")
    print(f"数据: {len(df)} 行")
    
    t0 = time.time()
    ind, c = compute_indicators(df)
    print(f"指标计算: {time.time()-t0:.1f}s")
    
    # 所有策略+参数组合
    strategies = [
        ("RSI回归(单向)", signal_rsi_reversal, [
            {"_rsi_lo": lo, "_rsi_hi": hi, "min_bars": mb, "atr_mult": atr, "allow_short": False}
            for lo in [25, 30, 35] for hi in [65, 70, 75]
            for mb in [1, 2] for atr in [1.5, 2.0]
        ]),
        ("RSI回归(双向)", signal_rsi_reversal_short, [
            {"_rsi_lo": lo, "_rsi_hi": hi, "min_bars": mb, "atr_mult": atr, "allow_short": True}
            for lo in [30, 35, 40] for hi in [60, 65, 70]
            for mb in [1, 2] for atr in [1.5, 2.0]
        ]),
        ("BB突破(单向)", signal_bb_break, [
            {"min_bars": mb, "atr_mult": atr, "allow_short": False}
            for mb in [1, 2] for atr in [1.5, 2.0]
        ]),
        ("BB突破(双向)", signal_bb_break_short, [
            {"min_bars": mb, "atr_mult": atr, "allow_short": True}
            for mb in [1, 2] for atr in [1.5, 2.0]
        ]),
        ("Stochastic", signal_stoch, [
            {"min_bars": mb, "atr_mult": atr, "allow_short": False}
            for mb in [1, 2] for atr in [1.5, 2.0]
        ]),
        ("三重确认", signal_triple_confirm, [
            {"min_bars": mb, "atr_mult": atr, "allow_short": s}
            for mb in [2, 3] for atr in [1.5, 2.0] for s in [False, True]
        ]),
        ("波动突破", signal_volatility_breakout, [
            {"min_bars": mb, "atr_mult": atr, "allow_short": s}
            for mb in [1, 2] for atr in [1.5, 2.0] for s in [False, True]
        ]),
    ]
    
    all_results = []
    total_combos = sum(len(params) for _, _, params in strategies)
    print(f"策略数: {len(strategies)} | 总组合: {total_combos}")
    
    t0 = time.time()
    for name, fn, param_list in strategies:
        for params in param_list:
            # 把特殊参数注入到ind里(RSI阈值等)
            for k, v in params.items():
                if k.startswith("_"):
                    ind[k] = np.full(len(c), v)
            
            r = backtest_strat(df, ind, c, fn, params)
            r["strategy"] = name
            r["_rsi_lo"] = params.get("_rsi_lo", 0)
            r["_rsi_hi"] = params.get("_rsi_hi", 0)
            r["allow_short"] = params.get("allow_short", False)
            r["min_bars"] = params.get("min_bars", 0)
            r["atr_mult"] = params.get("atr_mult", 0)
            all_results.append(r)
    
    all_results.sort(key=lambda x: x["ret"], reverse=True)
    
    print(f"\n🏆 Top 20:")
    print(f"{'#':<4} {'策略':<16} {'多空':<5} {'收益':>8} {'回撤':>8} {'夏普':>7} {'交易':>5} {'胜率':>7} {'盈亏比':>7}")
    print("-"*80)
    for i, x in enumerate(all_results[:20]):
        short_icon = "Y" if x["allow_short"] else "N"
        print(f"{i+1:<4} {x['strategy']:<16} {short_icon:<5} {x['ret']:>+7.2f}% {x['dd']:>+7.2f}% {x['sharpe']:>6.2f} {x['trades']:>5} {x['win']:>5.1f}% {x['pf']:>6.2f}")
    
    print(f"\n⏱ 耗时: {time.time()-t0:.1f}s ({total_combos}组)")
    
    best = all_results[0]
    print(f"\n🏆 BEST: {best['strategy']} | 收益: {best['ret']:+.2f}% | 回撤: {best['dd']:+.2f}% | 夏普: {best['sharpe']:.2f}")
    
    # 保存结果
    import json
    with open("data/v5_results.json", "w") as f:
        json.dump(all_results[:20], f, indent=2)
    print("结果已保存: data/v5_results.json")