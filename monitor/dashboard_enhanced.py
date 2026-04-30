"""实时监控看板 - HTML Dashboard (增强版)

生成一个高端深色主题的实时状态看板，包含:
- 账户总览 (权益、可用余额、浮动盈亏)
- 持仓详情
- 最近交易记录
- 权益曲线 (内嵌)
- 策略状态指示器
- 实盘模式监控 (API连接、保证金、强平距离、风险等级)

设计风格: Midnight Navy + Electric Blue (你喜欢的风格)
"""

import json
from datetime import datetime
from typing import Dict, Any, List, Optional
from pathlib import Path

from execution.executor import LiveAccount, LivePosition, LiveOrder


def generate_dashboard_html(account: LiveAccount, 
                             strategy_name: str = "MATrend(7/25)",
                             mode: str = "SIMULATION",
                             symbol: str = "BTC-USDT",
                             uptime: str = "00:00:00",
                             extra_info: Dict = None,
                             api_connected: bool = True,
                             risk_level: str = "SAFE",
                             margin_used: float = 0,
                             liq_distance: float = 0) -> str:
    """生成实时看板 HTML (增强版)
    
    Args:
        account: 账户快照
        strategy_name: 策略名称
        mode: LIVE / SIMULATION
        symbol: 交易对
        uptime: 运行时间
        extra_info: 额外信息 (信号历史等)
        api_connected: API连接状态
        risk_level: 风险等级 (SAFE, WARNING, DANGER)
        margin_used: 保证金使用率 (0-1)
        liq_distance: 强平距离 (0-1)
    
    Returns:
        完整 HTML 字符串
    """
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 损益颜色
    pnl_color = "#00D1FF"  # positive
    pnl_sign = "+"
    if account.unrealized_pnl < 0:
        pnl_color = "#FF4757"
        pnl_sign = ""
    
    # 初始资金根据模式调整
    initial_capital = 10000 if mode == "LIVE" else 1000
    total_return = ((account.total_equity - initial_capital) / initial_capital * 100) if account.total_equity else 0
    return_color = pnl_color if total_return >= 0 else "#FF4757"
    
    # 风险等级颜色
    risk_colors = {
        "SAFE": "#00D1FF",
        "WARNING": "#FFA500",
        "DANGER": "#FF4757",
        "UNKNOWN": "#64748B"
    }
    risk_color = risk_colors.get(risk_level, "#64748B")
    
    # 保证金使用率颜色
    margin_color = "#00D1FF" if margin_used < 0.6 else "#FFA500" if margin_used < 0.8 else "#FF4757"
    
    # 强平距离颜色
    liq_color = "#00D1FF" if liq_distance > 0.2 else "#FFA500" if liq_distance > 0.1 else "#FF4757"
    
    # === HTML 模板 ===
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="30">
<title>CryptoQuant Dashboard</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
    background: #0A0E17;
    color: #E2E8F0;
    min-height: 100vh;
    padding: 20px;
  }}
  
  .container {{
    max-width: 1200px;
    margin: 0 auto;
  }}
  
  /* Header */
  .header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 24px 32px;
    background: linear-gradient(135deg, #141B2D 0%, #1A2240 100%);
    border: 1px solid #1E293B;
    border-radius: 16px;
    margin-bottom: 20px;
    box-shadow: 0 4px 30px rgba(0, 0, 0, 0.3);
  }}
  
  .header-left h1 {{
    font-size: 28px;
    font-weight: 700;
    background: linear-gradient(135deg, #FFFFFF 0%, #00D1FF 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
  }}
  
  .header-left .subtitle {{
    color: #94A3B8;
    font-size: 13px;
    margin-top: 4px;
    font-family: 'Fira Code', 'Consolas', monospace;
  }}
  
  .header-right {{
    display: flex;
    gap: 16px;
    align-items: center;
  }}
  
  .status-badge {{
    padding: 6px 16px;
    border-radius: 20px;
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.5px;
  }}
  
  .status-live {{
    background: rgba(0, 209, 255, 0.15);
    color: #00D1FF;
    border: 1px solid rgba(0, 209, 255, 0.3);
    animation: pulse 2s infinite;
  }}
  
  .status-sim {{
    background: rgba(255, 165, 0, 0.15);
    color: #FFA500;
    border: 1px solid rgba(255, 165, 0, 0.3);
  }}
  
  @keyframes pulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.7; }}
  }}
  
  .api-status {{
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 12px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 600;
  }}
  
  .api-connected {{
    background: rgba(0, 209, 255, 0.15);
    color: #00D1FF;
    border: 1px solid rgba(0, 209, 255, 0.3);
  }}
  
  .api-disconnected {{
    background: rgba(255, 71, 87, 0.15);
    color: #FF4757;
    border: 1px solid rgba(255, 71, 87, 0.3);
  }}
  
  .status-dot {{
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: currentColor;
    animation: blink 1.5s infinite;
  }}
  
  @keyframes blink {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.3; }}
  }}
  
  .update-time {{
    color: #94A3B8;
    font-size: 12px;
    font-family: 'Fira Code', monospace;
  }}
  
  /* Metrics Grid */
  .metrics-grid {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    margin-bottom: 20px;
  }}
  
  .metric-card {{
    background: #141B2D;
    border: 1px solid #1E293B;
    border-radius: 14px;
    padding: 20px 24px;
    box-shadow: 0 2px 20px rgba(0, 0, 0, 0.2);
    transition: transform 0.2s, box-shadow 0.2s;
  }}
  
  .metric-card:hover {{
    transform: translateY(-2px);
    box-shadow: 0 4px 30px rgba(0, 209, 255, 0.1);
  }}
  
  .metric-label {{
    color: #94A3B8;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 8px;
  }}
  
  .metric-value {{
    font-size: 28px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }}
  
  .metric-sub {{
    font-size: 12px;
    margin-top: 4px;
  }}
  
  /* Risk Indicators */
  .risk-indicators {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 16px;
    margin-bottom: 20px;
  }}
  
  .risk-card {{
    background: #141B2D;
    border: 1px solid #1E293B;
    border-radius: 12px;
    padding: 16px 20px;
    box-shadow: 0 2px 20px rgba(0, 0, 0, 0.2);
  }}
  
  .risk-label {{
    color: #94A3B8;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 8px;
  }}
  
  .risk-value {{
    font-size: 20px;
    font-weight: 700;
    margin-bottom: 8px;
  }}
  
  .progress-bar {{
    height: 6px;
    background: #1E293B;
    border-radius: 3px;
    overflow: hidden;
  }}
  
  .progress-fill {{
    height: 100%;
    border-radius: 3px;
    transition: width 0.3s ease;
  }}
  
  .risk-badge {{
    display: inline-block;
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}
  
  /* Panels */
  .panels {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 20px;
  }}
  
  .panel {{
    background: #141B2D;
    border: 1px solid #1E293B;
    border-radius: 14px;
    padding: 24px;
    box-shadow: 0 2px 20px rgba(0, 0, 0, 0.2);
  }}
  
  .panel-title {{
    font-size: 16px;
    font-weight: 600;
    color: #00D1FF;
    margin-bottom: 16px;
    padding-bottom: 12px;
    border-bottom: 1px solid #1E293B;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}
  
  /* Position Table */
  .pos-table {{
    width: 100%;
    border-collapse: collapse;
  }}
  
  .pos-table th {{
    text-align: left;
    color: #94A3B8;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    padding: 8px 12px;
    border-bottom: 1px solid #1E293B;
  }}
  
  .pos-table td {{
    padding: 12px;
    border-bottom: 1px solid rgba(30, 41, 59, 0.5);
    font-size: 14px;
    font-variant-numeric: tabular-nums;
  }}
  
  .pos-table tr:hover td {{
    background: rgba(0, 209, 255, 0.03);
  }}
  
  .badge-long {{
    display: inline-block;
    padding: 2px 10px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 600;
    background: rgba(0, 209, 255, 0.15);
    color: #00D1FF;
  }}
  
  .badge-short {{
    display: inline-block;
    padding: 2px 10px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 600;
    background: rgba(255, 71, 87, 0.15);
    color: #FF4757;
  }}
  
  .profit {{ color: #00D1FF; }}
  .loss {{ color: #FF4757; }}
  
  /* Trades List */
  .trade-item {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 12px;
    border-radius: 8px;
    margin-bottom: 6px;
    background: rgba(255, 255, 255, 0.02);
    font-size: 13px;
  }}
  
  .trade-item:hover {{
    background: rgba(0, 209, 255, 0.05);
  }}
  
  .trade-side {{
    font-weight: 600;
    font-size: 12px;
  }}
  
  .buy-color {{ color: #00D1FF; }}
  .sell-color {{ color: #FF4757; }}
  
  .trade-time {{
    color: #64748B;
    font-size: 11px;
    font-family: 'Fira Code', monospace;
  }}
  
  .empty-state {{
    text-align: center;
    color: #64748B;
    padding: 40px 20px;
    font-size: 14px;
  }}
  
  /* Strategy Info */
  .strategy-box {{
    background: rgba(0, 209, 255, 0.05);
    border: 1px solid rgba(0, 209, 255, 0.2);
    border-radius: 12px;
    padding: 20px;
    grid-column: 1 / -1;
  }}
  
  .strategy-box h3 {{
    color: #00D1FF;
    margin-bottom: 12px;
    font-size: 15px;
  }}
  
  .strategy-params {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    gap: 12px;
  }}
  
  .param-item {{
    display: flex;
    justify-content: space-between;
    font-size: 13px;
  }}
  
  .param-label {{ color: #94A3B8; }}
  .param-value {{ color: #E2E8F0; font-weight: 500; }}
  
  /* Signals */
  .signals-box {{
    margin-top: 12px;
    padding: 12px 16px;
    background: rgba(0, 0, 0, 0.3);
    border-radius: 8px;
    font-family: 'Fira Code', monospace;
    font-size: 12px;
    color: #94A3B8;
    max-height: 120px;
    overflow-y: auto;
  }}
  
  .signal-buy {{ color: #00D1FF; }}
  .signal-sell {{ color: #FF4757; }}
  .signal-hold {{ color: #64748B; }}
  
  /* Footer */
  .footer {{
    text-align: center;
    color: #475569;
    font-size: 11px;
    padding: 16px;
    font-family: 'Fira Code', monospace;
  }}

  /* Mobile Responsive */
  @media (max-width: 768px) {{
    .container {{
      padding: 10px;
    }}

    .header {{
      flex-direction: column;
      gap: 12px;
      text-align: center;
      padding: 16px 20px;
    }}

    .header-left h1 {{
      font-size: 22px;
    }}

    .header-left .subtitle {{
      font-size: 11px;
    }}

    .header-right {{
      flex-wrap: wrap;
      justify-content: center;
      gap: 8px;
    }}

    .status-badge {{
      font-size: 11px;
      padding: 4px 12px;
    }}

    .api-status {{
      font-size: 11px;
      padding: 4px 10px;
    }}

    .update-time {{
      font-size: 10px;
    }}

    .metrics-grid {{
      grid-template-columns: repeat(2, 1fr);
      gap: 10px;
    }}

    .metric-card {{
      padding: 16px;
    }}

    .metric-value {{
      font-size: 22px;
    }}

    .risk-indicators {{
      grid-template-columns: 1fr;
      gap: 10px;
    }}

    .risk-card {{
      padding: 14px 16px;
    }}

    .risk-value {{
      font-size: 18px;
    }}

    .panels {{
      grid-template-columns: 1fr;
      gap: 12px;
    }}

    .panel {{
      padding: 16px;
    }}

    .panel-title {{
      font-size: 14px;
    }}

    .pos-table {{
      font-size: 11px;
    }}

    .pos-table th,
    .pos-table td {{
      padding: 8px 4px;
    }}

    .trade-item {{
      flex-direction: column;
      align-items: flex-start;
      gap: 8px;
    }}

    .trade-time {{
      align-self: flex-end;
    }}

    .strategy-params {{
      grid-template-columns: 1fr;
    }}
  }}

  @media (max-width: 480px) {{
    .metrics-grid {{
      grid-template-columns: 1fr;
    }}

    .metric-value {{
      font-size: 20px;
    }}

    .header-left h1 {{
      font-size: 18px;
    }}
  }}
</style>
</head>
<body>

<div class="container">
  
  <!-- Header -->
  <div class="header">
    <div class="header-left">
      <h1>CryptoQuant Dashboard</h1>
      <div class="subtitle">{symbol} · {strategy_name} · {mode} MODE</div>
    </div>
    <div class="header-right">
      <span class="status-badge {'status-sim' if mode == 'SIMULATION' else 'status-live'}">{mode}</span>
      <span class="api-status {'api-connected' if api_connected else 'api-disconnected'}">
        <span class="status-dot"></span>
        {'API' if mode == 'LIVE' else 'SIM'}
      </span>
      <span class="update-time">⏱ {uptime} · {now}</span>
    </div>
  </div>
  
  <!-- Metrics -->
  <div class="metrics-grid">
    <div class="metric-card">
      <div class="metric-label">📊 总权益</div>
      <div class="metric-value" style="color: {return_color}">${account.total_equity:,.2f}</div>
      <div class="metric-sub" style="color: {return_color}">{'+' if total_return >= 0 else ''}{total_return:.2f}%</div>
    </div>
    
    <div class="metric-card">
      <div class="metric-label">💰 可用余额</div>
      <div class="metric-value">${account.available_balance:,.2f}</div>
      <div class="metric-sub" style="color: #94A3B8">初始: ${initial_capital:,}</div>
    </div>
    
    <div class="metric-card">
      <div class="metric-label">📈 浮动盈亏</div>
      <div class="metric-value" style="color: {pnl_color}">{pnl_sign}${account.unrealized_pnl:,.2f}</div>
      <div class="metric-sub" style="color: {pnl_color}">{pnl_sign}{((account.unrealized_pnl/initial_capital)*100):.2f}%</div>
    </div>
    
    <div class="metric-card">
      <div class="metric-label">📋 交易统计</div>
      <div class="metric-value" style="color: #E2E8F0">{account.total_trades}</div>
      <div class="metric-sub" style="color: #94A3B8">胜率: {account.win_rate:.1f}% ({account.winning_trades}W)</div>
    </div>
  </div>
  
  <!-- Risk Indicators (仅实盘模式显示) -->
  {f'''<div class="risk-indicators">
    <div class="risk-card">
      <div class="risk-label">⚠️ 风险等级</div>
      <div class="risk-value" style="color: {risk_color}">{risk_level}</div>
      <span class="risk-badge" style="background: rgba({int(risk_color[1:3], 16)}, {int(risk_color[3:5], 16)}, {int(risk_color[5:7], 16)}, 0.15); color: {risk_color}; border: 1px solid rgba({int(risk_color[1:3], 16)}, {int(risk_color[3:5], 16)}, {int(risk_color[5:7], 16)}, 0.3);">
        {risk_level}
      </span>
    </div>
    
    <div class="risk-card">
      <div class="risk-label">💳 保证金使用率</div>
      <div class="risk-value" style="color: {margin_color}">{margin_used*100:.1f}%</div>
      <div class="progress-bar">
        <div class="progress-fill" style="width: {margin_used*100}%; background: {margin_color};"></div>
      </div>
    </div>
    
    <div class="risk-card">
      <div class="risk-label">🛡️ 强平距离</div>
      <div class="risk-value" style="color: {liq_color}">{liq_distance*100:.1f}%</div>
      <div class="progress-bar">
        <div class="progress-fill" style="width: {liq_distance*100}%; background: {liq_color};"></div>
      </div>
    </div>
  </div>''' if mode == 'LIVE' else ''}
  
  <!-- Panels -->
  <div class="panels">
    
    <!-- 持仓 -->
    <div class="panel">
      <div class="panel-title">
        📌 持仓明细
        <span style="font-size:12px;color:#94A3B8;font-weight:400">{len(account.positions)} 个</span>
      </div>
      
      {"".join([_position_row(pos) for pos in account.positions]) if account.positions else '<div class="empty-state">暂无持仓 🏖️</div>'}
    </div>
    
    <!-- 最近交易 -->
    <div class="panel">
      <div class="panel-title">
        🕐 最近交易
        <span style="font-size:12px;color:#94A3B8;font-weight:400">{len(account.recent_trades)} 条</span>
      </div>
      
      {"".join([_trade_item(t) for t in account.recent_trades]) if account.recent_trades else '<div class="empty-state">等待首次交易 ⏳</div>'}
    </div>
  </div>
  
  <!-- Strategy Info -->
  <div class="strategy-box">
    <h3>⚙️ 策略配置: {strategy_name}</h3>
    <div class="strategy-params">
      <div class="param-item"><span class="param-label">交易对</span><span class="param-value">{symbol}</span></div>
      <div class="param-item"><span class="param-label">K线周期</span><span class="param-value">1H</span></div>
      <div class="param-item"><span class="param-label">止损</span><span class="param-value">-3%</span></div>
      <div class="param-item"><span class="param-label">止盈</span><span class="param-value">+10%</span></div>
      <div class="param-item"><span class="param-label">MA快/慢</span><span class="param-value">7/25</span></div>
      <div class="param-item"><span class="param-label">RSI阈值</span><span class="param-value">35/65</span></div>
    </div>
    
    {_signals_box(extra_info.get('recent_signals', []) if extra_info else []) if extra_info else ''}
  </div>
  
  <!-- Footer -->
  <div class="footer">
    CryptoQuant v0.1 · Powered by Hermes Agent · {now}
  </div>

</div>

</body>
</html>"""
    
    return html


def _position_row(pos: LivePosition) -> str:
    """生成持仓行 HTML"""
    pnl_class = "profit" if pos.unrealized_pnl >= 0 else "loss"
    pnl_sign = "+" if pos.unrealized_pnl >= 0 else ""
    side_class = "badge-long" if pos.side == "long" else "badge-short"
    
    return f"""<table class="pos-table">
  <tr>
    <td>{pos.symbol}</td>
    <td><span class="{side_class}">{pos.side.upper()}</span></td>
    <td>{pos.size:.4f}</td>
    <td>${pos.entry_price:,.2f}</td>
    <td>${pos.current_price:,.2f}</td>
    <td class="{pnl_class}">{pnl_sign}${pos.unrealized_pnl:,.2f}</td>
    <td class="{pnl_class}">{pnl_sign}{pos.unrealized_pnl_pct:.2f}%</td>
    <td>{pos.leverage}x</td>
  </tr>
</table>"""


def _trade_item(trade: LiveOrder) -> str:
    """生成交易记录 HTML"""
    time_str = datetime.fromtimestamp(trade.timestamp / 1000).strftime("%m-%d %H:%M")
    side_class = "buy-color" if trade.side == "BUY" else "sell-color"
    
    return f"""<div class="trade-item">
  <span class="trade-side {side_class}">{trade.side}</span>
  <span>{trade.size:.4f} @ ${trade.price:,.2f}</span>
  <span class="trade-time">{time_str}</span>
</div>"""


def _signals_box(signals: List[str]) -> str:
    """生成信号历史 HTML"""
    if not signals:
        return ""
    
    items = []
    for s in signals[-10:]:
        cls = "signal-hold"
        if "BUY" in s:
            cls = "signal-buy"
        elif "SELL" in s:
            cls = "signal-sell"
        items.append(f'<span class="{cls}">{s}</span>')
    
    return f"""<div class="signals-box">
  📡 最近信号: {' · '.join(items)}
</div>"""


def save_dashboard(account: LiveAccount, 
                   output_path: Path = None,
                   **kwargs) -> Path:
    """生成并保存看板 HTML"""
    if output_path is None:
        from core.config import get_config
        output_path = get_config().project_root / "data" / "dashboard.html"
    
    html = generate_dashboard_html(account, **kwargs)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    
    return output_path
