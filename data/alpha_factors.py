#!/usr/bin/env python3
"""
Alpha 因子集 — 从 Qlib Alpha158 移植到加密货币 15 分钟 K 线

核心理念:
  不是用 ML 替代 MATrend，而是让因子工程 + 未来 LightGBM 与 MATrend 双确认。

因子类别 (共 44 个因子):
  1. K线形态 (Kbar)    — 8 个: KMID, KLEN, KSFT, KUP, KUP2, KLOW, KLOW2, KAMP
  2. 价格动量 (Mom)     — 10 个: ROC5/10/20/30/60, MA5/10/20/30/60
  3. 波动率 (Vol)       — 8 个: STD5/10/20/30/60, RESI5/10/20
  4. 量价关系 (VW)      — 6 个: CORR5/10/20, CORD5/10/20
  5. RSI家族 (RSI)      — 4 个: SUMP, SUMN, SUMD, VSUMP
  6. 价格位置 (Pos)     — 6 个: MAX5/20/60, MIN5/20/60
  7. 趋势强度 (Trend)   — 2 个: CNTP, CNTN

窗口设计 (15分钟K线):
  5  = 1.25h   (短期动量)
  10 = 2.5h
  20 = 5h      (日内)
  30 = 7.5h    (半日)
  60 = 15h     (日间)
  120 = 30h    (跨日)

用法:
  from data.alpha_factors import AlphaFactors
  af = AlphaFactors()
  df_with_factors = af.compute(df)        # 输入: OHLCV DataFrame, 输出: 附加因子列的 DataFrame
  df_with_factors = af.compute_incremental(df, new_row_dict)  # 增量更新 (实时模式下追加单根K线后调用)
"""

from __future__ import annotations

import numpy as np
from typing import Optional, Dict


