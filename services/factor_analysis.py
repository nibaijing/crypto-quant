#!/usr/bin/env python3
"""CryptoQuant 因子分析模块 — 从 Vibe-Trading 移植并适配加密货币

核心功能:
  1. IC (Information Coefficient) — Spearman Rank 相关性检验
     - 因子值 vs 未来 N 日收益率
     - 输出: IC Mean, IC Std, IR (Information Ratio), IC>0 比例
  2. 分层回测 (Layered Backtest)
     - 按因子值分为 N 组 (默认 5 组, quantile-based)
     - 每组等权持有，计算累计净值曲线
     - 输出: 各组的 final_equity, long-short spread

支持的因子:
  - fear_greed_value: 恐惧贪婪指数 (0-100)
  - fear_greed_signal: 恐惧贪婪信号 (-1 bearish, 0 neutral, +1 bullish)
  - news_sentiment_net: 新闻情绪净值 (bullish_count - bearish_count)
  - news_sentiment_confidence: 新闻情绪置信度 (0.0-1.0)

用法:
  python3 services/factor_analysis.py           # 运行全部因子检验
  python3 services/factor_analysis.py --json    # JSON 输出
  python3 services/factor_analysis.py --daily   # 日常增量更新 (只跑今天)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# scipy 不可用 — 手写 Spearman rank correlation
def _spearmanr(x: np.ndarray, y: np.ndarray) -> float:
    """Pure-numpy Spearman rank correlation coefficient."""
    n = len(x)
    if n < 2 or np.std(x) == 0 or np.std(y) == 0:
        return 0.0
    # Assign ranks (average method for ties)
    from collections import Counter
    def rankdata(a):
        # Ties get average rank
        ranks = np.empty(n, dtype=float)
        order = np.argsort(a)
        sorted_a = a[order]
        i = 0
        while i < n:
            j = i
            while j < n and sorted_a[j] == sorted_a[i]:
                j += 1
            avg_rank = (i + j + 1) / 2.0  # 1-indexed average
            for k in range(i, j):
                ranks[order[k]] = avg_rank
            i = j
        return ranks
    rx = rankdata(x)
    ry = rankdata(y)
    diff = rx - ry
    rho = 1 - (6 * np.sum(diff**2)) / (n * (n**2 - 1))
    return float(rho)

PROJECT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT / "data"
OUTPUT_DIR = DATA_DIR / "factor_reports"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
HISTORICAL = DATA_DIR / "historical_data.json"

# ── Data I/O ──────────────────────────────────────────────────────────────────

def load_historical() -> pd.DataFrame:
    """加载历史数据并合并 BTC + FG 为统一 DataFrame。

    Returns
    -------
    DataFrame indexed by date (datetime), columns:
        btc_close, btc_return_1d, btc_return_3d, btc_return_5d,
        fg_value, fg_signal
    """
    if not HISTORICAL.exists():
        raise FileNotFoundError(f"{HISTORICAL} 不存在，先运行 scripts/fetch_historical.py")

    raw = json.loads(HISTORICAL.read_text())

    # BTC 日线 → DataFrame
    btc_df = pd.DataFrame(raw["btc_daily"])
    btc_df["date"] = pd.to_datetime(btc_df["close_time"], unit="ms").dt.date
    btc_df = btc_df.set_index("date").sort_index()
    btc_df = btc_df[["close"]].rename(columns={"close": "btc_close"})

    # 计算未来收益率 (forward return)
    for horizon, label in [(1, "btc_return_1d"), (3, "btc_return_3d"), (5, "btc_return_5d")]:
        btc_df[label] = btc_df["btc_close"].pct_change(horizon).shift(-horizon)

    # Fear & Greed history → DataFrame
    fg_df = pd.DataFrame(raw["fear_greed_history"])
    fg_df["date"] = pd.to_datetime(fg_df["timestamp"], unit="s").dt.date
    fg_df = fg_df.set_index("date").sort_index()
    fg_df = fg_df[["value"]].rename(columns={"value": "fg_value"})

    # 因子信号: bearish=-1, neutral=0, bullish=+1
    fg_df["fg_signal"] = 0
    fg_df.loc[fg_df["fg_value"] <= 45, "fg_signal"] = -1
    fg_df.loc[fg_df["fg_value"] > 55, "fg_signal"] = 1
    fg_df.loc[fg_df["fg_value"] <= 25, "fg_signal"] = -1  # extreme fear 强化
    fg_df.loc[fg_df["fg_value"] > 75, "fg_signal"] = 1   # extreme greed 强化

    # Merge
    merged = btc_df.join(fg_df, how="inner").dropna()
    return merged


def load_news_sentiment_history() -> pd.DataFrame | None:
    """尝试加载已缓存的历史新闻情绪数据。"""
    news_cache = DATA_DIR / "news_sentiment_history.json"
    if not news_cache.exists():
        return None
    try:
        raw = json.loads(news_cache.read_text())
        df = pd.DataFrame(raw)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.date
            df = df.set_index("date").sort_index()
        return df
    except Exception:
        return None


# ── IC Computation ───────────────────────────────────────────────────────────

def compute_ic(
    factor: pd.Series,
    forward_return: pd.Series,
    *,
    label: str = "factor",
    min_samples: int = 30,
) -> dict[str, Any]:
    """计算 Spearman Rank IC (Information Coefficient)。

    IC = corr(factor_t, return_{t+horizon}) for each cross-section.
    对于时序因子（如恐惧贪婪），这里是每天一个值，所以是时序 IC。

    Parameters
    ----------
    factor : pd.Series (index=date)
    forward_return : pd.Series (index=date, 未来收益率)
    label : 因子名称
    min_samples : 最少样本数

    Returns
    -------
    dict with ic_mean, ic_std, ir, ic_positive_ratio, ic_series
    """
    common = factor.dropna().index.intersection(forward_return.dropna().index)
    if len(common) < min_samples:
        return {
            "status": "insufficient_data",
            "label": label,
            "samples": len(common),
            "error": f"需要至少 {min_samples} 个样本，当前 {len(common)}",
        }

    f = factor.loc[common]
    r = forward_return.loc[common]

    # 时序 IC: 每个时间点只有一个因子值，无法算截面相关性
    # 改用滚动窗口 IC（rolling 30-day correlation）
    combined = pd.DataFrame({"factor": f, "forward_return": r}).dropna()
    if len(combined) < min_samples:
        return {
            "status": "insufficient_data",
            "label": label,
            "samples": len(combined),
            "error": f"合并后不足 {min_samples} 样本",
        }

    # Rolling Spearman: 30d 窗口
    window = min(30, len(combined) // 3)
    if window < 5:
        window = 5

    ic_values = []
    for i in range(window, len(combined)):
        slice_f = combined["factor"].iloc[i-window:i]
        slice_r = combined["forward_return"].iloc[i-window:i]
        if slice_f.nunique() < 2 or slice_r.nunique() < 2:
            continue
        corr = _spearmanr(slice_f.values, slice_r.values)
        if not np.isnan(corr):
            ic_values.append(corr)

    if not ic_values:
        return {
            "status": "no_variance",
            "label": label,
            "samples": len(combined),
            "error": "因子或无收益率方差为零",
        }

    ic_array = np.array(ic_values)
    ic_mean = float(np.mean(ic_array))
    ic_std = float(np.std(ic_array, ddof=1))
    ir = ic_mean / ic_std if ic_std > 0 else 0.0
    ic_positive_ratio = float(np.mean(ic_array > 0))

    # Statistical significance: t-test on IC mean
    t_stat = ic_mean / (ic_std / np.sqrt(len(ic_array))) if ic_std > 0 else 0.0

    return {
        "status": "ok",
        "label": label,
        "samples": len(combined),
        "rolling_window": window,
        "ic_count": len(ic_array),
        "ic_mean": round(ic_mean, 4),
        "ic_std": round(ic_std, 4),
        "ir": round(ir, 4),
        "ic_positive_ratio": round(ic_positive_ratio, 4),
        "t_stat": round(t_stat, 4),
        "significant": abs(t_stat) > 1.96,  # 95% confidence
        "interpretation": _interpret_ic(ic_mean, ir, ic_positive_ratio),
    }


def _interpret_ic(ic_mean: float, ir: float, ic_positive_ratio: float) -> str:
    """用中文解释 IC 结果。"""
    if abs(ic_mean) < 0.02:
        return "几乎无预测力，建议弃用"
    if ic_mean > 0.05 and ic_positive_ratio > 0.55 and ir > 0.3:
        return "显著正向预测力，强烈建议纳入策略"
    if ic_mean > 0.03 and ic_positive_ratio > 0.5:
        return "有正向预测力，建议纳入作为辅助信号"
    if ic_mean < -0.03 and ic_positive_ratio < 0.5:
        return "反向预测力（可作为反向指标），谨慎使用"
    return "预测力不足，建议进一步验证或弃用"


# ── Layered Backtest ─────────────────────────────────────────────────────────

def layered_backtest(
    factor: pd.Series,
    forward_return: pd.Series,
    *,
    n_groups: int = 5,
    label: str = "factor",
    min_samples: int = 30,
) -> dict[str, Any]:
    """分层回测：按因子值分组，等权持有，计算累计净值。

    因子值最高的组 (Group_5) 通常应该是表现最好的组。
    """
    common = factor.dropna().index.intersection(forward_return.dropna().index)
    if len(common) < min_samples:
        return {
            "status": "insufficient_data",
            "label": label,
            "error": f"样本不足: {len(common)} < {min_samples}",
        }

    f = factor.loc[common]
    r = forward_return.loc[common]

    # 按因子值排序 → 分 N 组
    ranked = f.rank(method="first")
    try:
        bins = pd.qcut(ranked, n_groups, labels=False, duplicates="drop")
    except ValueError:
        bins = pd.cut(ranked, n_groups, labels=False)

    groups = {}
    for g in sorted(set(bins)):
        mask = bins == g
        if mask.sum() == 0:
            continue
        group_return = r[mask].mean()
        groups[f"Group_{g+1}"] = {
            "mean_return": round(float(group_return), 6),
            "count": int(mask.sum()),
            "mean_factor": round(float(f[mask].mean()), 2),
        }

    if len(groups) < 2:
        return {"status": "no_variance", "label": label, "error": "无法形成有效分组"}

    # Long-short spread (Group_N - Group_1)
    top = groups[f"Group_{len(groups)}"]["mean_return"]
    bottom = groups["Group_1"]["mean_return"]
    spread = round(top - bottom, 6)

    # 正收益率比例 (各组有多少比例的天数是正收益)
    for g_key in groups:
        g_idx = int(g_key.split("_")[-1]) - 1
        mask = bins == g_idx
        if mask.sum() > 0:
            pos_ratio = float((r[mask] > 0).mean())
            groups[g_key]["positive_day_ratio"] = round(pos_ratio, 4)

    # Monotonicity check: 高因子组应该高收益
    returns = [groups[g]["mean_return"] for g in sorted(groups.keys())]
    monotonic = all(returns[i] <= returns[i+1] for i in range(len(returns)-1))

    return {
        "status": "ok",
        "label": label,
        "n_groups": len(groups),
        "groups": groups,
        "long_short_spread": spread,
        "monotonic": monotonic,
        "interpretation": (
            f"高因子组年化超额 {spread * 365 * 100:.1f}%，"
            + ("分层单调。" if monotonic else "分层不单调，信号质量存疑。")
        ),
    }


# ── Factor Score Card ────────────────────────────────────────────────────────

def run_all_factors(
    df: pd.DataFrame | None = None,
    *,
    horizons: list[int] | None = None,
) -> dict[str, Any]:
    """运行全部因子检验。

    Parameters
    ----------
    df : DataFrame, 可选。不传则自动加载。
    horizons : 预测周期列表，默认 [1, 3, 5] 天

    Returns
    -------
    dict with keys for each factor-horizon combination
    """
    if df is None:
        df = load_historical()
    if horizons is None:
        horizons = [1, 3, 5]

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_range": {
            "start": str(df.index.min()),
            "end": str(df.index.max()),
        },
        "samples": len(df),
    }

    # Factor 1: Fear & Greed value
    if "fg_value" in df.columns:
        for h in horizons:
            ret_col = f"btc_return_{h}d"
            if ret_col not in df.columns:
                continue
            key = f"fg_value_{h}d"
            results[key] = {
                "ic": compute_ic(df["fg_value"], df[ret_col], label=f"FG值 → {h}d收益"),
                "layered": layered_backtest(df["fg_value"], df[ret_col], label=f"FG值分层 → {h}d收益"),
            }
            # Composite score
            ic = results[key]["ic"]
            layer = results[key]["layered"]
            results[key]["composite_score"] = _composite_score(ic, layer)

    # Factor 2: Fear & Greed signal (-1/0/+1)
    if "fg_signal" in df.columns:
        for h in horizons:
            ret_col = f"btc_return_{h}d"
            if ret_col not in df.columns:
                continue
            key = f"fg_signal_{h}d"
            results[key] = {
                "ic": compute_ic(df["fg_signal"], df[ret_col], label=f"FG信号 → {h}d收益"),
                "layered": layered_backtest(df["fg_signal"], df[ret_col], label=f"FG信号分层 → {h}d收益"),
            }
            results[key]["composite_score"] = _composite_score(results[key]["ic"], results[key]["layered"])

    # Factor 3: News sentiment (if available)
    news_df = load_news_sentiment_history()
    if news_df is not None and not news_df.empty:
        news_merged = df.join(news_df, how="inner")
        if "news_net" in news_merged.columns and len(news_merged) > 20:
            for h in horizons:
                ret_col = f"btc_return_{h}d"
                key = f"news_net_{h}d"
                results[key] = {
                    "ic": compute_ic(news_merged["news_net"], news_merged[ret_col], label=f"新闻情绪净值 → {h}d收益"),
                    "layered": layered_backtest(news_merged["news_net"], news_merged[ret_col], label=f"新闻情绪分层 → {h}d收益"),
                }
                results[key]["composite_score"] = _composite_score(results[key]["ic"], results[key]["layered"])

    # Summary: best factor
    results["summary"] = _build_summary(results, horizons)
    return results


def _composite_score(ic: dict, layered: dict) -> float:
    """综合打分 (0-100): IC + 分层单调性"""
    score = 50.0
    if ic.get("status") == "ok":
        # IC contribution (0-30)
        ic_abs = abs(ic.get("ic_mean", 0))
        ic_score = min(30, ic_abs * 400)  # 0.05 → 20, 0.075 → 30
        if ic.get("significant"):
            ic_score *= 1.3
        score += ic_score

        # IR contribution (0-20)
        ir = abs(ic.get("ir", 0))
        score += min(20, ir * 40)  # 0.5 → 20

    if layered.get("status") == "ok":
        # Monotonicity bonus
        if layered.get("monotonic"):
            score += 15
        # Spread bonus (0-10)
        spread_abs = abs(layered.get("long_short_spread", 0))
        score += min(10, spread_abs * 15000)  # 0.0005 → 7.5

    return round(min(100, score), 1)


def _build_summary(results: dict[str, Any], horizons: list[int]) -> dict[str, Any]:
    """生成因子总结"""
    best_score = 0
    best_factor = None
    top_factors = []

    for key, value in results.items():
        if not isinstance(value, dict) or "composite_score" not in value:
            continue
        score = value["composite_score"]
        ic_info = value.get("ic", {})
        top_factors.append({
            "factor": key,
            "score": score,
            "ic_mean": ic_info.get("ic_mean"),
            "significant": ic_info.get("significant"),
        })
        if score > best_score:
            best_score = score
            best_factor = key

    top_factors.sort(key=lambda x: x["score"], reverse=True)

    return {
        "best_factor": best_factor,
        "best_score": best_score,
        "usable_factors": [f for f in top_factors if f["score"] >= 55],
        "all_factors": top_factors,
    }


# ── Strategy Integration ─────────────────────────────────────────────────────

def get_active_factor_bias() -> dict[str, Any]:
    """获取当前应该激活的因子偏向，供 strategy_evolver 使用。

    Returns
    -------
    dict with:
        bias: "long_bias" | "short_bias" | "neutral"
        confidence: 0.0-1.0
        active_factors: list of factor names that are significant
    """
    # Load latest factor report
    reports = sorted(OUTPUT_DIR.glob("factor_report_*.json"), reverse=True)
    if not reports:
        return {"bias": "neutral", "confidence": 0.0, "active_factors": []}

    try:
        report = json.loads(reports[0].read_text())
    except Exception:
        return {"bias": "neutral", "confidence": 0.0, "active_factors": []}

    summary = report.get("summary", {})
    usable = summary.get("usable_factors", [])

    if not usable:
        return {"bias": "neutral", "confidence": 0.0, "active_factors": []}

    # Determine bias from the best factor's IC sign
    best_name = summary.get("best_factor", "")
    best_data = report.get(best_name, {})
    ic = best_data.get("ic", {})
    ic_mean = ic.get("ic_mean", 0)
    confidence = min(1.0, abs(ic_mean) * 10)  # IC 0.05 → 0.5, 0.1 → 1.0

    if ic_mean > 0.03:
        bias = "long_bias"
    elif ic_mean < -0.03:
        bias = "short_bias"
    else:
        bias = "neutral"

    return {
        "bias": bias,
        "confidence": round(confidence, 2),
        "active_factors": [f["factor"] for f in usable],
        "source_report": str(reports[0]),
    }


# ── Alpha Factor Testing ─────────────────────────────────────────────────────

def run_alpha_factors(
    df: pd.DataFrame | None = None,
    *,
    horizons: list[int] | None = None,
    max_factors: int = 15,
) -> dict[str, Any]:
    """检验 Alpha 因子集的预测力（IC + 分层回测）。

    只检验 top N 最可能显著的因子，避免报告爆炸。

    Parameters
    ----------
    df : DataFrame — 必须包含 alpha_* 列（通过 AlphaFactors.compute() 产出）
    horizons : 预测周期，默认 [1, 3]
    max_factors : 最多检验因子数

    Returns
    -------
    dict — 每个因子的 IC + 分层回测结果，含 summary ranking
    """
    from data.alpha_factors import AlphaFactors

    if df is None:
        df = load_historical()
    if horizons is None:
        horizons = [1, 3]

    af = AlphaFactors()
    df = af.compute(df)

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "factor_type": "Alpha因子集",
        "total_factors": len(af.factor_names()),
        "tested_factors": 0,
    }

    # 只检验覆盖率高的因子（NaN 率 < 50%）
    factor_names = [f for f in af.factor_names() if f in df.columns and df[f].notna().mean() > 0.5]

    # 按最近一段时间的方差排序取 top N（方差太小的因子没意义）
    recent = df.iloc[-100:] if len(df) > 100 else df
    factor_vars = {}
    for f in factor_names:
        std = recent[f].std()
        if not np.isnan(std) and std > 1e-8:
            factor_vars[f] = std

    top_factors = sorted(factor_vars, key=factor_vars.get, reverse=True)[:max_factors]
    results["tested_factors"] = len(top_factors)

    all_scores = []
    for factor_name in top_factors:
        for h in horizons:
            ret_col = f"btc_return_{h}d"
            if ret_col not in df.columns:
                continue
            key = f"alpha_{factor_name}_{h}d"
            ic_result = compute_ic(
                df[factor_name], df[ret_col],
                label=f"{factor_name} → {h}d收益",
            )
            layer_result = layered_backtest(
                df[factor_name], df[ret_col],
                label=f"{factor_name}分层 → {h}d收益",
            )
            composite = _composite_score(ic_result, layer_result)
            results[key] = {
                "ic": ic_result,
                "layered": layer_result,
                "composite_score": composite,
            }
            if ic_result.get("significant"):
                all_scores.append({
                    "factor": key,
                    "score": composite,
                    "ic_mean": ic_result.get("ic_mean"),
                    "significant": ic_result.get("significant"),
                })

    all_scores.sort(key=lambda x: x["score"], reverse=True)
    best = all_scores[0] if all_scores else {"factor": "none", "score": 0}

    results["summary"] = {
        "best_factor": best["factor"],
        "best_score": best["score"],
        "usable_factors": [f for f in all_scores if f["score"] >= 55][:10],
        "all_factors": all_scores[:20],
    }
    return results


def run_all_factors_full(df: pd.DataFrame | None = None, horizons: list[int] | None = None) -> dict[str, Any]:
    """运行全部因子检验：外部信号 + Alpha 因子集，合并报告。"""
    if horizons is None:
        horizons = [1, 3]

    # 基础因子（FG + 新闻情绪）
    base_report = run_all_factors(df, horizons=horizons)
    # Alpha 因子
    alpha_report = run_alpha_factors(df, horizons=horizons)

    # 合并 summary
    merged = dict(base_report)
    merged["alpha_factors"] = {
        k: v for k, v in alpha_report.items()
        if k not in ("generated_at", "factor_type", "total_factors", "tested_factors")
    }
    merged["alpha_summary"] = alpha_report.get("summary", {})

    # 全局 best factor
    best_base = base_report.get("summary", {}).get("best_score", 0)
    best_alpha = alpha_report.get("summary", {}).get("best_score", 0)
    merged["_generated_by"] = "run_all_factors_full (基础 + Alpha)"

    return merged


# ── Report Formatting ────────────────────────────────────────────────────────

def format_markdown(report: dict) -> str:
    """Markdown 格式化"""
    lines = [
        "🔬 **因子分析报告**",
        f"📅 {report['generated_at'][:10]} | 样本: {report['samples']}天",
        f"📆 区间: {report['data_range']['start']} ~ {report['data_range']['end']}",
        "",
        "━━━━━━━━━━━━━━━",
        "📊 **因子检验结果**",
        "",
    ]

    for key, value in sorted(report.items()):
        if not isinstance(value, dict) or "composite_score" not in value:
            continue
        ic = value.get("ic", {})
        layer = value.get("layered", {})
        score = value["composite_score"]

        sig_mark = "✅" if ic.get("significant") else "⚠️"
        lines.append(f"{sig_mark} **{key}** — 评分 {score}/100")
        if ic.get("status") == "ok":
            lines.append(f"   IC={ic.get('ic_mean', '?'):.4f}  IR={ic.get('ir', '?'):.4f}  +率={ic.get('ic_positive_ratio', 0):.0%}")
            lines.append(f"   {ic.get('interpretation', '')}")
        if layer.get("status") == "ok":
            mono = "✅单调" if layer.get("monotonic") else "⚠️不单调"
            sp = layer.get("long_short_spread", 0)
            lines.append(f"   分层({layer.get('n_groups', '?')}组): {mono}  多空利差={sp:.4%}")
            for g, gd in layer.get("groups", {}).items():
                lines.append(f"     {g}: avg_ret={gd['mean_return']:.4%}  n={gd['count']}  factor_avg={gd['mean_factor']}")
        lines.append("")

    # Summary
    summary = report.get("summary", {})
    lines.append("━━━━━━━━━━━━━━━")
    lines.append("🏆 **策略建议**")
    lines.append(f"• 最优因子: {summary.get('best_factor', 'N/A')} (评分 {summary.get('best_score', 0)})")
    usable = summary.get("usable_factors", [])
    if usable:
        lines.append(f"• 可用因子: {', '.join(f['factor'] for f in usable[:3])}")
    else:
        lines.append("• 当前无显著可用因子，策略维持中性")
    lines.append(f"• 综合 bias: {get_active_factor_bias()['bias']} (置信度 {get_active_factor_bias()['confidence']})")

    lines.append("")
    lines.append(f"🤖 CryptoQuant Factor Analysis · {report['generated_at'][:19]}")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    json_mode = "--json" in sys.argv

    df = load_historical()
    report = run_all_factors(df, horizons=[1, 3, 5])

    # Save report
    report_path = OUTPUT_DIR / f"factor_report_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    if json_mode:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_markdown(report))