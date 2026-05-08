#!/usr/bin/env python3
"""AI 深度复盘引擎 — 替代 daily_review.py 的模板化分析

职责:
  1. 接收 daily_review.py 输出的 JSON + 市场上下文
  2. 构造结构化 prompt → 调 LLM 做逐笔复盘
  3. 输出: AI 推理结果 (JSON) — 供 cron agent 使用

决策层不在此模块 — 仅生成 prompt 和解析响应。
LLM 调用由 cron agent (Hermes) 执行。

用法:
  python3 ai_review.py --input data/reviews/daily_review_2026-05-06.json
  → 输出 AI prompt 到 stdout (供 agent 消费)

  python3 ai_review.py --input data/reviews/daily_review_2026-05-06.json --raw
  → 输出原始市场数据上下文 (供 agent 嵌入自己的 prompt)
"""

from __future__ import annotations

import json
import sys
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT = Path(__file__).parent
DATA_DIR = PROJECT / "data"

# 因子分析 & 市场上下文
from services.factor_analysis import get_active_factor_bias


def load_review_json(review_path: str) -> dict:
    """加载 daily_review.py 输出的 JSON"""
    return json.loads(Path(review_path).read_text())


def load_market_context() -> dict:
    """加载当前市场上下文 — 供 AI 理解盘面背景"""
    ctx = {}

    # 因子 bias
    try:
        ctx["factor_bias"] = get_active_factor_bias()
    except Exception:
        ctx["factor_bias"] = {"bias": "neutral", "confidence": 0, "active_factors": []}

    # DecisionEngine 状态
    de_state = DATA_DIR / "decision_engine_state.json"
    if de_state.exists():
        try:
            ctx["decision_engine"] = json.loads(de_state.read_text())
        except Exception:
            ctx["decision_engine"] = {}

    # 当前持仓
    state_file = DATA_DIR / "live_futures_state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            ctx["current"] = {
                "cash": state.get("cash", 0),
                "position": state.get("position"),
            }
        except Exception:
            ctx["current"] = {}

    # 最新因子报告 (因子有效性统计)
    factor_reports = sorted(
        (DATA_DIR / "factor_reports").glob("factor_report_*.json"),
        reverse=True,
    )
    if factor_reports:
        try:
            ctx["latest_factor_report"] = json.loads(
                factor_reports[0].read_text()
            ).get("summary", {})
        except Exception:
            ctx["latest_factor_report"] = {}

    return ctx


def build_trade_context(trades: list[dict]) -> str:
    """将交易列表格式化为 AI 可读的上下文"""
    if not trades:
        return "(无已平仓交易)"

    lines = []
    for i, t in enumerate(trades, 1):
        direction = t["direction"]
        entry = t.get("entry_price", 0)
        exit_p = t.get("exit_price")
        pnl = t.get("net_pnl", t.get("pnl", 0))
        lev = t.get("leverage", 10)

        if exit_p:
            pct = (exit_p - entry) / entry * 100
            if direction == "SHORT":
                pct = -pct
            lines.append(
                f"#{i} {direction} {lev}x | "
                f"entry=${entry:,.0f} exit=${exit_p:,.0f} ({pct:+.2f}%) | "
                f"PnL=${pnl:+.2f} | "
                f"{t.get('entry_time','?')} → {t.get('exit_time','?')} | "
                f"result={t['result']}"
            )
        else:
            lines.append(
                f"#{i} {direction} {lev}x | "
                f"entry=${entry:,.0f} | "
                f"HOLDING since {t.get('entry_time','?')}"
            )

    return "\n".join(lines)


