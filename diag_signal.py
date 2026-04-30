#!/usr/bin/env python3
"""诊断脚本：拉最新K线，打印所有指标，看为什么没有信号"""
import sys, json, requests, time
sys.path.insert(0, '/home/ni/crypto_quant')
import pandas as pd, numpy as np
from strategies.spot.optimized_v6 import OptimizedStrategy

# 拉200根15m K线
resp = requests.get("https://api.binance.com/api/v3/klines", params={
    "symbol": "BTCUSDT", "interval": "15m", "limit": 200
}, timeout=10)
data = resp.json()

df = pd.DataFrame([{
    "timestamp": d[0], "open": float(d[1]), "high": float(d[2]),
    "low": float(d[3]), "close": float(d[4]), "volume": float(d[5])
} for d in data])
df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")

strat = OptimizedStrategy()
df = strat.compute_indicators(df)

# 最后5根
cols = ["datetime", "close", "ma_7", "ma_25", "ma_99", "rsi", "adx", "macd_hist"]
print("=== Last 5 bars ===")
print(df[cols].iloc[-5:].to_string())
print()

# 最近10根K线的信号
print("=== Last 10 bar signals ===")
for i in range(max(len(df)-10, 99), len(df)):
    row = df.iloc[i]
    bar = {
        "close": float(row["close"]), "high": float(row["high"]),
        "low": float(row["low"]), "history": df.iloc[:i+1].copy(),
        "index": i, "position": None
    }
    s = strat.on_bar(bar)
    ts = pd.to_datetime(row["timestamp"], unit="ms")
    print(f"  {ts} | C=${row['close']:,.0f} | RSI={row['rsi']:.1f} | ADX={row['adx']:.1f} | MACDH={row['macd_hist']:.1f} | SIG={s}")

print()

# 最新一根详情
row = df.iloc[-1]
idx = len(df) - 1
bar = {
    "close": float(row["close"]), "high": float(row["high"]),
    "low": float(row["low"]), "history": df,
    "index": idx, "position": None
}
signal = strat.on_bar(bar)
c = float(row["close"])
ma99 = float(row.get("ma_99", c))
m7 = float(row.get("ma_7", c))
m25 = float(row.get("ma_25", c))
rsi = float(row.get("rsi", 50))
adx = float(row.get("adx", 0))
macdh = float(row.get("macd_hist", 0))
dev = c/ma99 - 1

print(f"=== LATEST BAR ===")
print(f"Signal: {signal}")
print(f"Price: ${c:,.0f} | Dev from MA99: {dev*100:+.2f}%")
print(f"MA7: {m7:,.0f} | MA25: {m25:,.0f} | MA99: {ma99:,.0f}")
print(f"MA7>MA25: {m7>m25} | MA7<MA25: {m7<m25}")
print(f"RSI: {rsi:.1f} | ADX: {adx:.1f} (>20: {adx>20})")
print(f"MACD_hist: {macdh:.1f}")
print()
short = (m7 < m25 and macdh < -15 and adx > 20 and dev <= 0.02 and rsi > 40)
long_cond = (m7 > m25 and macdh > 15 and adx > 20 and dev >= -0.02 and rsi < 60)
print(f"SHORT cond: M7<M25={m7<m25} MACDH<-15={macdh<-15} ADX>20={adx>20} dev<2%={dev<=0.02} RSI>40={rsi>40} => {'YES' if short else 'NO'}")
print(f"LONG  cond: M7>M25={m7>m25} MACDH>15={macdh>15} ADX>20={adx>20} dev>-2%={dev>=-0.02} RSI<60={rsi<60} => {'YES' if long_cond else 'NO'}")