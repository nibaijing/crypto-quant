"""AI复盘系统

让AI分析失败决策，提取经验教训
"""

import logging
from datetime import datetime
from typing import Optional, Dict, Any
from .decision_snapshot import DecisionSnapshot, DecisionSnapshotDB

logger = logging.getLogger(__name__)


class AIReviewer:
    """AI复盘器"""

    def __init__(self, ai_client=None):
        """初始化复盘器

        Args:
            ai_client: AI客户端（用于调用AI进行复盘）
        """
        self.ai_client = ai_client

    def review_failure(self, snapshot: DecisionSnapshot) -> str:
        """复盘失败决策

        Args:
            snapshot: 决策快照

        Returns:
            复盘结论
        """
        logger.info(f"🔍 开始复盘失败决策: {snapshot.id}")

        # 构建复盘提示
        prompt = self._build_review_prompt(snapshot)

        # 调用AI进行复盘
        if self.ai_client:
            review = self.ai_client.chat(prompt)
        else:
            # 如果没有AI客户端，使用示例复盘
            review = self._generate_sample_review(snapshot)

        logger.info(f"✅ 复盘完成: {snapshot.id}")

        return review

    def _build_review_prompt(self, snapshot: DecisionSnapshot) -> str:
        """构建复盘提示

        Args:
            snapshot: 决策快照

        Returns:
            复盘提示
        """
        prompt = f"""
你24小时前分析{snapshot.symbol}，{snapshot.decision}，信心分数{snapshot.conviction}

当时你的依据是：
- RSI: {snapshot.rsi}
- ADX: {snapshot.adx}
- MACD: {snapshot.macd}
- 市场情绪: {snapshot.market_sentiment}
- VIX: {snapshot.vix}
- 宏观环境: {snapshot.macro_environment}
- 订单簿不平衡: {snapshot.order_book_imbalance}
- 聪明钱方向: {snapshot.smart_money_direction}
- Polymarket预测: {snapshot.polymarket_prediction} (置信度: {snapshot.polymarket_confidence})
- 量价趋势: {snapshot.volume_trend}

判断理由: {snapshot.reasoning}
目标价格: ${snapshot.target_price:.2f}
止损价格: ${snapshot.stop_loss:.2f}

但实际结果: {snapshot.result}, 实际收益: {snapshot.actual_return:.2%}

请复盘：
1. 哪个信号判断失误？
2. 当时忽略了什么？
3. 下次遇到类似场景怎么处理？

请给出详细的复盘结论。
"""

        return prompt

    def _generate_sample_review(self, snapshot: DecisionSnapshot) -> str:
        """生成示例复盘（用于测试）

        Args:
            snapshot: 决策快照

        Returns:
            示例复盘
        """
        review = f"""
## 复盘结论

### 1. 哪个信号判断失误？

主要失误在于过度依赖聪明钱方向信号，忽略了Polymarket的反向信号。

### 2. 当时忽略了什么？

- **Polymarket反向信号**: 当时Polymarket预测{snapshot.polymarket_prediction}，但置信度只有{snapshot.polymarket_confidence:.1%}，市场预期不足以造成拉盘
- **宏观环境**: {snapshot.macro_environment}期间，市场波动性增加，聪明钱信号有滞后性
- **量价背离**: 虽然订单簿买盘强，但实际成交量没有跟上

### 3. 下次遇到类似场景怎么处理？

- **Polymarket反向时谨慎**: 当Polymarket预测与聪明钱方向相反时，应该降低信心分数
- **聪明钱信号折扣**: 在{snapshot.macro_environment}期间，聪明钱信号需要打折处理
- **量价确认**: 必须等待成交量确认，不能只看订单簿
- **降低杠杆**: 在不确定的环境下，降低杠杆倍数

### 经验教训

在{snapshot.macro_environment}期间，当RSI={snapshot.rsi:.1f}、ADX={snapshot.adx:.1f}时，
如果Polymarket预测与聪明钱方向相反，应该谨慎做多，避免被打止损。
"""

        return review

    def extract_lessons(
        self,
        review: str,
        snapshot: DecisionSnapshot,
    ) -> Dict[str, Any]:
        """提取经验教训

        Args:
            review: 复盘内容
            snapshot: 决策快照

        Returns:
            经验教训字典
        """
        logger.info(f"🔍 开始提取经验教训: {snapshot.id}")

        # 构建提取提示
        prompt = self._build_extraction_prompt(review, snapshot)

        # 调用AI提取经验
        if self.ai_client:
            lessons = self.ai_client.chat(prompt)
        else:
            # 如果没有AI客户端，使用示例提取
            lessons = self._extract_sample_lessons(snapshot)

        logger.info(f"✅ 经验教训提取完成: {snapshot.id}")

        return lessons

    def _build_extraction_prompt(
        self,
        review: str,
        snapshot: DecisionSnapshot,
    ) -> str:
        """构建提取提示

        Args:
            review: 复盘内容
            snapshot: 决策快照

        Returns:
            提取提示
        """
        prompt = f"""
从以下复盘内容中提取经验教训，格式化为JSON：

复盘内容:
{review}

决策快照:
- 币种: {snapshot.symbol}
- RSI: {snapshot.rsi} (区间: {self._get_rsi_range(snapshot.rsi)})
- ADX: {snapshot.adx} (区间: {self._get_adx_range(snapshot.adx)})
- 宏观环境: {snapshot.macro_environment}
- 失败原因: {snapshot.result}

请提取：
1. 失败原因 (failure_reason)
2. 教训 (lesson)
3. 下次处理方式 (next_action)

返回JSON格式：
{{
  "failure_reason": "...",
  "lesson": "...",
  "next_action": "..."
}}
"""

        return prompt

    def _extract_sample_lessons(
        self,
        snapshot: DecisionSnapshot,
    ) -> Dict[str, Any]:
        """提取示例经验教训（用于测试）

        Args:
            snapshot: 决策快照

        Returns:
            示例经验教训
        """
        lessons = {
            "failure_reason": f"忽略了Polymarket反向信号，在{snapshot.macro_environment}期间过度依赖聪明钱方向",
            "lesson": f"在{snapshot.macro_environment}期间，当RSI={snapshot.rsi:.1f}、ADX={snapshot.adx:.1f}时，如果Polymarket预测与聪明钱方向相反，应该谨慎做多",
            "next_action": "下次遇到Polymarket反向时，降低信心分数20%，等待成交量确认后再开仓",
        }

        return lessons

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


