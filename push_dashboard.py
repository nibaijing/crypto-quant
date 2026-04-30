#!/usr/bin/env python3
"""看板推送脚本 - 由 cron 每30分钟调用一次

读取当前账户状态，生成看板内容并输出到 stdout。
Hermes cron 会自动将 stdout 内容通过 Telegram 发送。
"""

import sys
import json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from core.config import init_config
init_config()

from execution.executor import SpotExecutor


def main():
    executor = SpotExecutor(sandbox=True)
    
    # 更新价格
    ticker = executor.get_ticker("BTC-USDT")
    price = ticker["price"] if ticker else 0
    executor.update_price("BTC-USDT", price)
    
    account = executor.get_account()
    total_return = (account.total_equity - 10000) / 10000 * 100
    pnl_emoji = "🟢" if total_return >= 0 else "🔴"
    
    # 读取日志最后几行获取信号
    log_path = Path(__file__).parent / "data" / "live_trading.log"
    recent_lines = []
    try:
        with open(log_path) as f:
            lines = f.readlines()[-20:]
        for line in lines:
            if "信号:" in line:
                recent_lines.append(line.strip().split("|")[-1].strip()[-80:])
    except:
        pass
    
    # 持仓详情
    if account.positions:
        p = account.positions[0]
        pnl_sign = "+" if p.unrealized_pnl >= 0 else ""
        pos_info = (
            f"*{p.side.upper()}* {p.size:.4f} BTC\n"
            f"开仓: ${p.entry_price:,.2f} → 当前: ${p.current_price:,.2f}\n"
            f"浮动: {pnl_sign}${p.unrealized_pnl:,.2f} _({pnl_sign}{p.unrealized_pnl_pct:.2f}%)_\n"
            f"杠杆: {p.leverage}x"
        )
    else:
        pos_info = "无持仓 🏖️"
    
    # 运行时长
    try:
        state_file = Path(__file__).parent / "data" / "live_state.json"
        if state_file.exists():
            mtime = datetime.fromtimestamp(state_file.stat().st_mtime)
            delta = datetime.now() - mtime
            h, r = divmod(delta.seconds, 3600)
            m, s = divmod(r, 60)
            uptime = f"{delta.days}d {h:02d}:{m:02d}"
        else:
            uptime = "N/A"
    except:
        uptime = "N/A"
    
    # 信号历史
    signals_block = "\n".join(f"• {s}" for s in recent_lines[-5:]) if recent_lines else "等待信号..."
    
    print(f"""📊 **CryptoQuant 模拟盘报告**

⏱ 运行: {uptime} | 📅 {datetime.now().strftime('%m-%d %H:%M')}

---

{pnl_emoji} **账户总览**
• 总权益: **${account.total_equity:,.2f}** _({total_return:+.2f}%)_
• 可用余额: ${account.available_balance:,.2f}
• 浮动盈亏: ${account.unrealized_pnl:,.2f}

📌 **持仓**
{pos_info}

📋 **交易统计**
• 总交易: {account.total_trades} | 胜率: {account.win_rate:.1f}%
• 策略: MATrend(7/25)
• BTC: ${price:,.2f}

📡 **最近信号**
{signals_block}

---
> CryptoQuant v0.1 · 模拟盘 · {datetime.now().strftime('%Y-%m-%d %H:%M')}""")
    
    # 输出文件路径供 Hermes 获取 MEDIA
    html_path = Path(__file__).parent / "data" / "dashboard.html"
    if html_path.exists():
        print(f"\nMEDIA:{html_path}")


if __name__ == "__main__":
    main()