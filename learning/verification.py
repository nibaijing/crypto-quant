"""回访验证机制

定期验证历史决策，标记成功/失败
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple
from .decision_snapshot import DecisionSnapshot, DecisionSnapshotDB

logger = logging.getLogger(__name__)


class DecisionVerifier:
    """决策验证器"""

    def __init__(self, price_fetcher=None):
        """初始化验证器"""
        self.price_fetcher = price_fetcher

    def verify_decision(
        self,
        snapshot: DecisionSnapshot,
        current_price: float,
    ) -> Tuple[str, float]:
        """验证决策是否正确

        Args:
            snapshot: 决策快照
            current_price: 当前价格

        Returns:
            (result, actual_return) - 结果和实际收益
        """
        # 计算实际收益
        actual_return = self.calculate_return(snapshot, current_price)

        # 判断成功/失败
        if actual_return > 0:
            result = "SUCCESS"
        else:
            result = "FAILURE"

        logger.info(
            f"🔍 验证决策: {snapshot.id} -> {result} (收益: {actual_return:.2%})"
        )

        return result, actual_return

    def calculate_return(
        self,
        snapshot: DecisionSnapshot,
        current_price: float,
    ) -> float:
        """计算实际收益

        Args:
            snapshot: 决策快照
            current_price: 当前价格

        Returns:
            实际收益率
        """
        if snapshot.decision == "BUY":
            # 做多：当前价格相对于目标价格的收益
            return (current_price - snapshot.target_price) / snapshot.target_price
        elif snapshot.decision == "SELL":
            # 做空：目标价格相对于当前价格的收益
            return (snapshot.target_price - current_price) / snapshot.target_price
        else:
            # 持仓：无收益
            return 0.0

    def should_verify(self, snapshot: DecisionSnapshot, hours: int = 24) -> bool:
        """判断是否应该验证

        Args:
            snapshot: 决策快照
            hours: 验证时间间隔（小时）

        Returns:
            是否应该验证
        """
        # 如果已经验证过，不需要再验证
        if snapshot.verified:
            return False

        # 检查是否超过验证时间
        snapshot_time = datetime.fromtimestamp(snapshot.timestamp / 1000)
        verify_time = snapshot_time + timedelta(hours=hours)
        now = datetime.now()

        return now >= verify_time


class VerificationScheduler:
    """验证调度器"""

    def __init__(
        self,
        db: DecisionSnapshotDB,
        verifier: DecisionVerifier,
        price_fetcher=None,
    ):
        """初始化调度器

        Args:
            db: 决策快照数据库
            verifier: 决策验证器
            price_fetcher: 价格获取器
        """
        self.db = db
        self.verifier = verifier
        self.price_fetcher = price_fetcher

    def verify_old_decisions(self, hours: int = 24):
        """验证24小时前的决策

        Args:
            hours: 验证时间间隔（小时）
        """
        logger.info(f"🔍 开始验证 {hours} 小时前的决策...")

        # 获取未验证的快照
        snapshots = self.db.get_unverified_snapshots(hours=hours)

        logger.info(f"📋 找到 {len(snapshots)} 个未验证的决策")

        # 验证每个快照
        verified_count = 0
        for snapshot in snapshots:
            try:
                # 获取当前价格
                if self.price_fetcher:
                    current_price = self.price_fetcher.get_price(snapshot.symbol)
                else:
                    # 如果没有价格获取器，使用目标价格作为示例
                    logger.warning(f"⚠️ 没有价格获取器，使用目标价格作为示例")
                    current_price = snapshot.target_price * 1.02  # 假设涨了2%

                # 验证决策
                result, actual_return = self.verifier.verify_decision(
                    snapshot, current_price
                )

                # 更新快照
                self.db.verify_snapshot(snapshot.id, result, actual_return)

                verified_count += 1

            except Exception as e:
                logger.error(f"❌ 验证决策失败: {snapshot.id} - {e}")

        logger.info(f"✅ 验证完成: {verified_count}/{len(snapshots)}")

        # 更新统计信息
        stats = self.db.get_statistics()
        logger.info(
            f"📊 统计信息: 总数={stats['total']}, 已验证={stats['verified']}, "
            f"成功={stats['success']}, 失败={stats['failure']}, 胜率={stats['win_rate']:.1%}"
        )

    def verify_single_decision(
        self,
        snapshot_id: str,
        current_price: float,
    ) -> Tuple[str, float]:
        """验证单个决策

        Args:
            snapshot_id: 决策快照ID
            current_price: 当前价格

        Returns:
            (result, actual_return) - 结果和实际收益
        """
        # 获取快照
        snapshot = self.db.get_snapshot(snapshot_id)
        if not snapshot:
            raise ValueError(f"快照不存在: {snapshot_id}")

        # 验证决策
        result, actual_return = self.verifier.verify_decision(snapshot, current_price)

        # 更新快照
        self.db.verify_snapshot(snapshot_id, result, actual_return)

        return result, actual_return


# 测试代码
if __name__ == "__main__":
    import uuid

    # 创建数据库
    db = DecisionSnapshotDB()

    # 创建验证器
    verifier = DecisionVerifier()

    # 创建调度器
    scheduler = VerificationScheduler(db, verifier)

    # 创建测试快照
    snapshot = DecisionSnapshot(
        id=str(uuid.uuid4()),
        timestamp=int((datetime.now() - timedelta(hours=25)).timestamp() * 1000),
        symbol="BTC-USDT",
        decision="BUY",
        conviction=75.0,
        market_sentiment="GREED",
        vix=25.5,
        macro_environment="FOMC",
        rsi=65.0,
        adx=30.0,
        macd=0.5,
        volume_trend="INCREASING",
        order_book_imbalance=0.3,
        smart_money_direction="LONG",
        polymarket_prediction="UP",
        polymarket_confidence=0.7,
        reasoning="RSI超卖+订单簿买盘强+聪明钱方向一致",
        target_price=68000.0,
        stop_loss=65000.0,
    )

    # 保存快照
    db.save_snapshot(snapshot)

    # 验证决策
    result, actual_return = scheduler.verify_single_decision(
        snapshot.id, current_price=69000.0
    )

    print(f"✅ 验证结果: {result}, 收益: {actual_return:.2%}")

    # 验证所有旧决策
    scheduler.verify_old_decisions(hours=24)

    print("✅ 回访验证机制测试完成")
