import pandas as pd, numpy as np, time

df = pd.read_csv('data/klines/BTC_USDT_15m.csv')
closes = df['close'].values
highs = df['high'].values
lows = df['low'].values
n = len(closes)

# 预计算
t0 = time.time()
ma7 = np.array([np.mean(closes[max(0,i-6):i+1]) for i in range(n)])
ma25 = np.array([np.mean(closes[max(0,i-24):i+1]) for i in range(n)])
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

def run_short_backtest(rsi_entry, rsi_exit, atr_m, position_mult):
    cash = 10000.0
    init = cash
    pos = 0.0  # negative = short
    entry_price = 0.0
    entry_atr = 0.0
    bars_held = 0
    trades = 0
    wins = 0

    for i in range(30, n):
        c = closes[i]
        r = rsi[i]
        a = atr[i]
        m7 = ma7[i]
        m25 = ma25[i]

        if np.isnan(r) or np.isnan(a):
            continue

        equity = cash
        if pos < 0:
            pnl_unreal = (entry_price - c) * abs(pos)
            equity = cash + pnl_unreal

        # ATR止损
        if pos < 0 and entry_price > 0 and entry_atr > 0:
            if c >= entry_price + entry_atr * atr_m:
                pnl = (entry_price - c) * abs(pos)
                cash += pnl
                trades += 1
                if pnl > 0: wins += 1
                pos = 0
                continue

        if pos < 0:
            bars_held += 1

        # 做空: MA7 < MA25 (趋势向下) + RSI > entry阈值
        if pos == 0 and m7 < m25 and r > rsi_entry:
            size = (cash * 0.95) / c * position_mult
            pos = -size
            entry_price = c
            entry_atr = a
            bars_held = 0

        # 平空: RSI超卖 或 MA金叉
        elif pos < 0 and bars_held >= 2 and (r < rsi_exit or m7 > m25):
            pnl = (entry_price - c) * abs(pos)
            cash += pnl
            trades += 1
            if pnl > 0: wins += 1
            pos = 0

    # 最终清算
    if pos < 0:
        pnl = (entry_price - closes[-1]) * abs(pos)
        cash += pnl
        trades += 1
        if pnl > 0: wins += 1

    ret_pct = (cash - init) / init * 100
    win_rate = wins / max(trades, 1) * 100
    return ret_pct, trades, win_rate, cash

# 基线
print("\n基线 V5: MA7<MA25 + RSI>45")
ret, tr, wr, eq = run_short_backtest(45, 25, 1.5, 1.0)
print(f"  收益={ret:+.2f}%  交易={tr}  胜率={wr:.1f}%  最终={eq:.0f}")

# 网格搜索
print("\n" + "="*55)
print("网格搜索 V5")
print("="*55)
results = []
best_ret = -9999
best = None

for rsi_ent in [40, 42, 45, 48, 50]:
    for rsi_ext in [20, 25, 30, 35]:
        for atr_m in [1.0, 1.5, 2.0, 2.5]:
            for pos_m in [0.5, 1.0, 1.5, 2.0]:
                ret, tr, wr, eq = run_short_backtest(rsi_ent, rsi_ext, atr_m, pos_m)
                results.append((ret, tr, wr, rsi_ent, rsi_ext, atr_m, pos_m))
                if ret > best_ret:
                    best_ret = ret
                    best = (ret, tr, wr, rsi_ent, rsi_ext, atr_m, pos_m)

results.sort(key=lambda x: x[0], reverse=True)
print(f"{'排':<3} {'RSI入':<6} {'RSI出':<6} {'ATR':<6} {'仓位':<6} {'收益':>8} {'交易':>5} {'胜率':>7}")
print('-'*60)
for i, (ret, tr, wr, re, rx, am, pm) in enumerate(results[:15]):
    print(f"{i+1:<3} {re:<6} {rx:<6} {am:<6.1f} {pm:<6.1f} {ret:>+7.2f}% {tr:>5} {wr:>5.1f}%")

print(f"\n🏆 最佳: {best[0]:+.2f}%  交易={best[1]}  胜率={best[2]:.1f}%")
print(f"   RSI入={best[3]} RSI出={best[4]} ATR={best[5]} 仓位={best[6]}x")