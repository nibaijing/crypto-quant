#!/usr/bin/env python3
"""测试增强版Dashboard"""

import sys
from pathlib import Path
from datetime import datetime

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from execution.executor import LiveAccount, LivePosition, LiveOrder
from monitor.dashboard_enhanced import generate_dashboard_html, save_dashboard


def test_enhanced_dashboard():
    """测试增强版Dashboard"""
    print("=" * 60)
    print("测试增强版Dashboard")
    print("=" * 60)
    
    # 创建测试账户数据
    account = LiveAccount(
        total_equity=10500.50,
        available_balance=8500.00,
        unrealized_pnl=500.50,
        total_trades=25,
        winning_trades=18,
        win_rate=72.0,
        positions=[
            LivePosition(
                symbol="BTC-USDT-SWAP",
                side="long",
                size=0.05,
                entry_price=65000.00,
                current_price=66000.00,
                unrealized_pnl=500.50,
                unrealized_pnl_pct=15.4,
                leverage=10,
                margin=3250.00,
                timestamp=int(datetime.now().timestamp() * 1000),
            )
        ],
        recent_trades=[
            LiveOrder(
                order_id="12345",
                symbol="BTC-USDT-SWAP",
                side="BUY",
                type="MARKET",
                size=0.05,
                price=65000.00,
                status="FILLED",
                timestamp=int(datetime.now().timestamp() * 1000),
            )
        ],
    )
    
    # 测试实盘模式
    print("\n1. 测试实盘模式")
    html = generate_dashboard_html(
        account=account,
        strategy_name="MATrend(7/25)",
        mode="LIVE",
        symbol="BTC-USDT-SWAP",
        uptime="1d 12:30:45",
        extra_info={
            "recent_signals": [
                "[14:30] LONG @ $65,000.00",
                "[14:15] HOLD @ $64,800.00",
                "[14:00] LONG @ $64,500.00",
            ]
        },
        api_connected=True,
        risk_level="SAFE",
        margin_used=0.31,
        liq_distance=0.25,
    )
    
    # 保存HTML
    output_path = Path(__file__).parent / "data" / "test_dashboard_live.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"✅ 实盘Dashboard已生成: {output_path}")
    
    # 测试模拟盘模式
    print("\n2. 测试模拟盘模式")
    account_sim = LiveAccount(
        total_equity=1050.50,
        available_balance=850.00,
        unrealized_pnl=50.50,
        total_trades=25,
        winning_trades=18,
        win_rate=72.0,
        positions=[
            LivePosition(
                symbol="BTC-USDT",
                side="long",
                size=0.005,
                entry_price=65000.00,
                current_price=66000.00,
                unrealized_pnl=50.50,
                unrealized_pnl_pct=15.4,
                leverage=10,
                margin=325.00,
                timestamp=int(datetime.now().timestamp() * 1000),
            )
        ],
        recent_trades=[
            LiveOrder(
                order_id="12345",
                symbol="BTC-USDT",
                side="BUY",
                type="MARKET",
                size=0.005,
                price=65000.00,
                status="FILLED",
                timestamp=int(datetime.now().timestamp() * 1000),
            )
        ],
    )
    
    html_sim = generate_dashboard_html(
        account=account_sim,
        strategy_name="MATrend(7/25)",
        mode="SIMULATION",
        symbol="BTC-USDT",
        uptime="2d 05:15:30",
        extra_info={
            "recent_signals": [
                "[14:30] LONG @ $65,000.00",
                "[14:15] HOLD @ $64,800.00",
            ]
        },
    )
    
    # 保存HTML
    output_path_sim = Path(__file__).parent / "data" / "test_dashboard_sim.html"
    output_path_sim.write_text(html_sim, encoding="utf-8")
    print(f"✅ 模拟盘Dashboard已生成: {output_path_sim}")
    
    # 测试风险等级
    print("\n3. 测试不同风险等级")
    for risk_level in ["SAFE", "WARNING", "DANGER"]:
        html_risk = generate_dashboard_html(
            account=account,
            strategy_name="MATrend(7/25)",
            mode="LIVE",
            symbol="BTC-USDT-SWAP",
            uptime="1d 12:30:45",
            api_connected=True,
            risk_level=risk_level,
            margin_used=0.8 if risk_level == "DANGER" else 0.5 if risk_level == "WARNING" else 0.3,
            liq_distance=0.05 if risk_level == "DANGER" else 0.15 if risk_level == "WARNING" else 0.25,
        )
        
        output_path_risk = Path(__file__).parent / "data" / f"test_dashboard_{risk_level.lower()}.html"
        output_path_risk.write_text(html_risk, encoding="utf-8")
        print(f"✅ {risk_level}等级Dashboard已生成: {output_path_risk}")
    
    # 测试API断开连接
    print("\n4. 测试API断开连接")
    html_offline = generate_dashboard_html(
        account=account,
        strategy_name="MATrend(7/25)",
        mode="LIVE",
        symbol="BTC-USDT-SWAP",
        uptime="1d 12:30:45",
        api_connected=False,
        risk_level="UNKNOWN",
        margin_used=0.3,
        liq_distance=0.25,
    )
    
    output_path_offline = Path(__file__).parent / "data" / "test_dashboard_offline.html"
    output_path_offline.write_text(html_offline, encoding="utf-8")
    print(f"✅ API断开Dashboard已生成: {output_path_offline}")
    
    print("\n" + "=" * 60)
    print("✅ 所有Dashboard测试完成")
    print("=" * 60)
    print("\n生成的文件:")
    print("  - data/test_dashboard_live.html (实盘模式)")
    print("  - data/test_dashboard_sim.html (模拟盘模式)")
    print("  - data/test_dashboard_safe.html (安全等级)")
    print("  - data/test_dashboard_warning.html (警告等级)")
    print("  - data/test_dashboard_danger.html (危险等级)")
    print("  - data/test_dashboard_offline.html (API断开)")
    
    return True


if __name__ == "__main__":
    success = test_enhanced_dashboard()
    sys.exit(0 if success else 1)
