#!/usr/bin/env python3
"""CryptoQuant 行为诊断模块 — 从 Vibe-Trading 移植

四项核心诊断:
  1. Disposition Effect  — 亏损单持有时间 vs 盈利单
  2. Overtrading         — 高频交易日 PnL vs 低频
  3. Chasing Momentum    — 追涨买入比例
  4. Anchoring           — 同品种交易价格变异系数

纯算术实现，无外部数据依赖（Chasing 需要 price history，降级为仅依赖日志）
"""

from __future__ import annotations

from datetime import datetime


def _safe_div(a: float, b: float) -> float:
    return float(a) / float(b) if b else 0.0


def _severity(score: float, med_cutoff: float, high_cutoff: float) -> str:
    if score >= high_cutoff:
        return "high"
    if score >= med_cutoff:
        return "medium"
    return "low"


# --------------- 1. Disposition Effect ---------------

def diagnose_disposition(roundtrips: list[dict]) -> dict:
    """检测处置效应：亏损单持有时间是否远长于盈利单。

    Metric = avg_loss_hold / avg_win_hold.
    > 1: 持有亏损单更久 → disposition bias.
    """
    if not roundtrips:
        return {"severity": "low", "evidence": "没有已平仓记录"}

    wins = [r for r in roundtrips if r.get("pnl", 0) > 0]
    losses = [r for r in roundtrips if r.get("pnl", 0) < 0]

    if not wins or not losses:
        return {"severity": "low", "evidence": "赢单或亏单不足，无法比较持有时长"}

    def _hold_hours(r: dict) -> float:
        """计算持有小时数"""
        if r.get("entry_time") and r.get("exit_time"):
            try:
                t0 = datetime.fromisoformat(r["entry_time"])
                t1 = datetime.fromisoformat(r["exit_time"])
                return (t1 - t0).total_seconds() / 3600.0
            except (ValueError, TypeError):
                pass
        return 0

    win_hold = sum(_hold_hours(w) for w in wins) / len(wins)
    loss_hold = sum(_hold_hours(l) for l in losses) / len(losses)
    ratio = _safe_div(loss_hold, win_hold)
    sev = _severity(ratio, 1.2, 1.5)

    evidence = (
        f"亏损单平均持有 {loss_hold:.1f}h，盈利单 {win_hold:.1f}h（比值 {ratio:.2f}）。"
        + ("典型的处置效应——亏了不肯走。" if sev == "high"
           else "轻微的持有亏损单偏久倾向。" if sev == "medium"
           else "持有时长基本对称，无处置效应。")
    )

    return {
        "severity": sev,
        "ratio_loss_to_win_hold": round(ratio, 2),
        "avg_winner_hold_hours": round(win_hold, 1),
        "avg_loser_hold_hours": round(loss_hold, 1),
        "evidence": evidence,
        "action": (
            "启用硬止损（如 ATR 1.5x），强制平掉亏损单"
            if sev == "high" else "在策略中加入持仓时长限制（max 48h）"
            if sev == "medium" else None
        ),
    }


# --------------- 2. Overtrading ---------------

