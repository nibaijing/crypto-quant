#!/usr/bin/env python3
"""
提取 15 分钟 K 线历史数据用于 LightGBM 训练

用法:
  python3 scripts/extract_15m_data.py              # 拉取最近 30 天 (~2880 根) 并保存
  python3 scripts/extract_15m_data.py --days 60    # 拉取 60 天
  python3 scripts/extract_15m_data.py --file data/training/btc_15m.csv  # 指定输出路径
"""

import sys, json, time, requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import numpy as np

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from data.alpha_factors import AlphaFactors

DEFAULT_DAYS = 30
DEFAULT_OUTPUT = PROJECT / "data" / "training" / "btc_15m.parquet"


def fetch_klines_paginated(days: int, max_retries: int = 3) -> pd.DataFrame:
    """分页拉取 Binance 15m K 线。

    每页 1000 根 (~10.4 天)，按需循环拉取直到覆盖目标天数。
    """
    url = "https://api.binance.com/api/v3/klines"
    end_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    all_rows = []
    current_end = end_time_ms
    page = 0

    while current_end > start_time_ms and page < 20:
        params = {
            "symbol": "BTCUSDT",
            "interval": "15m",
            "limit": 1000,
            "endTime": current_end,
        }
        for attempt in range(max_retries):
            try:
                resp = requests.get(url, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                if not data:
                    break
                all_rows.extend(data)
                current_end = data[0][0] - 1
                page += 1
                print(f"  📦 第{page}页: {len(data)} 条, 最新={datetime.utcfromtimestamp(data[0][0]/1000)}")
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"  ⚠️ 重试 {attempt+1}/{max_retries}: {e}")
                    time.sleep(2 ** attempt)
                else:
                    print(f"  ❌ 失败: {e}")
                    return pd.DataFrame()
        time.sleep(0.5)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore",
    ])
    df = df[["open_time", "open", "high", "low", "close", "volume"]]
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col])
    df["open_time"] = pd.to_numeric(df["open_time"])
    df = df.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)
    return df


def add_labels(df: pd.DataFrame, horizons: list[int] = [4, 8, 24]):
    """
    添加预测标签:
      - label_{h}: 未来第 h 根 K 线的收益率 (回归目标)
      - label_dir_{h}: 0/1 方向 (分类目标)
      - label_ma_dir_{h}: 未来 h 根 K 线平均收益率方向 (更稳健的分类目标)
    """
    closes = df["close"].values.astype(np.float64)
    for h in horizons:
        # 回归标签: 未来第 h 根收益率
        fwd_close = np.full_like(closes, np.nan)
        fwd_close[:-h] = closes[h:]
        df[f"label_{h}"] = (fwd_close / closes - 1).astype(np.float32)

        # 方向标签: 涨=1, 跌=0
        df[f"label_dir_{h}"] = ((fwd_close / closes - 1) > 0).astype(np.int8)

        # 平均方向: 未来 h 根的平均价格比当前高?
        fwd_avg = np.full_like(closes, np.nan)
        for k in range(h):
            fwd = np.full_like(closes, np.nan)
            fwd[:-(k+1)] = closes[k+1:]
            fwd_avg = np.where(np.isnan(fwd_avg), fwd, fwd_avg)
            if k > 0:
                fwd_avg = (fwd_avg * k + fwd) / (k + 1)
        df[f"label_ma_dir_{h}"] = ((fwd_avg / closes - 1) > 0).astype(np.int8)

    return df


def main():
    days = DEFAULT_DAYS
    output_path = DEFAULT_OUTPUT

    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            days = int(arg.split("=")[1])
        elif arg.startswith("--file="):
            output_path = Path(arg.split("=")[1])
        elif arg == "--help":
            print(__doc__)
            return

    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"📡 拉取 BTC 15m K 线 — {days} 天")
    start = time.time()
    df = fetch_klines_paginated(days)
    elapsed = time.time() - start

    if df.empty:
        print("❌ 无数据，退出")
        sys.exit(1)

    print(f"✅ 拉取完成: {len(df):,} 条K线 ({elapsed:.1f}s)")
    print(f"   范围: {datetime.utcfromtimestamp(df['open_time'].min()/1000)} ~ {datetime.utcfromtimestamp(df['open_time'].max()/1000)}")

    # 添加标签
    print("🏷️  添加预测标签 (4/8/24 根 K 线)...")
    df = add_labels(df, horizons=[4, 8, 24])

    # 计算 Alpha 因子
    print("🧮 计算 Alpha 因子 (44个)...")
    af = AlphaFactors()
    df = af.compute(df)

    # 统计因子覆盖率
    factor_names = af.factor_names()
    valid = [c for c in factor_names if c in df.columns]
    nan_rates = {c: df[c].isna().mean() for c in valid}
    usable = [c for c in valid if nan_rates[c] < 0.5]
    print(f"   因子数: {len(valid)}, 可用 (>50%覆盖): {len(usable)}")

    # 删除前 120 行（因子需要预热）
    df_clean = df.iloc[120:].reset_index(drop=True)
    print(f"   训练用数据: {len(df_clean):,} 行 (去除前120行预热)")

    # 保存
    if output_path.suffix == ".parquet":
        df_clean.to_parquet(output_path, index=False)
    else:
        df_clean.to_csv(output_path, index=False)

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"\n💾 已保存: {output_path} ({size_mb:.1f} MB)")
    print(f"   列数: {len(df_clean.columns)}")
    print(f"   时间戳列: {datetime.utcfromtimestamp(df_clean['open_time'].min()/1000)} ~ {datetime.utcfromtimestamp(df_clean['open_time'].max()/1000)}")

    # 快速统计
    for h in [4, 8, 24]:
        col = f"label_dir_{h}"
        if col in df_clean.columns:
            pos_ratio = df_clean[col].dropna().mean()
            print(f"   label_dir_{h}: 涨={pos_ratio:.1%}")


if __name__ == "__main__":
    main()