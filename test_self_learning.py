#!/usr/bin/env python3
"""测试AI自学习系统"""

import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from learning.self_learning_system import SelfLearningSystem


def test_self_learning_system():
    """测试AI自学习系统"""
    print("=" * 60)
    print("测试AI自学习系统")
    print("=" * 60)

    # 创建自学习系统
    print("\n1. 创建自学习系统...")
    system = SelfLearningSystem()
    print("✅ 自学习系统创建完成")

    # 记录决策
    print("\n2. 记录决策...")
    decision_id = system.record_decision(
        symbol="BTC-USDT",
        decision="BUY",
        conviction=75.0,
        market_data={
            "market_sentiment": "GREED",
            "vix": 25.5,
            "macro_environment": "FOMC",
            "rsi": 65.0,
            "adx": 30.0,
            "macd": 0.5,
            "volume_trend": "INCREASING",
            "order_book_imbalance": 0.3,
            "smart_money_direction": "LONG",
            "polymarket_prediction": "UP",
            "polymarket_confidence": 0.7,
        },
        reasoning="RSI超卖+订单簿买盘强+聪明钱方向一致",
        target_price=68000.0,
        stop_loss=65000.0,
    )
    print(f"✅ 决策已记录: {decision_id}")

    # 记录多个决策
    print("\n3. 记录多个决策...")
    for i in range(5):
        system.record_decision(
            symbol="BTC-USDT",
            decision="BUY" if i % 2 == 0 else "SELL",
            conviction=70.0 + i * 2,
            market_data={
                "market_sentiment": "GREED" if i % 2 == 0 else "FEAR",
                "vix": 20.0 + i,
                "macro_environment": "FOMC" if i % 2 == 0 else "NORMAL",
                "rsi": 60.0 + i * 5,
                "adx": 25.0 + i,
                "macd": 0.5,
                "volume_trend": "INCREASING",
                "order_book_imbalance": 0.3,
                "smart_money_direction": "LONG" if i % 2 == 0 else "SHORT",
                "polymarket_prediction": "UP" if i % 2 == 0 else "DOWN",
                "polymarket_confidence": 0.7,
            },
            reasoning=f"测试决策 {i}",
            target_price=68000.0 + i * 100,
            stop_loss=65000.0,
        )
    print("✅ 多个决策已记录")

    # 获取统计信息
    print("\n4. 获取统计信息...")
    stats = system.get_statistics()
    print(f"📊 决策统计: {stats['decisions']}")
    print(f"📊 经验统计: {stats['experiences']}")

    # 获取相关经验
    print("\n5. 获取相关经验...")
    experiences = system.format_experiences_for_decision(
        symbol="BTC-USDT",
        rsi=65.0,
        adx=30.0,
        macro_environment="FOMC",
        top_k=5,
    )
    print(f"📝 相关经验:\n{experiences}")

    print("\n" + "=" * 60)
    print("✅ AI自学习系统测试完成")
    print("=" * 60)


if __name__ == "__main__":
    test_self_learning_system()