def diagnose_overtrading(roundtrips: list[dict]) -> dict:
    """检测过度交易：高频交易日的平均 PnL 是否远低于低频日。

    按每天 roundtrip 数量分为高频组（top 25%）和低频组（bottom 25%），
    比较两组平均 PnL。
    """
    if len(roundtrips) < 8:
        return {"severity": "low", "evidence": "交易样本不足（<8笔），无法评估"}

    # 按天聚合
    daily: dict[str, list[float]] = {}
    for r in roundtrips:
        if r.get("pnl") is None:
            continue
        day = (r.get("exit_time") or r.get("entry_time", ""))[:10]
        daily.setdefault(day, []).append(r["pnl"])

    if len(daily) < 4:
        return {"severity": "low", "evidence": "交易日不足4天"}

    daily_counts = {d: len(pnls) for d, pnls in daily.items()}
    sorted_counts = sorted(daily_counts.values())

    busy_cutoff = sorted_counts[int(len(sorted_counts) * 0.75)]
    quiet_cutoff = sorted_counts[int(len(sorted_counts) * 0.25)]

    busy_pnls = []
    quiet_pnls = []
    for d, pnls in daily.items():
        cnt = daily_counts[d]
        if cnt >= busy_cutoff:
            busy_pnls.extend(pnls)
        if cnt <= quiet_cutoff:
            quiet_pnls.extend(pnls)

    if not busy_pnls or not quiet_pnls:
        return {"severity": "low", "evidence": "高频/低频组分布不均"}

    busy_avg = sum(busy_pnls) / len(busy_pnls)
    quiet_avg = sum(quiet_pnls) / len(quiet_pnls)

    gap = quiet_avg - busy_avg
    base = abs(quiet_avg) if quiet_avg != 0 else 1.0
    sev = _severity(gap / base, 0.3, 1.0) if busy_avg < quiet_avg else "low"

    evidence = (
        f"高频日（≥{busy_cutoff}笔）均PnL {busy_avg:+.1f}，"
        f"低频日（≤{quiet_cutoff}笔）均PnL {quiet_avg:+.1f}。"
        + ("高频交易显著拖累收益。" if sev == "high"
           else "高频日有一定拖累。" if sev == "medium"
           else "频率对收益无显著影响。")
    )

    return {
        "severity": sev,
        "busy_day_avg_pnl": round(busy_avg, 2),
        "quiet_day_avg_pnl": round(quiet_avg, 2),
        "busy_day_trade_count": busy_cutoff,
        "gap": round(gap, 2),
        "evidence": evidence,
        "action": (
            "每天最多开仓 2 次，减少不必要的交易"
            if sev == "high" else "适当降低交易频率，专注高置信信号"
            if sev == "medium" else None
        ),
    }


# --------------- 3. Chasing Momentum ---------------

def diagnose_chasing(roundtrips: list[dict]) -> dict:
    """检测追涨杀跌：连续同向操作是否在高位/低位追入。

    降级版（无外部价格数据）：检查同一方向的连续交易，
    如果入场价每次都在抬高（做多）或降低（做空），标记为追涨。
    """
    if len(roundtrips) < 5:
        return {"severity": "low", "evidence": "样本不足（<5笔）"}

    # 按方向分组
    longs = [r for r in roundtrips if r.get("direction") == "LONG"]
    shorts = [r for r in roundtrips if r.get("direction") == "SHORT"]

    chased = 0
    total = 0

    def _count_sequence(trades: list[dict], direction: str) -> tuple[int, int]:
        """统计连续同向入场价趋势"""
        if len(trades) < 3:
            return 0, 0
        chased_seq = 0
        for i in range(1, len(trades)):
            ep = trades[i].get("entry_price")
            ep_prev = trades[i-1].get("entry_price")
            if ep is None or ep_prev is None:
                continue
            pct = (ep - ep_prev) / ep_prev
            if direction == "LONG" and pct > 0.03:   # 做多入场价涨了 3% → 追涨
                chased_seq += 1
            elif direction == "SHORT" and pct < -0.03:  # 做空入场价跌了 3% → 追跌
                chased_seq += 1
        return chased_seq, max(len(trades) - 1, 0)

    l_chased, l_total = _count_sequence(longs, "LONG")
    s_chased, s_total = _count_sequence(shorts, "SHORT")
    chased = l_chased + s_chased
    total = l_total + s_total

    if total == 0:
        return {"severity": "low", "evidence": "连续同向交易不足"}

    ratio = chased / total
    sev = _severity(ratio, 0.4, 0.6)

    evidence = (
        f"{chased}/{total} 次连续同向入场（{ratio:.0%}）价格移动 >3%。"
        + ("强烈的追涨/追跌倾向。" if sev == "high"
           else "有一定追涨倾向。" if sev == "medium"
           else "无明显追涨行为。")
    )

    return {
        "severity": sev,
        "chase_ratio": round(ratio, 3),
        "chased_count": chased,
        "total_sequences": total,
        "evidence": evidence,
        "action": (
            "等待回调再入场，不要追突破"
            if sev == "high" else "增加入场确认指标（如 RSI < 30 才做多）"
            if sev == "medium" else None
        ),
    }


