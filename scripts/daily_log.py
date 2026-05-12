#!/usr/bin/env python3
"""CryptoQuant 当日日志看板 — 从日志文件聚合当天运行状态

Usage:
    python scripts/daily_log.py                # 当天完整看板
    python scripts/daily_log.py --date 2026-05-11   # 指定日期
    python scripts/daily_log.py --signal       # 只看SIGNAL LOG
    python scripts/daily_log.py --trade        # 只看TRADE LOG
    python scripts/daily_log.py --system       # 只看系统状态
"""

import re
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Optional

PROJECT = Path(__file__).resolve().parent.parent
LOG_DT_FMT = "%Y-%m-%d %H:%M:%S"
W = 62


def get_today() -> str:
    return date.today().strftime("%Y-%m-%d")


def is_today_line(line: str, target_date: str) -> bool:
    return len(line) >= 10 and line[:10] == target_date


def parse_entry_action(line: str, ts: str, src: str) -> Optional[dict]:
    """Parse a trading entry/exit action from a log line"""
    # 📡 信号: LONG/SHORT/SELL/COVER @$
    m = re.search(r"📡 信号: (\w+) \| close=\$([\d,]+)", line)
    if m:
        return {"ts": ts, "src": src, "tag": "signal",
                "action": m.group(1), "price": m.group(2).replace(",", "")}

    # force override
    m = re.search(r"🔒 force override: (.+)", line)
    if m:
        return {"ts": ts, "src": src, "tag": "override", "detail": m.group(1)[:60]}

    # ATR止损
    m = re.search(r"ATR止损\(直接执行\): (\w+)", line)
    if m:
        return {"ts": ts, "src": src, "tag": "risk_exit", "action": m.group(1)}

    # AI决策（非HOLD）
    m = re.search(r"(✅|🚨) AI决策: (LONG|SHORT|SELL|COVER) \| confidence=([\d.]+)%", line)
    if m:
        return {"ts": ts, "src": src, "tag": "ai_decision",
                "action": m.group(2), "confidence": m.group(3)}

    # AI决策触发 (trigger line)
    m = re.search(r"(🚨|🤖) AI决策触发: (.+?) \| \$([\d,]+)", line)
    if m:
        return {"ts": ts, "src": src, "tag": "ai_trigger", "trigger": m.group(2),
                "price": m.group(3).replace(",", "")}

    # 平仓AI审核
    m = re.search(r"🤖 平仓AI审核: (\w+)\((\w+)\) \| \$([\d,]+)", line)
    if m:
        return {"ts": ts, "src": src, "tag": "exit_audit",
                "exit_signal": m.group(1), "exit_type": m.group(2),
                "price": m.group(3).replace(",", "")}

    return None


def parse_status(line: str, ts: str) -> Optional[dict]:
    """Parse system status line"""
    # 📊 $price | Eq=$eq | Pos=pnl% | SIG=sig | K#k
    m = re.search(r"📊 \$([\d,]+) \| Eq=\$([\d,]+)(.*)", line)
    if m:
        entry = {"ts": ts, "price": m.group(1).replace(",", ""),
                 "equity": m.group(2).replace(",", "")}
        rest = m.group(3)
        pm = re.search(r"Pos=([+\-.\d]+)%", rest)
        if pm: entry["pnl"] = pm.group(1)
        sm = re.search(r"SIG=(\w+)", rest)
        if sm: entry["signal"] = sm.group(1)
        km = re.search(r"K#(\d+)", rest)
        if km: entry["kline"] = int(km.group(1))
        return entry
    return None