class AlphaFactors:
    """加密货币 Alpha 因子集 — 纯 NumPy 实现，零外部依赖"""

    # 滚动窗口（15分钟K线基准）
    WINDOWS = [5, 10, 20, 30, 60, 120]
    # 量价相关窗口
    CORR_WINDOWS = [5, 10, 20]
    # RSI 窗口
    RSI_WINDOW = 14
    # 趋势窗口
    TREND_WINDOW = 15

    def compute(self, df, inplace=True):
        """批量计算全部 44 个 Alpha 因子。

        Parameters
        ----------
        df : pd.DataFrame with columns: open, high, low, close, volume
        inplace : bool

        Returns
        -------
        pd.DataFrame with 44 additional factor columns prepended with 'alpha_'.
        """
        import pandas as pd

        if not inplace:
            df = df.copy()

        if len(df) < 120:
            return df

        closes = df["close"].values.astype(np.float64)
        opens = df["open"].values.astype(np.float64)
        highs = df["high"].values.astype(np.float64)
        lows = df["low"].values.astype(np.float64)
        volumes = df["volume"].values.astype(np.float64)

        # ── 1. K线形态 (8 个因子) ──────────────────────────────────────
        df["alpha_KMID"] = (closes - opens) / np.maximum(opens, 1e-12)
        df["alpha_KLEN"] = (highs - lows) / np.maximum(opens, 1e-12)
        df["alpha_KSFT"] = (2 * closes - highs - lows) / np.maximum(highs - lows, 1e-12)
        df["alpha_KUP"] = (highs - np.maximum(opens, closes)) / np.maximum(opens, 1e-12)
        df["alpha_KUP2"] = (highs - np.maximum(opens, closes)) / np.maximum(highs - lows + 1e-12, 1e-12)
        df["alpha_KLOW"] = (np.minimum(opens, closes) - lows) / np.maximum(opens, 1e-12)
        df["alpha_KLOW2"] = (np.minimum(opens, closes) - lows) / np.maximum(highs - lows + 1e-12, 1e-12)
        df["alpha_KAMP"] = (highs - lows) / np.maximum(closes, 1e-12)

        # ── 2. 价格动量 (10 个因子) ─────────────────────────────────────
        for w in self.WINDOWS:
            # ROC: Rate of Change
            df[f"alpha_ROC{w}"] = (closes / np.maximum(self._shift(closes, w), 1e-12)) - 1
            # MA: 移动均线比值 (close / MA_w - 1)
            ma = self._rolling_mean(closes, w)
            df[f"alpha_MA{w}"] = closes / np.maximum(ma, 1e-12) - 1

        # ── 3. 波动率 (8 个因子) ───────────────────────────────────────
        returns = np.diff(closes, prepend=closes[0]) / np.maximum(
            self._shift(closes, 1), 1e-12
        )
        for w in self.WINDOWS[:5]:  # 5,10,20,30,60
            df[f"alpha_STD{w}"] = self._rolling_std(returns, w)
            # RESI: 收益率的标准化残差 (当前收益率 vs 最近 N 期均值)
            ret_ma = self._rolling_mean(returns, w)
            ret_std = self._rolling_std(returns, w)
            df[f"alpha_RESI{w}"] = np.where(
                ret_std > 1e-12, (returns - ret_ma) / (ret_std + 1e-12), 0
            )

        # ── 4. 量价关系 (6 个因子) ─────────────────────────────────────
        for w in self.CORR_WINDOWS:
            # CORR: 价格收益率与成交量的滚动相关性
            vol_change = np.diff(volumes, prepend=volumes[0]) / np.maximum(
                self._shift(volumes, 1), 1e-12
            )
            df[f"alpha_CORR{w}"] = self._rolling_corr(returns, vol_change, w)
            # CORD: 涨跌天数与涨跌量相关性
            up_mask = (returns > 0).astype(np.float64)
            df[f"alpha_CORD{w}"] = self._rolling_corr(up_mask, vol_change, w)

        # ── 5. RSI 家族 (4 个因子) ─────────────────────────────────────
        delta = np.diff(closes, prepend=closes[0])
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        avg_gain = self._rolling_mean(gain, self.RSI_WINDOW)
        avg_loss = self._rolling_mean(loss, self.RSI_WINDOW)

        # SUMP: RSI 正半部分
        df["alpha_SUMP"] = avg_gain / np.maximum(avg_gain + avg_loss, 1e-12)
        # SUMN: RSI 负半部分
        df["alpha_SUMN"] = avg_loss / np.maximum(avg_gain + avg_loss, 1e-12)
        # SUMD: RSI 归一化差值
        rs = np.where(avg_loss > 1e-12, avg_gain / avg_loss, avg_gain / 1e-12)
        df["alpha_SUMD"] = rs / (1 + rs)
        # VSUMP: 成交量加权的 SUMP
        vol_delta = self._rolling_mean(volumes * gain, self.RSI_WINDOW)
        vol_total = vol_delta + self._rolling_mean(volumes * loss, self.RSI_WINDOW)
        df["alpha_VSUMP"] = np.where(
            vol_total > 1e-12, vol_delta / vol_total, 0.5
        )

        # ── 6. 价格位置 (6 个因子) ─────────────────────────────────────
        for w in [5, 20, 60]:
            roll_max = self._rolling_max(highs, w)
            roll_min = self._rolling_min(lows, w)
            rng = roll_max - roll_min
            # 当前价格在 N 期内的相对位置 (0=最低, 1=最高)
            df[f"alpha_POS{w}"] = np.where(
                rng > 1e-12, (closes - roll_min) / rng, 0.5
            )
            # 当前位置的 z-score
            roll_mean = self._rolling_mean(closes, w)
            roll_std = self._rolling_std(closes, w)
            df[f"alpha_POSZ{w}"] = np.where(
                roll_std > 1e-12, (closes - roll_mean) / roll_std, 0
            )

        # ── 7. 趋势强度 (2 个因子) ─────────────────────────────────────
        df["alpha_CNTP"] = self._rolling_mean(
            (returns > 0).astype(np.float64), self.TREND_WINDOW
        )
        df["alpha_CNTN"] = self._rolling_mean(
            (returns < 0).astype(np.float64), self.TREND_WINDOW
        )

        # ── 8. 振幅 (1 个因子) ─────────────────────────────────────────
        df["alpha_HLAMP"] = (highs - lows) / np.maximum(closes, 1e-12)

        # ── 清理初始 NaN ───────────────────────────────────────────────
        # 不 fillna — 消费方自行处理 NaN

        return df

    def compute_incremental(self, df, new_row: dict = None):
        """增量更新：重算最后几行的因子值。

        调用方已用 df.loc[len(df)] = new_row 追加了新行，这里只做因子重算。

        Parameters
        ----------
        df : pd.DataFrame — 已追加新行的完整 DataFrame
        new_row : dict — 未使用（调用方已追加）

        Returns
        -------
        pd.DataFrame with factors updated in last rows.
        """
        import pandas as pd

        new_idx = len(df) - 1  # 行已由调用方追加

        # 只重算最后 max_window 行
        max_window = max(self.WINDOWS) + 20
        start_idx = max(0, new_idx - max_window)
        slice_df = df.iloc[start_idx : new_idx + 1]

        # 重新计算这个切片的所有因子
        result = self.compute(slice_df, inplace=False)

        # 写回原 df 的最后部分
        for col in result.columns:
            if col.startswith("alpha_"):
                df.loc[result.index, col] = result[col]

        return df

    # ── NumPy 纯函数工具 ─────────────────────────────────────────────

    @staticmethod
    def _rolling_mean(arr: np.ndarray, window: int) -> np.ndarray:
        """滚动均值（前置 NaN）"""
        n = len(arr)
        out = np.full(n, np.nan, dtype=np.float64)
        if n < window:
            return out
        cumsum = np.cumsum(np.insert(arr.astype(np.float64), 0, 0))
        out[window - 1 :] = (cumsum[window:] - cumsum[:-window]) / window
        return out

    @staticmethod
    def _rolling_std(arr: np.ndarray, window: int) -> np.ndarray:
        """滚动标准差（前置 NaN，ddof=1）"""
        n = len(arr)
        out = np.full(n, np.nan, dtype=np.float64)
        if n < window:
            return out
        mean = AlphaFactors._rolling_mean(arr, window)
        for i in range(window - 1, n):
            seg = arr[i - window + 1 : i + 1]
            out[i] = np.std(seg, ddof=1) if len(seg) > 1 else 0
        return out

    @staticmethod
    def _rolling_max(arr: np.ndarray, window: int) -> np.ndarray:
        """滚动最大值"""
        n = len(arr)
        out = np.full(n, np.nan, dtype=np.float64)
        if n < window:
            return out
        for i in range(window - 1, n):
            out[i] = np.max(arr[i - window + 1 : i + 1])
        return out

    @staticmethod
    def _rolling_min(arr: np.ndarray, window: int) -> np.ndarray:
        """滚动最小值"""
        n = len(arr)
        out = np.full(n, np.nan, dtype=np.float64)
        if n < window:
            return out
        for i in range(window - 1, n):
            out[i] = np.min(arr[i - window + 1 : i + 1])
        return out

    @staticmethod
    def _rolling_corr(x: np.ndarray, y: np.ndarray, window: int) -> np.ndarray:
        """滚动 Pearson 相关系数"""
        n = len(x)
        out = np.full(n, np.nan, dtype=np.float64)
        if n < window:
            return out
        for i in range(window - 1, n):
            sx = x[i - window + 1 : i + 1]
            sy = y[i - window + 1 : i + 1]
            if np.std(sx) < 1e-12 or np.std(sy) < 1e-12:
                out[i] = 0
            else:
                out[i] = np.corrcoef(sx, sy)[0, 1]
        return out

    @staticmethod
    def _shift(arr: np.ndarray, n: int) -> np.ndarray:
        """滞后 N 期（前置 NaN）"""
        out = np.full_like(arr, np.nan, dtype=np.float64)
        if n < len(arr):
            out[n:] = arr[:-n]
        return out

    def factor_names(self) -> list[str]:
        """返回全部 Alpha 因子名称列表"""
        return sorted(
            [
                # K线形态
                "alpha_KMID", "alpha_KLEN", "alpha_KSFT",
                "alpha_KUP", "alpha_KUP2", "alpha_KLOW", "alpha_KLOW2", "alpha_KAMP",
                # 动量
                *[f"alpha_ROC{w}" for w in self.WINDOWS],
                *[f"alpha_MA{w}" for w in self.WINDOWS],
                # 波动率
                *[f"alpha_STD{w}" for w in self.WINDOWS[:5]],
                *[f"alpha_RESI{w}" for w in self.WINDOWS[:5]],
                # 量价关系
                *[f"alpha_CORR{w}" for w in self.CORR_WINDOWS],
                *[f"alpha_CORD{w}" for w in self.CORR_WINDOWS],
                # RSI
                "alpha_SUMP", "alpha_SUMN", "alpha_SUMD", "alpha_VSUMP",
                # 价格位置
                "alpha_POS5", "alpha_POS20", "alpha_POS60",
                "alpha_POSZ5", "alpha_POSZ20", "alpha_POSZ60",
                # 趋势
                "alpha_CNTP", "alpha_CNTN",
                # 振幅
                "alpha_HLAMP",
            ]
        )

    def factor_groups(self) -> dict[str, list[str]]:
        """返回按类别分组的因子名"""
        return {
            "Kbar": ["alpha_KMID", "alpha_KLEN", "alpha_KSFT", "alpha_KUP", "alpha_KUP2", "alpha_KLOW", "alpha_KLOW2", "alpha_KAMP"],
            "Momentum": [f"alpha_ROC{w}" for w in self.WINDOWS] + [f"alpha_MA{w}" for w in self.WINDOWS],
            "Volatility": [f"alpha_STD{w}" for w in self.WINDOWS[:5]] + [f"alpha_RESI{w}" for w in self.WINDOWS[:5]],
            "VolumePrice": [f"alpha_CORR{w}" for w in self.CORR_WINDOWS] + [f"alpha_CORD{w}" for w in self.CORR_WINDOWS],
            "RSI": ["alpha_SUMP", "alpha_SUMN", "alpha_SUMD", "alpha_VSUMP"],
            "Position": ["alpha_POS5", "alpha_POS20", "alpha_POS60", "alpha_POSZ5", "alpha_POSZ20", "alpha_POSZ60"],
            "Trend": ["alpha_CNTP", "alpha_CNTN"],
            "Amplitude": ["alpha_HLAMP"],
        }


