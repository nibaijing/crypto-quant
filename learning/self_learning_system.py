"""AI自学习系统集成模块

将决策快照、回访验证、AI复盘、经验库整合在一起
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from .decision_snapshot import DecisionSnapshot, DecisionSnapshotDB
from .verification import DecisionVerifier, VerificationScheduler
from .ai_reviewer import AIReviewer, ReviewScheduler
from .experience_db import ExperienceLesson, ExperienceDB, ExperienceRetriever

logger = logging.getLogger(__name__)


class SelfLearningSystem:
    """AI自学习系统"""

    def __init__(
        self,
        decision_db_path: str = "data/decision_snapshots.db",
        experience_db_path: str = "data/experience_lessons.db",
        ai_client=None,
        price_fetcher=None,
    ):
        """初始化自学习系统

        Args:
            decision_db_path: 决策快照数据库路径
            experience_db_path: 经验数据库路径
            ai_client: AI客户端
            price_fetcher: 价格获取器
        """
        # 初始化数据库
        self.decision_db = DecisionSnapshotDB(decision_db_path)
        self.experience_db = ExperienceDB(experience_db_path)

        # 初始化组件
        self.verifier = DecisionVerifier(price_fetcher)
        self.verification_scheduler = VerificationScheduler(
            self.decision_db, self.verifier, price_fetcher
        )

        self.reviewer = AIReviewer(ai_client)
        self.review_scheduler = ReviewScheduler(self.decision_db, self.reviewer)

        self.experience_retriever = ExperienceRetriever(self.experience_db)

        logger.info("✅ AI自学习系统初始化完成")

    def record_decision(
        self,
        symbol: str,
        decision: str,
        conviction: float,
        market_data: Dict[str, Any],
        reasoning: str,
        target_price: float,
        stop_loss: float,
    ) -> str:
        """记录决策快照

        Args:
            symbol: 币种
            decision: 决策 (BUY/SELL/HOLD)
            conviction: 信心分数
            market_data: 市场数据
            reasoning: 判断理由
            target_price: 目标价格
            stop_loss: 止损价格

        Returns:
            决策快照ID
        """
        # 创建决策快照
        snapshot = DecisionSnapshot(
            id=str(uuid.uuid4()),
            timestamp=int(datetime.now().timestamp() * 1000),
            symbol=symbol,
            decision=decision,
            conviction=conviction,
            market_sentiment=market_data.get("market_sentiment", "NEUTRAL"),
            vix=market_data.get("vix", 0.0),
            macro_environment=market_data.get("macro_environment", "NORMAL"),
            rsi=market_data.get("rsi", 50.0),
            adx=market_data.get("adx", 0.0),
            macd=market_data.get("macd", 0.0),
            volume_trend=market_data.get("volume_trend", "NEUTRAL"),
            order_book_imbalance=market_data.get("order_book_imbalance", 0.0),
            smart_money_direction=market_data.get("smart_money_direction", "NEUTRAL"),
            polymarket_prediction=market_data.get("polymarket_prediction", "NEUTRAL"),
            polymarket_confidence=market_data.get("polymarket_confidence", 0.0),
            reasoning=reasoning,
            target_price=target_price,
            stop_loss=stop_loss,
        )

        # 保存快照
        self.decision_db.save_snapshot(snapshot)

        logger.info(f"✅ 决策已记录: {snapshot.id} -> {decision}")

        return snapshot.id

    def verify_old_decisions(self, hours: int = 24):
        """验证旧决策

        Args:
            hours: 验证时间间隔（小时）
        """
        logger.info(f"🔍 开始验证 {hours} 小时前的决策...")

        # 验证决策
        self.verification_scheduler.verify_old_decisions(hours=hours)

        # 复盘失败的决策
        self.review_failed_decisions()

        logger.info("✅ 验证和复盘完成")

    def review_failed_decisions(self):
        """复盘失败的决策"""
        logger.info("🔍 开始复盘失败的决策...")

        # 获取所有已验证的快照
        all_snapshots = self.decision_db.get_all_snapshots(limit=1000)

        # 筛选失败的决策
        failed_snapshots = [
            s for s in all_snapshots if s.verified and s.result == "FAILURE"
        ]

        logger.info(f"📋 找到 {len(failed_snapshots)} 个失败的决策")

        # 复盘每个失败的决策
        for snapshot in failed_snapshots:
            try:
                # 复盘决策
                review = self.reviewer.review_failure(snapshot)

                # 提取经验教训
                lessons = self.reviewer.extract_lessons(review, snapshot)

                # 保存经验教训
                self._save_lessons_from_review(lessons, snapshot)

                logger.info(f"✅ 复盘完成: {snapshot.id}")

            except Exception as e:
                logger.error(f"❌ 复盘失败: {snapshot.id} - {e}")

        logger.info("✅ 复盘完成")

    def _save_lessons_from_review(
        self,
        lessons: Dict[str, Any],
        snapshot: DecisionSnapshot,
    ):
        """从复盘内容保存经验教训

        Args:
            lessons: 经验教训字典
            snapshot: 决策快照
        """
        # 创建经验教训
        lesson = ExperienceLesson(
            id=str(uuid.uuid4()),
            timestamp=int(datetime.now().timestamp() * 1000),
            symbol=snapshot.symbol,
            rsi_range=self._get_rsi_range(snapshot.rsi),
            adx_range=self._get_adx_range(snapshot.adx),
            macro_environment=snapshot.macro_environment,
            failure_reason=lessons.get("failure_reason", ""),
            lesson=lessons.get("lesson", ""),
            next_action=lessons.get("next_action", ""),
        )

        # 保存经验
        self.experience_db.save_lesson(lesson)

        logger.info(f"✅ 经验教训已保存: {lesson.id}")

    def get_relevant_experiences(
        self,
        symbol: str,
        rsi: float,
        adx: float,
        macro_environment: str,
        top_k: int = 5,
    ) -> List[ExperienceLesson]:
        """获取相关经验

        Args:
            symbol: 币种
            rsi: RSI值
            adx: ADX值
            macro_environment: 宏观环境
            top_k: 返回数量

        Returns:
            相关经验列表
        """
        return self.experience_retriever.retrieve_relevant_experiences(
            symbol=symbol,
            rsi=rsi,
            adx=adx,
            macro_environment=macro_environment,
            top_k=top_k,
        )

    def format_experiences_for_decision(
        self,
        symbol: str,
        rsi: float,
        adx: float,
        macro_environment: str,
        top_k: int = 5,
    ) -> str:
        """格式化经验供决策使用

        Args:
            symbol: 币种
            rsi: RSI值
            adx: ADX值
            macro_environment: 宏观环境
            top_k: 返回数量

        Returns:
            格式化的经验文本
        """
        # 获取相关经验
        lessons = self.get_relevant_experiences(
            symbol=symbol,
            rsi=rsi,
            adx=adx,
            macro_environment=macro_environment,
            top_k=top_k,
        )

        # 格式化经验
        return self.experience_retriever.format_experiences_for_ai(lessons)

    def update_lesson_stats(self, lesson_id: str, success: bool):
        """更新经验统计

        Args:
            lesson_id: 经验ID
            success: 是否成功
        """
        self.experience_db.update_lesson_stats(lesson_id, success)

    def get_statistics(self) -> Dict[str, Any]:
        """获取统计信息

        Returns:
            统计信息字典
        """
        decision_stats = self.decision_db.get_statistics()
        experience_stats = self.experience_db.get_statistics()

        return {
            "decisions": decision_stats,
            "experiences": experience_stats,
        }

    def _get_rsi_range(self, rsi: float) -> str:
        """获取RSI区间

        Args:
            rsi: RSI值

        Returns:
            RSI区间
        """
        if rsi > 70:
            return "OVERBOUGHT"
        elif rsi < 30:
            return "OVERSOLD"
        else:
            return "NEUTRAL"

    def _get_adx_range(self, adx: float) -> str:
        """获取ADX区间

        Args:
            adx: ADX值

        Returns:
            ADX区间
        """
        if adx > 25:
            return "HIGH"
        elif adx < 20:
            return "LOW"
        else:
            return "MEDIUM"


# 测试代码
if __name__ == "__main__":
    # 创建自学习系统
    system = SelfLearningSystem()

    # 记录决策
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

    # 获取统计信息
    stats = system.get_statistics()
    print(f"📊 统计信息: {stats}")

    # 获取相关经验
    experiences = system.format_experiences_for_decision(
        symbol="BTC-USDT",
        rsi=65.0,
        adx=30.0,
        macro_environment="FOMC",
        top_k=5,
    )

    print(f"📝 相关经验:\n{experiences}")

    print("✅ AI自学习系统集成测试完成")
