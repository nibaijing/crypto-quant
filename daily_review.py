#!/usr/bin/env python3
"""CryptoQuant 每日复盘系统

解析 live_trading.log → 开仓/平仓配对 → 统计 → JSON 报告 + Markdown 推送

用法:
  python3 daily_review.py          # Markdown 输出 (供 cron/Telegram)
  python3 daily_review.py --json   # JSON 输出

输出 JSON 文件: data/daily_review_YYYY-MM-DD.json
"""

import json
import re
import sys
from pathlib import Path
from datetime import datetime

# 行为诊断模块
from services.behavior_diagnosis import run_full_diagnosis, format_behavior_markdown
# 因子分析模块
from services.factor_analysis import get_active_factor_bias

PROJECT = Path(__file__).parent
LOG_FILE = PROJECT / "data" / "live_trading.log"
STATE_FILE = PROJECT / "data" / "live_futures_state.json"
OUTPUT_DIR = PROJECT / "data" / "reviews"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def parse_trades(log_path: Path) -> list[dict]:
    """从日志解析所有交易"""
    if not log_path.exists():
        return []

    lines = log_path.read_text(encoding="utf-8").splitlines()
    trades = []

    for line in lines:
        m = re.search(
            r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*合约(做[多空]|平[多空]).*?@ ([\d.]+)',
            line,
        )
        if not m:
            continue

        t = {
            "time": m.group(1),
            "action": m.group(2),
            "price": float(m.group(3)),
        }

        lm = re.search(r'\|\s*([\d.]+)x', line)
        if lm:
            t["leverage"] = float(lm.group(1))

        pm = re.search(r'PnL=\$?([+-]?[\d.]+)', line)
        if pm:
            t["pnl"] = float(pm.group(1))

        cm = re.search(r'手续费=\$?([\d.]+)', line)
        if cm:
            t["commission"] = float(cm.group(1))

        nm = re.search(r'净利=\$?([+-]?[\d.]+)', line)
        if nm:
            t["net_pnl"] = float(nm.group(1))

        # Extract size for commission estimate
        sm = re.search(r'(\d+\.\d+)\s+BTC-USDT', line)
        if sm:
            t["size"] = float(sm.group(1))

        trades.append(t)

    return trades


def pair_trades(trades: list[dict]) -> tuple[list[dict], dict | None]:
    """配对开仓和平仓"""
    opens = [t for t in trades if t["action"] in ("做多", "做空")]
    closes = [t for t in trades if t["action"] in ("平多", "平空")]
    paired = []
    holding = None

    for i, o in enumerate(opens):
        entry = {
            "entry_time": o["time"],
            "direction": "LONG" if o["action"] == "做多" else "SHORT",
            "entry_price": o["price"],
            "leverage": o.get("leverage", 10),
        }
        if i < len(closes):
            c = closes[i]
            entry["exit_time"] = c["time"]
            entry["exit_price"] = c["price"]
            entry["pnl"] = c.get("pnl", 0)
            entry["commission"] = c.get("commission", 0) or 0
            entry["net_pnl"] = c.get("net_pnl", entry["pnl"])
            entry["result"] = "win" if entry["net_pnl"] > 0 else "loss"
        else:
            entry["exit_time"] = None
            entry["exit_price"] = None
            entry["pnl"] = None
            entry["commission"] = None
            entry["net_pnl"] = None
            entry["result"] = "holding"
            holding = entry

        paired.append(entry)

    return paired, holding


def analyze(paired: list[dict], init_capital: float = 1000) -> dict:
    """分析交易表现"""
    closed = [p for p in paired if p["result"] != "holding"]
    wins = [p for p in closed if p["result"] == "win"]
    losses = [p for p in closed if p["result"] == "loss"]

    total_pnl = sum(p["net_pnl"] for p in closed)
    total_commission = sum(p["commission"] for p in closed)
    win_rate = len(wins) / max(len(closed), 1) * 100

    # Direction analysis
    longs = [p for p in closed if p["direction"] == "LONG"]
    shorts = [p for p in closed if p["direction"] == "SHORT"]

    # Consecutive losses
    max_consecutive = 0
    current_streak = 0
    for p in closed:
        if p["result"] == "loss":
            current_streak += 1
            max_consecutive = max(max_consecutive, current_streak)
        else:
            current_streak = 0

    # Loss pattern
    loss_pnls = [p["net_pnl"] for p in losses]
    avg_loss = sum(loss_pnls) / max(len(loss_pnls), 1)
    max_loss = min(loss_pnls) if loss_pnls else 0

    # Regime diagnosis
    long_pnl = sum(p["net_pnl"] for p in longs)
    short_pnl = sum(p["net_pnl"] for p in shorts)

    # Patterns
    patterns = []
    if avg_loss < -30:
        patterns.append("大额止损: 单笔亏损超30，考虑缩小仓位或收紧止损")
    if max_consecutive >= 3:
        patterns.append(f"连续亏损{max_consecutive}次: 市场震荡或策略不适配当前行情")
    if long_pnl < 0 and short_pnl < 0:
        patterns.append("双向亏损: 市场横盘震荡，ADX过滤可能不足")
    if total_commission > abs(total_pnl) * 0.2:
        patterns.append(f"手续费占比高({total_commission:.0f}/{abs(total_pnl):.0f}): 降低交易频率")

    # Suggestions
    suggestions = []
    if max_consecutive >= 2:
        suggestions.append("ADX < 25 时不交易，避免震荡市被反复止损")
    if len(closed) < 5:
        suggestions.append("样本不足 (<5笔)，继续收集数据")
    else:
        if loss_pnls and min(loss_pnls) < -50:
            suggestions.append("单笔最大亏损超$50，启用ATR动态止损 (2x ATR)")
        if win_rate < 40:
            suggestions.append("胜率低 (<40%)，增加外部信号确认 (新闻情绪/恐惧贪婪)")
        if len(closed) > 10 and win_rate < 30:
            suggestions.append("持续低胜率，考虑暂停实盘，回测验证参数")

    suggestions.append("每日复盘自动调参: 根据胜负比动态调整RSI/MACD阈值")

    return {
        "summary": {
            "total_trades": len(closed),
            "wins": len(wins),
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "total_commission": round(total_commission, 2),
            "init_capital": init_capital,
            "current_equity": round(init_capital + total_pnl, 2),
            "return_pct": round(total_pnl / init_capital * 100, 2),
        },
        "direction": {
            "long": {"count": len(longs), "pnl": round(long_pnl, 2)},
            "short": {"count": len(shorts), "pnl": round(short_pnl, 2)},
        },
        "loss_analysis": {
            "avg_loss": round(avg_loss, 2),
            "max_loss": round(max_loss, 2),
            "consecutive_losses": max_consecutive,
            "patterns": patterns,
        },
        "suggestions": suggestions,
    }


