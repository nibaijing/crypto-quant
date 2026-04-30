#!/usr/bin/env python3
"""V4 向量化策略搜索引擎

在 V3 基础上尝试多种改进:
1. 趋势强度过滤 (ADX/斜率)
2. 动态仓位 (波动率越高仓位越小)
3. 移动止损 (trailing stop)
4. 时间过滤 (避开低波动时段)
5. MA交叉延迟确认 (避免假突破)

直接枚举所有组合，找出收益为正的配置。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import numpy as np
import time
import json
import logging
logging.basicConfig(level=logging.WARNING)
from core.config import init_config; init_config()


def compute_indicators(df: pd.DataFrame) -> dict:
    """一次性预计算所有指标"""
    n = len(df)
    o, h, l, c, v = df["open"].values, df["high"].values, df["low"].values, df["close"].values, df["volume"].values
    
    # MAs
    ind = {
        "ma7": pd.Series(c).rolling(7).mean().values,
        "ma25": pd.Series(c).rolling(25).mean().values,
        "ma99": pd.Series(c).rolling(99).mean().values,
    }
    
    # RSI
    delta = np.diff(c, prepend=c[0])
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    ag = pd.Series(gain).rolling(14).mean().values
    al = pd.Series(loss).rolling(14).mean().values
    rs_arr = np.divide(ag, al, out=np.zeros_like(ag), where=al != 0)
    ind["rsi"] = 100 - 100 / (1 + rs_arr)
    ind["rsi"][al == 0] = 100
    
    # MACD
    e12 = pd.Series(c).ewm(span=12, adjust=False).mean().values
    e26 = pd.Series(c).ewm(span=26, adjust=False).mean().values
    macd = e12 - e26
    ms = pd.Series(macd).ewm(span=9, adjust=False).mean().values
    ind["macd_hist"] = macd - ms
    
    # ATR
    tr = np.maximum(h - l, np.maximum(abs(h - np.roll(c, 1)), abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    ind["atr"] = pd.Series(tr).rolling(14).mean().values
    ind["atr_pct"] = ind["atr"] / c  # ATR百分比
    
    # ADX
    p_dm = np.where((d1 := np.diff(h, prepend=h[0])) > (d2 := -np.diff(l, prepend=l[0])), np.maximum(d1, 0), 0)
    m_dm = np.where(d2 > d1, np.maximum(d2, 0), 0)
    atr14 = pd.Series(tr).rolling(14).mean().values
    p_di = pd.Series(p_dm).rolling(14).mean().values / atr14 * 100
    m_di = pd.Series(m_dm).rolling(14).mean().values / atr14 * 100
    dx = np.abs(p_di - m_di) / np.maximum(p_di + m_di, 0.001) * 100
    ind["adx"] = pd.Series(dx).rolling(14).mean().values
    
    # 波动率
    rets = np.diff(c) / c[:-1]
    rets = np.insert(rets, 0, 0)
    ind["volatility"] = pd.Series(np.abs(rets)).rolling(20).mean().values
    
    # 成交量
    ind["vol_ma20"] = pd.Series(v).rolling(20).mean().values
    ind["vol_ratio"] = v / np.maximum(ind["vol_ma20"], 0.01)
    
    # 市场状态
    ind["regime"] = np.full(n, "neutral", dtype=object)
    for i in range(99, n):
        if not np.isnan(ind["ma99"][i]):
            if c[i] > ind["ma99"][i] * 1.02:
                ind["regime"][i] = "bull"
            elif c[i] < ind["ma99"][i] * 0.98:
                ind["regime"][i] = "bear"
    
    return ind, c, o, h, l, v


def run_strategy(df, ind, c, **params):
    """运行一次策略回测，返回结果"""
    n = len(df)
    
    # 参数提取
    rsi_lo = params.get("rsi_lo", 40)
    rsi_hi = params.get("rsi_hi", 60)
    atr_mult = params.get("atr_mult", 2.0)
    min_bars = params.get("min_bars", 4)
    allow_short = params.get("allow_short", True)
    vol_mult = params.get("vol_mult", 1.2)
    use_adx = params.get("use_adx", False)
    adx_thresh = params.get("adx_thresh", 20)
    use_trail = params.get("use_trail", False)
    trail_atr = params.get("trail_atr", 1.0)
    dynamic_size = params.get("dynamic_size", False)
    vol_size_div = params.get("vol_size_div", 2.0)
    delay_bars = params.get("delay_bars", 0)  # MA交叉延迟确认
    
    cash = 10000.0
    pos = 0.0         # >0 long, <0 short
    entry_px = 0.0
    entry_atr = 0.0
    held = 0
    side = None
    trail_high = 0.0  # 做多移动止损最高价
    trail_low = 999999  # 做空移动止损最低价
    
    trades = []
    eq = []
    
    for i in range(99, n):
        px = c[i]
        if np.isnan(px): continue
        
        # 更新 trail
        if pos > 0:
            trail_high = max(trail_high, px)
        elif pos < 0:
            trail_low = min(trail_low, px)
        
        if pos != 0:
            held += 1
        
        # ATR
        a = ind["atr"][i]
        if np.isnan(a) or a <= 0:
            a = px * 0.01
        
        # === 止损 ===
        if pos > 0 and entry_atr > 0 and held >= min_bars:
            stop = entry_px - entry_atr * atr_mult
            if px <= stop:
                pnl = (px - entry_px) * pos
                cash += pos * px * 0.9995  # -commission
                trades.append(("long_stop", pnl))
                pos, held, side, trail_high = 0, 0, None, 0
        
        elif pos < 0 and entry_atr > 0 and held >= min_bars:
            stop = entry_px + entry_atr * atr_mult
            if px >= stop:
                sz = abs(pos)
                pnl = (entry_px - px) * sz
                cash += pnl - sz * px * 0.0005
                trades.append(("short_stop", pnl))
                pos, held, side, trail_low = 0, 0, None, 999999
        
        # 移动止损
        if use_trail and pos != 0 and held >= min_bars:
            if pos > 0:
                tstop = trail_high - entry_atr * trail_atr
                if px <= tstop:
                    pnl = (px - entry_px) * pos
                    cash += pos * px * 0.9995
                    trades.append(("long_trail", pnl))
                    pos, held, side, trail_high = 0, 0, None, 0
            elif pos < 0:
                tstop = trail_low + entry_atr * trail_atr
                if px >= tstop:
                    sz = abs(pos)
                    pnl = (entry_px - px) * sz
                    cash += pnl - sz * px * 0.0005
                    trades.append(("short_trail", pnl))
                    pos, held, side, trail_low = 0, 0, None, 999999
        
        # === 开仓 ===
        if pos == 0:
            rsi_val = ind["rsi"][i]
            macd_val = ind["macd_hist"][i]
            vol_ratio = ind["vol_ratio"][i]
            regime = ind["regime"][i]
            adx_val = ind["adx"][i]
            vol_pct = ind["volatility"][i]
            
            if np.isnan(rsi_val) or np.isnan(macd_val) or np.isnan(vol_ratio):
                continue
            
            # 做多
            long_ok = regime in ("bull", "neutral")
            if use_adx and not np.isnan(adx_val):
                long_ok = long_ok and adx_val > adx_thresh
            
            if long_ok and ind["ma7"][i] > ind["ma25"][i] and rsi_val < rsi_lo and macd_val > 0 and vol_ratio > vol_mult:
                # 动态仓位
                size_ratio = 0.2
                if dynamic_size and not np.isnan(vol_pct) and vol_pct > 0:
                    size_ratio = min(0.3, 0.1 / (vol_pct * vol_size_div))
                
                size = (cash * size_ratio) / px
                cost = size * px
                cash -= cost * 1.0005
                pos = size
                entry_px = px
                entry_atr = a
                held = 0
                side = "long"
                trail_high = px
            
            # 做空
            elif allow_short:
                short_ok = regime in ("bear", "neutral")
                if use_adx and not np.isnan(adx_val):
                    short_ok = short_ok and adx_val > adx_thresh
                
                if short_ok and ind["ma7"][i] < ind["ma25"][i] and rsi_val > 55 and macd_val < 0 and vol_ratio > vol_mult:
                    size_ratio = 0.2
                    if dynamic_size and not np.isnan(vol_pct) and vol_pct > 0:
                        size_ratio = min(0.3, 0.1 / (vol_pct * vol_size_div))
                    
                    size = (cash * size_ratio) / px
                    cash -= size * px * 0.0005
                    pos = -size
                    entry_px = px
                    entry_atr = a
                    held = 0
                    side = "short"
                    trail_low = px
        
        # === 平仓 ===
        elif pos > 0 and held >= min_bars:
            if ind["ma7"][i] < ind["ma25"][i] or ind["rsi"][i] > rsi_hi:
                pnl = (px - entry_px) * pos
                cash += pos * px * 0.9995
                trades.append(("long_exit", pnl))
                pos, held, side, trail_high = 0, 0, None, 0
        
        elif pos < 0 and held >= min_bars:
            if ind["ma7"][i] > ind["ma25"][i] or ind["rsi"][i] < rsi_lo:
                sz = abs(pos)
                pnl = (entry_px - px) * sz
                cash += pnl - sz * px * 0.0005
                trades.append(("short_exit", pnl))
                pos, held, side, trail_low = 0, 0, None, 999999
        
        # 权益
        eq_val = cash
        if pos > 0:
            eq_val = cash + pos * px
        elif pos < 0:
            eq_val = cash + (entry_px - px) * abs(pos)
        eq.append(eq_val)
    
    if not eq:
        return {"ret": 0, "dd": 0, "sharpe": 0, "trades": 0, "win": 0, "pf": 0}
    
    eq_arr = np.array(eq)
    peak = np.maximum.accumulate(eq_arr)
    dd = ((eq_arr - peak) / np.maximum(peak, 1e-6)).min() * 100
    final = eq_arr[-1]
    ret = (final - 10000) / 10000 * 100
    
    rets = np.diff(eq_arr) / np.maximum(eq_arr[:-1], 1e-6)
    sharpe = (np.mean(rets) / np.std(rets)) * np.sqrt(365 * 96) if len(rets) > 1 and np.std(rets) > 0 else 0
    
    wins = sum(1 for t in trades if t[1] > 0)
    tot = len(trades)
    win_rate = wins / max(tot, 1) * 100
    
    pos_pnl = [t[1] for t in trades if t[1] > 0]
    neg_pnl = [abs(t[1]) for t in trades if t[1] <= 0]
    pf = (np.mean(pos_pnl) / np.mean(neg_pnl)) if wins > 0 and len(neg_pnl) > 0 and np.mean(neg_pnl) > 0 else 0
    
    return {"ret": round(ret, 2), "dd": round(dd, 2), "sharpe": round(sharpe, 2),
            "trades": tot, "win": round(win_rate, 1), "pf": round(pf, 2)}


if __name__ == "__main__":
    df = pd.read_csv("data/klines/BTC_USDT_15m.csv")
    print(f"数据: {len(df)} 行")
    
    t0 = time.time()
    ind, c, o, h, l, v = compute_indicators(df)
    print(f"指标计算: {time.time()-t0:.1f}s")
    
    # === 扩展搜索: V4 ===
    combos = []
    
    # 基础参数
    for rsi_lo, rsi_hi in [(35, 55), (40, 60), (35, 60)]:
        for atr_m in [1.5, 2.0, 2.5]:
            for mb in [2, 4, 6]:
                for vol_m in [1.0, 1.2, 1.5]:
                    # 基础 V3
                    combos.append({
                        "name": f"V3-{rsi_lo}/{rsi_hi}-A{atr_m}-H{mb}-V{vol_m}",
                        "rsi_lo": rsi_lo, "rsi_hi": rsi_hi,
                        "atr_mult": atr_m, "min_bars": mb, "vol_mult": vol_m,
                        "allow_short": True,
                    })
    
    # V4 改进
    for base in [
        {"rsi_lo": 35, "rsi_hi": 55, "atr_mult": 2.0, "min_bars": 6, "vol_mult": 1.0},
        {"rsi_lo": 40, "rsi_hi": 60, "atr_mult": 2.0, "min_bars": 4, "vol_mult": 1.2},
        {"rsi_lo": 35, "rsi_hi": 60, "atr_mult": 1.5, "min_bars": 2, "vol_mult": 1.5},
    ]:
        for adx_flag in [False, True]:
            for trail_flag in [False, True]:
                for dyn_size in [False, True]:
                    name = f"V4"
                    if adx_flag: name += "+ADX"
                    if trail_flag: name += "+Trail"
                    if dyn_size: name += "+Dyn"
                    
                    p = dict(base)
                    p["name"] = name
                    p["allow_short"] = True
                    p["use_adx"] = adx_flag
                    p["adx_thresh"] = 20
                    p["use_trail"] = trail_flag
                    p["trail_atr"] = 1.0
                    p["dynamic_size"] = dyn_size
                    p["vol_size_div"] = 2.0
                    combos.append(p)
    
    print(f"组合数: {len(combos)}")
    print(f"\n正在搜索...")
    
    t0 = time.time()
    results = []
    for i, params in enumerate(combos):
        r = run_strategy(df, ind, c, **params)
        r["name"] = params["name"]
        r["rsi_lo"] = params.get("rsi_lo", 0)
        r["rsi_hi"] = params.get("rsi_hi", 0)
        r["atr_m"] = params.get("atr_mult", 0)
        r["min_b"] = params.get("min_bars", 0)
        r["vol_m"] = params.get("vol_mult", 0)
        results.append(r)
    
    results.sort(key=lambda x: x["ret"], reverse=True)
    elapsed = time.time() - t0
    
    print(f"\n{'排名':<4} {'策略':<20} {'收益':>8} {'回撤':>8} {'夏普':>7} {'交易':>5} {'胜率':>7} {'盈亏比':>7}")
    print("-"*80)
    for i, x in enumerate(results[:20]):
        print(f"{i+1:<4} {x['name']:<20} {x['ret']:>+7.2f}% {x['dd']:>+7.2f}% {x['sharpe']:>6.2f} {x['trades']:>5} {x['win']:>5.1f}% {x['pf']:>6.2f}")
    
    print(f"\n{'排名':<4} {'策略':<20} {'收益':>8} {'回撤':>8} {'夏普':>7} {'交易':>5} {'胜率':>7} {'盈亏比':>7}")
    print("-"*80)
    for i, x in enumerate(results[-5:]):
        print(f"{len(results)-4+i:<4} {x['name']:<20} {x['ret']:>+7.2f}% {x['dd']:>+7.2f}% {x['sharpe']:>6.2f} {x['trades']:>5} {x['win']:>5.1f}% {x['pf']:>6.2f}")
    
    print(f"\n⏱ 耗时: {elapsed:.1f}s ({len(combos)}组)")
    
    best = results[0]
    print(f"\n🏆 最佳: {best['name']}")
    print(f"   收益: {best['ret']:+.2f}% | 回撤: {best['dd']:+.2f}% | 夏普: {best['sharpe']:.2f} | 交易: {best['trades']} | 胜率: {best['win']:.1f}%")