# --------------- 4. Anchoring ---------------

def diagnose_anchoring(roundtrips: list[dict]) -> dict:
    """检测价格锚定：同一方向的交易价格是否集中在窄区间内。

    对每个方向，计算入场价的标准差/均值（CV）。
    CV < 0.05 说明在固定价格区间反复交易 → 锚定效应。
    """
    longs = [r.get("entry_price") for r in roundtrips
             if r.get("direction") == "LONG" and r.get("entry_price")]
    shorts = [r.get("entry_price") for r in roundtrips
              if r.get("direction") == "SHORT" and r.get("entry_price")]

    results = []
    for name, prices in [("LONG", longs), ("SHORT", shorts)]:
        if len(prices) < 3:
            continue
        avg = sum(prices) / len(prices)
        if avg == 0:
            continue
        variance = sum((p - avg) ** 2 for p in prices) / len(prices)
        std = variance ** 0.5
        cv = std / avg
        results.append({
            "direction": name,
            "count": len(prices),
            "avg_price": round(avg, 2),
            "std": round(std, 2),
            "cv": round(cv, 4),
            "range": f"${min(prices):.0f} ~ ${max(prices):.0f}",
        })

    if not results:
        return {"severity": "low", "evidence": "每个方向交易次数不足（<3笔）"}

    anchored = [r for r in results if r["cv"] < 0.05]
    ratio = len(anchored) / len(results)
    sev = _severity(ratio, 0.33, 0.66)

    details = "; ".join(
        f"{r['direction']}: CV={r['cv']:.4f}, 区间={r['range']}"
        for r in anchored
    ) if anchored else "无"

    evidence = (
        f"{len(anchored)}/{len(results)} 个方向交易价格 CV<5%。{details}。"
        + ("强烈的价格锚定——在固定价格反复交易。" if sev == "high"
           else "部分方向存在锚定。" if sev == "medium"
           else "价格分布自然，无锚定效应。")
    )

    return {
        "severity": sev,
        "anchored_ratio": round(ratio, 3),
        "details": results,
        "anchored_count": len(anchored),
        "evidence": evidence,
        "action": (
            "不要重复在同一价位开仓，等待趋势确认后入场"
            if sev == "high" else "扩大入场价位的接受范围，避免卡死在某价位"
            if sev == "medium" else None
        ),
    }


# --------------- 5. 综合诊断入口 ---------------

def run_full_diagnosis(roundtrips: list[dict]) -> dict:
    """运行全部 4 项行为诊断"""
    return {
        "disposition_effect": diagnose_disposition(roundtrips),
        "overtrading": diagnose_overtrading(roundtrips),
        "chasing_momentum": diagnose_chasing(roundtrips),
        "anchoring": diagnose_anchoring(roundtrips),
    }


# --------------- 6. Markdown 格式化 ---------------

def format_behavior_markdown(behavior: dict) -> str:
    """格式化为 Telegram Markdown"""
    lines = ["━━━━━━━━━━━━━━━", "🧠 **行为诊断**", ""]
    labels = {
        "disposition_effect": "📌 处置效应",
        "overtrading": "📌 过度交易",
        "chasing_momentum": "📌 追涨杀跌",
        "anchoring": "📌 价格锚定",
    }
    icons = {"high": "🔴", "medium": "🟡", "low": "🟢"}

    for key, label in labels.items():
        d = behavior.get(key, {})
        sev = d.get("severity", "low")
        lines.append(f"{icons.get(sev, '⚪')} {label}: **{sev.upper()}**")
        lines.append(f"   {d.get('evidence', 'N/A')}")
        if d.get("action"):
            lines.append(f"   ⏭ {d['action']}")
        lines.append("")

    return "\n".join(lines)