def build_review_prompt(review: dict, market_ctx: dict) -> str:
    """构造 AI 深度复盘 prompt"""
    summary = review.get("summary", {})
    direction = review.get("direction", {})
    loss = review.get("loss_analysis", {})
    behavior = review.get("behavior", {})
    trades = review.get("trades", [])
    holding = review.get("holding")

    # 只有已平仓的交易才做复盘
    closed = [t for t in trades if t.get("result") != "holding"]

    parts = []

    # ── 系统角色 ──
    parts.append(
        "You are the AI Trading Review Analyst for a BTC-USDT perpetual futures "
        "quantitative trading system (15-min bars).\n"
        "Your job: analyze yesterday's trades, identify patterns, diagnose what went "
        "right/wrong, and propose SPECIFIC parameter adjustments.\n"
        "Be precise, data-driven, and avoid vague suggestions.\n"
    )

    # ── 市场背景 ──
    fb = market_ctx.get("factor_bias", {})
    parts.append("## Market Context")
    parts.append(f"- Factor bias: {fb.get('bias','neutral')} (confidence: {fb.get('confidence',0)})")
    parts.append(f"- Active factors: {', '.join(fb.get('active_factors', []))}")
    de_state = market_ctx.get("decision_engine", {})
    parts.append(
        f"- DecisionEngine: {de_state.get('total_decisions',0)} total decisions, "
        f"{de_state.get('ai_decisions',0)} AI-assisted, "
        f"{de_state.get('daily_calls',0)} calls today"
    )

    # 当前状态
    cur = market_ctx.get("current", {})
    if cur.get("position"):
        pos = cur["position"]
        parts.append(
            f"- Current position: {pos.get('side','?')} {pos.get('leverage',10)}x "
            f"@ ${pos.get('entry_price',0):,.0f}, "
            f"size={pos.get('size',0):.4f} BTC, margin=${pos.get('margin',0):,.0f}"
        )
    else:
        parts.append("- Current position: None (flat)")

    # ── 交易统计 ──
    parts.append("\n## Trade Summary")
    parts.append(
        f"Total closed: {summary.get('total_trades',0)} | "
        f"Wins: {summary.get('wins',0)} | Win rate: {summary.get('win_rate',0)}%"
    )
    parts.append(
        f"Net PnL: ${summary.get('total_pnl',0):+.2f} | "
        f"Commission: ${summary.get('total_commission',0):.2f} | "
        f"Equity: ${summary.get('current_equity',0):,.2f} ({summary.get('return_pct',0):+.2f}%)"
    )

    # 方向统计
    long_data = direction.get("long", {})
    short_data = direction.get("short", {})
    parts.append(
        f"LONG: {long_data.get('count',0)} trades, PnL=${long_data.get('pnl',0):+.2f} | "
        f"SHORT: {short_data.get('count',0)} trades, PnL=${short_data.get('pnl',0):+.2f}"
    )

    # ── 亏损分析 ──
    if loss:
        parts.append("\n## Loss Analysis")
        parts.append(
            f"Avg loss: ${loss.get('avg_loss',0):.2f} | "
            f"Max loss: ${loss.get('max_loss',0):.2f} | "
            f"Consecutive losses: {loss.get('consecutive_losses',0)}"
        )
        for p in loss.get("patterns", []):
            parts.append(f"- {p}")

    # ── 行为诊断 ──
    if behavior:
        parts.append("\n## Behavioral Diagnosis (pre-computed)")
        for key, label in [
            ("disposition_effect", "Disposition Effect"),
            ("overtrading", "Overtrading"),
            ("chasing_momentum", "Chasing Momentum"),
            ("anchoring", "Anchoring"),
        ]:
            d = behavior.get(key, {})
            if d:
                parts.append(
                    f"- {label}: severity={d.get('severity','?')} | "
                    f"{d.get('evidence','')}"
                )

    # ── 逐笔交易详情 ──
    parts.append("\n## Trade Log (closed trades only)")
    parts.append(build_trade_context(closed))

    # ── 当前策略参数 ──
    from strategy_evolver import load_strategy_params
    params = load_strategy_params()
    parts.append("\n## Current Strategy Parameters")
    for k, v in params.items():
        parts.append(f"- {k}: {v}")

    # ── AI 任务要求 ──
    parts.append("\n## Your Analysis Task")
    parts.append(
        "1. **Trade-by-trade review**: For each closed trade, briefly assess whether "
        "the entry/exit was justified given the market conditions. Which indicators "
        "supported the decision? Were there warning signs?\n"
        "2. **Pattern identification**: What patterns emerge across trades? "
        "(e.g., 'all losses occurred during low-ADX chop zones', "
        "'entries consistently too early on momentum')\n"
        "3. **Parameter impact assessment**: Which current parameters contributed to "
        "losses? Which parameters helped winners? Be specific (e.g., "
        "'ADX_THRESHOLD=35 filtered out X bad signals but also blocked Y good ones')\n"
        "4. **Specific parameter proposals**: Propose 0-3 parameter changes with "
        "exact values, reasoning, and expected impact. Each proposal must include:\n"
        "   - param_name: exact constant name from the list above\n"
        "   - old_value → new_value\n"
        "   - reasoning: why this change (one sentence)\n"
        "   - expected_effect: what should improve\n"
        "   - risk: what could go wrong\n"
        "5. **Fail memory check**: Scan for signals that were repeatedly blocked "
        "by a single condition (e.g., SHORT=83% blocked by RSI 3x consecutively). "
        "If found, include in the response a fail_memory_update object with "
        "pattern and advice fields. This will be injected into the live "
        "DecisionEngine context to warn future AI decisions.\n"
    )

    # ── 反路径依赖警告 ──
    parts.append("\n## ⚠️ Anti-Path-Dependency Rule (CRITICAL)")
    parts.append(
        "You are evaluating a SINGLE day of trading. Your proposals will be applied "
        "to a live trading strategy. DO NOT fall into the trap of:\n"
        "- Assuming yesterday's winning parameters should be pushed further in the same direction\n"
        "- Treating good results as confirmation to relax conditions (this leads to strategy collapse)\n"
        "- Ignoring the trade-off: stricter filters ALWAYS mean fewer trades, less aggressive ALWAYS means lower drawdown\n"
        "\n"
        "GUIDELINES:\n"
        "- Prefer SMALL, REVERSIBLE changes (±1-3 on RSI thresholds, ±3-5 on ADX). Never suggest ±10+ jumps.\n"
        "- If the strategy is profitable, consider NO CHANGES (set no_changes_needed: true). Profitability is NOT a signal to relax.\n"
        "- If losses came from a specific regime (e.g., low-ADX chop), ask whether the fix should be TIGHTER filters, not looser ones.\n"
        "- Propose at most 3 changes. If unsure, propose 1 or 0.\n"
        "- Parameter proposals should CONVERGE on a stable set, not oscillate or trend in one direction.\n"
        "- Ask yourself: 'If this change were applied 10 times in a row, would the strategy still work?' If not, don't propose it.\n"
    )

    parts.append("\n## Response Format (JSON)")
    parts.append(
        "Reply with a JSON object:\n"
        "```json\n"
        "{\n"
        '  "overall_assessment": "one paragraph summary of trading day quality",\n'
        '  "key_findings": ["finding1", "finding2", ...],\n'
        '  "market_regime": "trending_up|trending_down|choppy|ranging",\n'
        '  "trade_reviews": [\n'
        '    {"trade_id": 1, "direction": "LONG", "was_good_trade": true, "notes": "..."},\n'
        '    ...\n'
        '  ],\n'
        '  "behavioral_root_cause": "primary behavioral issue if any, or null",\n'
        '  "parameter_proposals": [\n'
        '    {\n'
        '      "param_name": "ADX_THRESHOLD",\n'
        '      "old_value": 35,\n'
        '      "new_value": 40,\n'
        '      "reasoning": "Choppy market caused 2 false signals...",\n'
        '      "expected_effect": "Fewer false breakouts during ranging periods",\n'
        '      "risk": "May miss early trend entries in fast-moving markets"\n'
        '    }\n'
        '  ],\n'
        '  "confidence_self_assessment": "high|medium|low",\n'
        '  "no_changes_needed": false\n'
        "}\n"
        "```\n"
        "If you think no parameter changes are needed, set no_changes_needed=true "
        "and omit parameter_proposals.\n"
        "Only propose changes you have HIGH confidence in. When unsure, prefer no change."
    )

    return "\n".join(parts)


def build_raw_context(review: dict) -> str:
    """输出原始上下文数据 — 供 cron agent 直接嵌入 prompt"""
    market_ctx = load_market_context()
    return build_review_prompt(review, market_ctx)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI Deep Review Engine")
    parser.add_argument("--input", required=True, help="Path to daily_review JSON")
    parser.add_argument("--raw", action="store_true", help="Output raw context only")
    args = parser.parse_args()

    review = load_review_json(args.input)
    market_ctx = load_market_context()

    if args.raw:
        print(build_review_prompt(review, market_ctx))
    else:
        # Default: output structured review data as JSON for agent consumption
        output = {
            "prompt": build_review_prompt(review, market_ctx),
            "metadata": {
                "date": review.get("date"),
                "generated_at": datetime.now().isoformat(),
                "trades_count": len([t for t in review.get("trades", [])
                                     if t.get("result") != "holding"]),
            },
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))