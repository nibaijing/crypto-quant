"""V5 Dual 向量化回测"""
import pandas as pd, numpy as np, time

df = pd.read_csv('data/klines/BTC_USDT_15m.csv')
closes = df['close'].values
highs = df['high'].values
lows = df['low'].values
n = len(closes)

# 预计算指标
t0 = time.time()
ma7 = np.array([np.mean(closes[max(0,i-6):i+1]) for i in range(n)])
ma25 = np.array([np.mean(closes[max(0,i-24):i+1]) for i in range(n)])
ma99 = np.array([np.mean(closes[max(0,i-98):i+1]) for i in range(n)])

rsi = np.full(n, np.nan)
for i in range(14, n):
    d = np.diff(closes[i-14:i+1])
    gain = np.mean(d[d>0]) if any(d>0) else 0
    loss = -np.mean(d[d<0]) if any(d<0) else 0
    rsi[i] = 100 - 100/(1 + gain/loss) if loss > 0 else 100

atr = np.full(n, np.nan)
for i in range(14, n):
    tr = [max(highs[j]-lows[j], abs(highs[j]-closes[j-1]), abs(lows[j]-closes[j-1]))
          for j in range(i-13, i+1)]
    atr[i] = np.mean(tr)
print(f"预计算: {time.time()-t0:.2f}s")


def run_dual(long_mult, short_mult):
    """多空双杀回测"""
    cash = 10000.0
    init = cash
    pos = 0.0       # + = long, - = short
    entry_price = 0.0
    entry_atr = 0.0
    bars_held = 0
    trades = 0
    wins = 0
    pnls = []
    side_counts = {"long": 0, "short": 0}
    side_wins = {"long": 0, "short": 0}

    for i in range(99, n):
        c = closes[i]
        r = rsi[i]
        a = atr[i]
        m7 = ma7[i]
        m25 = ma25[i]
        m99 = ma99[i]

        if np.isnan(r) or np.isnan(a):
            continue

        # 市场状态
        dev = c / max(m99, 1) - 1
        regime = "bull" if dev > 0.02 else ("bear" if dev < -0.02 else "neutral")

        # ATR止损
        if pos != 0 and entry_price > 0 and entry_atr > 0:
            stopped = False
            if pos > 0 and c <= entry_price - entry_atr * 2.0:
                pnl = (c - entry_price) * abs(pos)
                cash += pnl; pnls.append(pnl); trades += 1
                if pnl > 0: wins += 1; side_wins["long"] += 1
                side_counts["long"] += 1; stopped = True
            elif pos < 0 and c >= entry_price + entry_atr * 2.5:
                pnl = (entry_price - c) * abs(pos)
                cash += pnl; pnls.append(pnl); trades += 1
                if pnl > 0: wins += 1; side_wins["short"] += 1
                side_counts["short"] += 1; stopped = True
            if stopped:
                pos = 0; continue

        if pos != 0:
            bars_held += 1

        # ============ 做空 ============
        if pos == 0 and m7 < m25 and r > 40:
            size = (cash * 0.95) / c * short_mult
            pos = -size
            entry_price = c; entry_atr = a; bars_held = 0

        elif pos < 0 and bars_held >= 2 and (r < 30 or m7 > m25):
            pnl = (entry_price - c) * abs(pos)
            cash += pnl; pnls.append(pnl); trades += 1
            if pnl > 0: wins += 1; side_wins["short"] += 1
            side_counts["short"] += 1
            pos = 0

        # ============ 做多 (非熊市) ============
        if pos == 0 and m7 > m25 and r < 40 and regime in ("bull", "neutral"):
            size = (cash * 0.95) / c * long_mult
            pos = size
            entry_price = c; entry_atr = a; bars_held = 0

        elif pos > 0 and bars_held >= 2 and (r > 65 or m7 < m25):
            pnl = (c - entry_price) * abs(pos)
            cash += pnl; pnls.append(pnl); trades += 1
            if pnl > 0: wins += 1; side_wins["long"] += 1
            side_counts["long"] += 1
            pos = 0

    # 最终清算
    if pos != 0:
        pnl = (closes[-1] - entry_price) * abs(pos) if pos > 0 else (entry_price - closes[-1]) * abs(pos)
        cash += pnl; pnls.append(pnl); trades += 1
        if pnl > 0: wins += 1

    ret_pct = (cash - init) / init * 100
    win_rate = wins / max(trades, 1) * 100
    avg_pnl = np.mean(pnls) if pnls else 0
    return ret_pct, trades, win_rate, avg_pnl, cash, side_counts, side_wins


# 基线: 做多1x, 做空2x
print("\n=== Dual V5 基线 ===")
ret, tr, wr, ap, eq, sc, sw = run_dual(1.0, 2.0)
print(f"收益={ret:+.2f}% | 交易={tr} | 胜率={wr:.1f}% | 均盈亏=${ap:+.1f} | 最终=${eq:.0f}")
print(f"多仓: {sc['long']}次/{sw['long']}胜 | 空仓: {sc['short']}次/{sw['short']}胜")

# 网格搜索仓位倍数
print("\n=== 网格搜索 (仓位倍数) ===")
results = []
best = None
best_ret = -9999

for lm in [0.5, 1.0, 1.5, 2.0]:
    for sm in [1.0, 1.5, 2.0, 2.5, 3.0]:
        ret, tr, wr, ap, eq, sc, sw = run_dual(lm, sm)
        results.append((ret, tr, wr, lm, sm, sc['long'], sw['long'], sc['short'], sw['short']))
        if ret > best_ret:
            best_ret = ret
            best = (ret, tr, wr, lm, sm, sc, sw)

results.sort(key=lambda x: x[0], reverse=True)
print(f"{'排':<3} {'多倍':<5} {'空倍':<5} {'收益':>8} {'交易':>5} {'胜率':>7} {'多仓':>5} {'多胜':>5} {'空仓':>5} {'空胜':>5}")
print('-'*75)
for i, (ret, tr, wr, lm, sm, lc, lw, sc, sw) in enumerate(results[:15]):
    print(f"{i+1:<3} {lm:<5.1f} {sm:<5.1f} {ret:>+7.2f}% {tr:>5} {wr:>5.1f}% {lc:>5} {lw:>5} {sc:>5} {sw:>5}")

print(f"\n🏆 最佳: {best[0]:+.2f}% 多={best[3]}x 空={best[4]}x")
print(f"   多仓={best[5]['long']}次/{best[6]['long']}胜 空仓={best[5]['short']}次/{best[6]['short']}胜")

# 纯做空 (V5) 对比
ret_s, tr_s, wr_s, ap_s, eq_s, sc_s, sw_s = run_dual(0.001, 2.0)  # long几乎禁用
print(f"\n纯做空对比: {ret_s:+.2f}% 交易={tr_s} 胜率={wr_s:.1f}%")