class ReviewScheduler:
    """复盘调度器"""

    def __init__(
        self,
        db: DecisionSnapshotDB,
        reviewer: AIReviewer,
    ):
        """初始化调度器

        Args:
            db: 决策快照数据库
            reviewer: AI复盘器
        """
        self.db = db
        self.reviewer = reviewer

    def review_failed_decisions(self):
        """复盘所有失败的决策"""
        logger.info("🔍 开始复盘失败的决策...")

        # 获取所有已验证的快照
        all_snapshots = self.db.get_all_snapshots(limit=1000)

        # 筛选失败的决策
        failed_snapshots = [
            s for s in all_snapshots if s.verified and s.result == "FAILURE"
        ]

        logger.info(f"📋 找到 {len(failed_snapshots)} 个失败的决策")

        # 复盘每个失败的决策
        reviewed_count = 0
        for snapshot in failed_snapshots:
            try:
                # 复盘决策
                review = self.reviewer.review_failure(snapshot)

                # 提取经验教训
                lessons = self.reviewer.extract_lessons(review, snapshot)

                logger.info(f"✅ 复盘完成: {snapshot.id}")
                reviewed_count += 1

            except Exception as e:
                logger.error(f"❌ 复盘失败: {snapshot.id} - {e}")

        logger.info(f"✅ 复盘完成: {reviewed_count}/{len(failed_snapshots)}")


# 测试代码
if __name__ == "__main__":
    import uuid

    # 创建数据库
    db = DecisionSnapshotDB()

    # 创建复盘器
    reviewer = AIReviewer()

    # 创建调度器
    scheduler = ReviewScheduler(db, reviewer)

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
        verified=True,
        result="FAILURE",
        actual_return=-0.05,
    )

    # 保存快照
    db.save_snapshot(snapshot)

    # 复盘失败决策
    review = reviewer.review_failure(snapshot)
    print(f"📝 复盘结论:\n{review}")

    # 提取经验教训
    lessons = reviewer.extract_lessons(review, snapshot)
    print(f"📚 经验教训:\n{lessons}")

    print("✅ AI复盘系统测试完成")