def get_current_state() -> dict:
    """读取当前模拟盘状态"""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"cash": 1000, "total_trades": 0, "winning_trades": 0}


def generate_review(json_mode: bool = False) -> dict:
    """生成完整复盘报告"""
    trades = parse_trades(LOG_FILE)
    if not trades:
        empty = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "period": {"start": None, "end": None},
            "message": "暂无交易记录",
            "summary": {"total_trades": 0, "wins": 0, "win_rate": 0},
            "direction": {"long": {"count": 0, "pnl": 0}, "short": {"count": 0, "pnl": 0}},
            "loss_analysis": {},
        }
        return empty

    paired, holding = pair_trades(trades)
    state = get_current_state()
    analysis = analyze(paired, init_capital=1000)

    # 行为诊断
    closed_trades = [p for p in paired if p["result"] != "holding"]
    behavior = run_full_diagnosis(closed_trades)
    analysis["behavior"] = behavior

    period_start = trades[0]["time"]
    period_end = trades[-1]["time"]

    report = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "period": {"start": period_start, "end": period_end},
        **analysis,
        "trades": paired,
    }

    if holding:
        report["holding"] = holding

    # Save JSON
    json_path = OUTPUT_DIR / f"daily_review_{datetime.now().strftime('%Y-%m-%d')}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return report


def format_markdown(report: dict) -> str:
    """格式化为 Markdown 推送"""
    s = report["summary"]
    d = report["direction"]
    l = report.get("loss_analysis", {})

    lines = [
        "📊 **CryptoQuant 每日复盘**",
        f"📅 {report['date']} | {report['period']['start'][:10]} ~ {report['period']['end'][:10]}",
        "",
        "━━━━━━━━━━━━━━━",
        "📈 **交易统计**",
        f"• 交易次数: {s['total_trades']} | 胜率: {s['win_rate']}%",
        f"• 累计净利: ${s['total_pnl']:+.2f} | 手续费: ${s['total_commission']:.2f}",
        f"• 权益: ${s['current_equity']:,.2f} ({s['return_pct']:+.2f}%)",
        "",
        "━━━━━━━━━━━━━━━",
        "🎯 **方向分析**",
        f"• LONG: {d['long']['count']}笔, PnL=${d['long']['pnl']:+.2f}",
        f"• SHORT: {d['short']['count']}笔, PnL=${d['short']['pnl']:+.2f}",
        "",
    ]

    if l:
        lines.append("━━━━━━━━━━━━━━━")
        lines.append("⚠️ **亏损分析**")
        lines.append(f"• 平均亏损: ${l['avg_loss']:.2f} | 最大亏损: ${l['max_loss']:.2f}")
        lines.append(f"• 连亏次数: {l['consecutive_losses']}")
        for p in l.get("patterns", []):
            lines.append(f"• {p}")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━")
    lines.append("💡 **优化建议**")
    for i, sug in enumerate(report.get("suggestions", []), 1):
        lines.append(f"{i}. {sug}")

    if report.get("holding"):
        h = report["holding"]
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━")
        lines.append(f"📌 **当前持仓**: {h['direction']} {h['leverage']}x @ ${h['entry_price']:,.0f}")

    # 行为诊断 section
    if report.get("behavior"):
        lines.append("")
        lines.append(format_behavior_markdown(report["behavior"]))

    # 因子 bias
    try:
        factor_bias = get_active_factor_bias()
        if factor_bias.get("active_factors"):
            lines.append("")
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("🧬 **因子信号**")
            lines.append(f"• Bias: {factor_bias['bias']} (置信度 {factor_bias['confidence']})")
            lines.append(f"• 活跃因子: {', '.join(factor_bias['active_factors'][:3])}")
    except Exception:
        pass

    lines.append("")
    lines.append(f"🤖 CryptoQuant · {report['generated_at']}")

    return "\n".join(lines)


if __name__ == "__main__":
    json_mode = "--json" in sys.argv

    report = generate_review()

    if json_mode:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_markdown(report))