# ── CLI 测试 ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import pandas as pd
    import json
    from pathlib import Path
    import sys

    # 1. 从模拟盘历史数据加载
    history_file = Path(__file__).parent / "historical_data.json"
    if not history_file.exists():
        print(f"❌ 无历史数据文件: {history_file}")
        print("   请先运行: python3 scripts/fetch_historical.py")
        sys.exit(1)

    data = json.loads(history_file.read_text())
    btc = data["btc_daily"]

    # 日线测试（15m 窗口不适合日线，但算法通用）
    df = pd.DataFrame(btc)
    df = df.rename(columns={
        "open": "open", "high": "high", "low": "low",
        "close": "close", "volume": "volume"
    })
    # 确保数值类型
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    print(f"📊 输入数据: {len(df)} 条日线")

    af = AlphaFactors()
    result = af.compute(df)

    factor_names = af.factor_names()
    valid_cols = [c for c in factor_names if c in result.columns]
    print(f"✅ 计算完成: {len(valid_cols)}/{len(factor_names)} 个因子")

    # 展示最后一行
    last = result.iloc[-1]
    print(f"\n📋 最新因子值 ({result.index[-1]}):")
    for group, names in af.factor_groups().items():
        vals = {n: round(float(last.get(n, np.nan)), 4) for n in names if n in result.columns and not np.isnan(last.get(n, np.nan))}
        if vals:
            print(f"  [{group}]: {vals}")

    # 因子覆盖率统计
    for col in valid_cols:
        nan_ratio = result[col].isna().mean()
        if nan_ratio > 0.5:
            print(f"  ⚠️ {col}: NaN 率 {nan_ratio:.0%} (可能是窗口过大导致)")