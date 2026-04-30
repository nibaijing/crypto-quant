import pandas as pd, numpy as np

df = pd.read_csv('data/klines/BTC_USDT_15m.csv')
closes = df['close'].values
highs = df['high'].values
lows = df['low'].values
n = len(closes)

ma25 = np.array([np.mean(closes[max(0,i-24):i+1]) for i in range(n)])
ma99 = np.array([np.mean(closes[max(0,i-98):i+1]) for i in range(n)])

rsi = np.full(n, np.nan)
for i in range(14, n):
    d = np.diff(closes[i-14:i+1])
    gain = np.mean(d[d>0]) if any(d>0) else 0
    loss = -np.mean(d[d<0]) if any(d<0) else 0
    rsi[i] = 100 - 100/(1 + gain/loss) if loss > 0 else 100

bear_bars = (closes < ma99 * 0.98)
neutral_bars = (closes >= ma99 * 0.98) & (closes <= ma99 * 1.02)
bull_bars = (closes > ma99 * 1.02)

print(f"总K线: {n}")
print(f"熊市: {bear_bars.sum()} ({bear_bars.sum()/n*100:.1f}%)")
print(f"震荡: {neutral_bars.sum()} ({neutral_bars.sum()/n*100:.1f}%)")
print(f"牛市: {bull_bars.sum()} ({bull_bars.sum()/n*100:.1f}%)")

above_ma25 = closes > ma25
above_ma25_bear = above_ma25 & bear_bars
print(f"\n价格>MA25: {above_ma25.sum()} ({above_ma25.sum()/n*100:.1f}%)")
print(f"熊市中价格>MA25: {above_ma25_bear.sum()} ({above_ma25_bear.sum()/bear_bars.sum()*100:.1f}% of bear)")

short_cond = above_ma25_bear & (rsi > 50)
print(f"\n做空条件满足 (熊市+>MA25+RSI>50): {short_cond[99:].sum()} 次")

closes_v = closes[99:]
ma99_v = ma99[99:]
print(f"\nBTC: {closes_v.min():.0f} ~ {closes_v.max():.0f}")
print(f"起始: {closes_v[0]:.0f} -> 结束: {closes_v[-1]:.0f}")
print(f"总跌幅: {(closes_v[-1]/closes_v[0]-1)*100:.1f}%")

print("\n=== 做空条件分析 ===")
for rsi_thresh in [40, 45, 50, 55]:
    cnt = (above_ma25_bear & (rsi > rsi_thresh))[99:].sum()
    print(f"  price>MA25 + RSI>{rsi_thresh}: {cnt}次")

# 不用>MA25，直接用更敏感的MA
for ma in [7, 12, 25, 50]:
    ma_val = np.array([np.mean(closes[max(0,i-(ma-1)):i+1]) for i in range(n)])
    cond = bear_bars & (closes > ma_val) & (rsi > 45)
    print(f"  熊市+price>MA{ma}+RSI>45: {cond[99:].sum()}次")

# 最简单的做空：只要RSI反弹到高点就做空
print("\n=== 最简做空条件 ===")
for rsi_th in [55, 60, 65, 70]:
    cnt = (bear_bars & (rsi > rsi_th))[99:].sum()
    print(f"  熊市+RSI>{rsi_th}: {cnt}次")