def collect(target_date: str) -> dict:
    """Collect all events for target_date from log files"""
    events = []
    statuses = []
    overrides = []
    override_keys = set()
    equity_history = []
    startup = None
    llm_model = None
    kline_total = 0

    # Primary: live_trading.log
    lf = PROJECT / "data" / "live_trading.log"
    if lf.exists():
        with open(lf) as f:
            for raw in f:
                line = raw.strip()
                if not line or not is_today_line(line, target_date):
                    continue
                ts = line[:19]
                # System events
                if "LLM 可用" in line:
                    m = re.search(r"🤖 LLM 可用 \((.+?)\)", line)
                    if m: llm_model = m.group(1)
                if "LLM 不可用" in line:
                    llm_model = "N/A (fallback)"
                if "🚀 CryptoQuant" in line or "启动" in line:
                    startup = ts

                # Status
                st = parse_status(line, ts)
                if st:
                    statuses.append(st)
                    try: equity_history.append(float(st["equity"]))
                    except: pass
                    if st.get("kline") and st["kline"] > kline_total:
                        kline_total = st["kline"]
                    continue

                # Actions
                act = parse_entry_action(line, ts, "live")
                if act:
                    events.append(act)
                    if act["tag"] == "override":
                        k = act["detail"][:50]
                        if k not in override_keys:
                            override_keys.add(k)
                            overrides.append(act)
                    continue

    # Secondary: decision_engine.log (only force-override from here)
    df = PROJECT / "data" / "decision_engine.log"
    if df.exists():
        with open(df) as f:
            for raw in f:
                line = raw.strip()
                if not line or not is_today_line(line, target_date):
                    continue
                ts = line[:19]
                act = parse_entry_action(line, ts, "engine")
                if act and act["tag"] == "override":
                    k = act["detail"][:50]
                    if k not in override_keys:
                        override_keys.add(k)
                        overrides.append(act)
                # LLM availability
                if "LLM 可用" in line and not llm_model:
                    m = re.search(r"🤖 LLM 可用 \((.+?)\)", line)
                    if m: llm_model = m.group(1)

    # Deduplicate events by time+tag+action for same-tag noise
    seen_events = set()
    deduped = []
    for e in events:
        key = (e["ts"], e["tag"], e.get("action", e.get("detail", "")))
        if key not in seen_events:
            seen_events.add(key)
            deduped.append(e)

    return {
        "events": deduped,
        "statuses": statuses,
        "overrides": overrides,
        "equity_history": equity_history,
        "startup": startup,
        "llm_model": llm_model,
        "kline_total": kline_total,
    }


def calc_uptime(startup_str: str) -> str:
    if not startup_str: return "?"
    try:
        s = datetime.strptime(startup_str, LOG_DT_FMT)
        e = datetime.now()
        delta = e - s
        h, r = divmod(delta.seconds, 3600)
        m = r // 60
        return f"{delta.days}d {h:02d}:{m:02d}m"
    except:
        return "?"


def render(data: dict, target_date: str, mode: str = "all"):
    W2 = W - 4
    def S(t=""):
        return f"║  {str(t)}{' ' * max(0, W2 - len(str(t)))} ║"
    def H(t=""):
        if t: return f"║  ── {t} {'─' * max(0, W2 - len(t) - 4)} ║"
        return f"║  {'─' * W2} ║"
    def N(t=""):
        return f"║  {str(t)}{' ' * max(0, W2 - len(str(t)))} ║"

    eqs = data["equity_history"]
    start_eq = eqs[0] if eqs else 0
    cur_eq = eqs[-1] if eqs else 0
    pct_ret = ((cur_eq - 1000) / 1000 * 100) if cur_eq else 0

    # Group events by minute
    entries = [e for e in data["events"] if e["tag"] == "signal"]
    exit_audits = [e for e in data["events"] if e["tag"] == "exit_audit"]
    ai_decs = [e for e in data["events"] if e["tag"] == "ai_decision"]

    out = []
    out.append(f"╔{'═' * W2}╗")
    out.append(S(f"CryptoQuant Daily Report | {target_date}"))
    out.append(f"╠{'═' * W2}╣")
    out.append(H("System Run"))
    out.append(S())
    out.append(S(f"Startup: {data['startup'] or '?'}  |  Uptime: {calc_uptime(data['startup'])}"))
    out.append(S(f"Klines:  {data['kline_total']}  |  Equity: ${cur_eq:,.0f} ({pct_ret:+.1f}%)"))
    llm_label = ("✅ " + data["llm_model"]) if data["llm_model"] else "⚠️ N/A"
    out.append(S(f"LLM:     {llm_label}"))
    sig_entry = [e for e in entries if e["action"] in ("LONG", "SHORT")]
    sig_exit = [e for e in entries if e["action"] in ("SELL", "COVER")]
    out.append(S(f"Entries: {len(sig_entry)}  |  Exits: {len(sig_exit)}  |  Overrides: {len(data['overrides'])}"))
    out.append(S())

    if mode in ("all", "signal"):
        out.append(H("SIGNAL LOG"))
        out.append(S())

        # Build a timeline
        timeline = []

        # 1) exit audits
        for e in exit_audits:
            t = e["ts"][11:16]
            p = f"${e['price']}" if e.get("price") else ""
            timeline.append((e["ts"], f"{t}  🕵️  {e['exit_signal']} ({e['exit_type']})  {p}"))

        # 2) AI triggers
        for e in data["events"]:
            if e["tag"] == "ai_trigger":
                t = e["ts"][11:16]
                trig = e["trigger"][:30]  # truncate
                p = f"${e['price']}" if e.get("price") else ""
                timeline.append((e["ts"], f"{t}  🤖  {trig}  {p}"))

        # 3) AI decisions
        for e in ai_decs:
            t = e["ts"][11:16]
            a = e["action"]
            cf = f" ({e.get('confidence','?')}%)" if e.get("confidence") else ""
            timeline.append((e["ts"], f"{t}  {'✅' if a in ('LONG','SHORT') else '🚨'}  {a:<6}{cf}"))

        # 4) override lines
        for e in data["overrides"]:
            t = e["ts"][11:16]
            d = e.get("detail", "")[:52]
            timeline.append((e["ts"], f"{t}  🔒  {d}"))

        # 5) signal (actual execution)
        for e in entries:
            t = e["ts"][11:16]
            a = e["action"]
            p = f"@{e['price']}" if e.get("price") else ""
            timeline.append((e["ts"], f"{t}  {'📈' if a in ('LONG','COVER') else '📉'}   {a:<6} {p}"))

        # 6) risk
        for e in data["events"]:
            if e["tag"] == "risk_exit":
                t = e["ts"][11:16]
                timeline.append((e["ts"], f"{t}  ⚠️  ATR stop: {e.get('action','?')}"))

        # sort by time
        timeline.sort(key=lambda x: x[0])

        if not timeline:
            out.append(S("(no signals)"))
        else:
            for _, line in timeline:
                out.append(S(line))

        out.append(S())

    if mode in ("all", "trade"):
        out.append(H("TRADE LOG"))
        out.append(S())

        # Show entry-exit pairs from signal log
        pairs = []
        i = 0
        while i < len(entries):
            e = entries[i]
            if e["action"] in ("LONG", "SHORT"):
                entry_price = e["price"]
                entry_time = e["ts"][11:16]
                # Look for matching exit
                exit_info = None
                for j in range(i+1, min(i+4, len(entries))):
                    ex = entries[j]
                    if (e["action"] == "LONG" and ex["action"] == "SELL") or \
                       (e["action"] == "SHORT" and ex["action"] == "COVER"):
                        if ex.get("price"):
                            entry_float = float(entry_price)
                            ex_float = float(ex["price"])
                            pnl_abs = ((ex_float - entry_float) / entry_float * 100) if e["action"] == "LONG" \
                                else ((entry_float - ex_float) / entry_float * 100)
                            exit_info = {
                                "time": ex["ts"][11:16],
                                "price": ex["price"],
                                "pnl": pnl_abs,
                                "bars": j - i + 1,
                            }
                        i = j  # skip past exit
                        break
                pairs.append({
                    "action": e["action"], "entry_price": entry_price, "entry_time": entry_time,
                    "exit": exit_info,
                })
            i += 1

        if not pairs:
            out.append(S("(no trades)"))
        else:
            for p in pairs:
                a = p["action"]
                t = p["entry_time"]
                ep = p["entry_price"]
                if p["exit"]:
                    ex = p["exit"]
                    pnl_str = f" {ex['pnl']:+.2f}%" if ex['pnl'] != 0 else " 0.00%"
                    out.append(S(f"  {t}  {'📈' if a=='LONG' else '📉'} {a:<6} ${ep}  →  {ex['time']} ${ex['price']} ({ex['bars']}K){pnl_str}"))
                else:
                    out.append(S(f"  {t}  {'📈' if a=='LONG' else '📉'} {a:<6} ${ep}  →  [holding]"))

        out.append(S())

    out.append(H("Current Position"))
    out.append(S())
    if data["statuses"]:
        last = data["statuses"][-1]
        out.append(S(f"  Price: ${last.get('price','?')}  |  Equity: ${last.get('equity','?')}"))
        out.append(S(f"  PnL:   {last.get('pnl','?')}%  |  Signal: {last.get('signal','?')}  |  K#{last.get('kline','?')}"))
    else:
        out.append(S("  (no status data)"))
    out.append(S())
    out.append(f"╚{'═' * W2}╝")

    return "\n".join(out)


def main():
    target = get_today()
    mode = "all"
    for a in sys.argv[1:]:
        if a.startswith("--date="):
            target = a.split("=", 1)[1]
        elif a in ("--signal",):
            mode = "signal"
        elif a in ("--trade",):
            mode = "trade"
        elif a in ("--system",):
            mode = "system"

    data = collect(target)
    print(render(data, target, mode))
    print()
    print(f"📊 {target}  |  "
          f"{len(data['events'])} events, {len(data['statuses'])} status snapshots "
          f"| Mode: {mode}")


if __name__ == "__main__":
    main()