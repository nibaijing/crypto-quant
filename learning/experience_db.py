"""经验库系统

存储和检索历史经验，用于改进决策
"""

import json
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
import logging

logger = logging.getLogger(__name__)


@dataclass
class ExperienceLesson:
    """经验教训"""

    # 基本信息
    id: str
    timestamp: int

    # 场景标签
    symbol: str
    rsi_range: str
    adx_range: str
    macro_environment: str

    # 失败原因
    failure_reason: str

    # 教训
    lesson: str

    # 下次处理方式
    next_action: str

    # 使用统计
    usage_count: int = 0
    success_count: int = 0
    success_rate: float = 0.0

    def to_dict(self):
        """转换为字典"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict):
        """从字典创建"""
        return cls(**data)


class ExperienceDB:
    """经验数据库"""

    def __init__(self, db_path: str = "data/experience_lessons.db"):
        """初始化数据库"""
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """初始化数据库表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 创建经验教训表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS experience_lessons (
                id TEXT PRIMARY KEY,
                timestamp INTEGER,
                symbol TEXT,
                rsi_range TEXT,
                adx_range TEXT,
                macro_environment TEXT,
                failure_reason TEXT,
                lesson TEXT,
                next_action TEXT,
                usage_count INTEGER,
                success_count INTEGER,
                success_rate REAL
            )
        """)

        # 创建索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_symbol ON experience_lessons(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rsi_range ON experience_lessons(rsi_range)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_adx_range ON experience_lessons(adx_range)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_macro ON experience_lessons(macro_environment)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON experience_lessons(timestamp)")

        conn.commit()
        conn.close()

    def save_lesson(self, lesson: ExperienceLesson):
        """保存经验教训"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO experience_lessons
            (id, timestamp, symbol, rsi_range, adx_range, macro_environment,
             failure_reason, lesson, next_action, usage_count, success_count, success_rate)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            lesson.id, lesson.timestamp, lesson.symbol, lesson.rsi_range,
            lesson.adx_range, lesson.macro_environment, lesson.failure_reason,
            lesson.lesson, lesson.next_action, lesson.usage_count,
            lesson.success_count, lesson.success_rate
        ))

        conn.commit()
        conn.close()

        logger.info(f"✅ 经验教训已保存: {lesson.id}")

    def get_lesson(self, lesson_id: str) -> Optional[ExperienceLesson]:
        """获取经验教训"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM experience_lessons WHERE id = ?", (lesson_id,))
        row = cursor.fetchone()

        conn.close()

        if row:
            return self._row_to_lesson(row)
        return None

    def search_relevant_lessons(
        self,
        symbol: str,
        rsi_range: str,
        adx_range: str,
        macro_environment: str,
        top_k: int = 5,
    ) -> List[ExperienceLesson]:
        """搜索相关经验

        Args:
            symbol: 币种
            rsi_range: RSI区间
            adx_range: ADX区间
            macro_environment: 宏观环境
            top_k: 返回数量

        Returns:
            相关经验列表
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 构建查询条件
        conditions = []
        params = []

        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)

        if rsi_range:
            conditions.append("rsi_range = ?")
            params.append(rsi_range)

        if adx_range:
            conditions.append("adx_range = ?")
            params.append(adx_range)

        if macro_environment:
            conditions.append("macro_environment = ?")
            params.append(macro_environment)

        # 如果没有条件，返回所有经验
        if not conditions:
            query = "SELECT * FROM experience_lessons ORDER BY timestamp DESC LIMIT ?"
            params.append(top_k)
        else:
            query = f"SELECT * FROM experience_lessons WHERE {' AND '.join(conditions)} ORDER BY timestamp DESC LIMIT ?"
            params.append(top_k)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        return [self._row_to_lesson(row) for row in rows]

    def update_lesson_stats(self, lesson_id: str, success: bool):
        """更新经验统计

        Args:
            lesson_id: 经验ID
            success: 是否成功
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 获取当前统计
        cursor.execute("SELECT usage_count, success_count FROM experience_lessons WHERE id = ?", (lesson_id,))
        row = cursor.fetchone()

        if row:
            usage_count, success_count = row
            usage_count += 1
            if success:
                success_count += 1

            # 计算成功率
            success_rate = success_count / usage_count if usage_count > 0 else 0.0

            # 更新统计
            cursor.execute("""
                UPDATE experience_lessons
                SET usage_count = ?, success_count = ?, success_rate = ?
                WHERE id = ?
            """, (usage_count, success_count, success_rate, lesson_id))

            conn.commit()

        conn.close()

        logger.info(f"✅ 经验统计已更新: {lesson_id}")

    def get_all_lessons(self, limit: int = 100) -> List[ExperienceLesson]:
        """获取所有经验"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM experience_lessons
            ORDER BY timestamp DESC
            LIMIT ?
        """, (limit,))

        rows = cursor.fetchall()
        conn.close()

        return [self._row_to_lesson(row) for row in rows]

    def get_statistics(self) -> dict:
        """获取统计信息"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 总数
        cursor.execute("SELECT COUNT(*) FROM experience_lessons")
        total = cursor.fetchone()[0]

        # 总使用次数
        cursor.execute("SELECT SUM(usage_count) FROM experience_lessons")
        total_usage = cursor.fetchone()[0] or 0

        # 平均成功率
        cursor.execute("SELECT AVG(success_rate) FROM experience_lessons WHERE usage_count > 0")
        avg_success_rate = cursor.fetchone()[0] or 0.0

        conn.close()

        return {
            "total": total,
            "total_usage": total_usage,
            "avg_success_rate": avg_success_rate,
        }

    def _row_to_lesson(self, row) -> ExperienceLesson:
        """将数据库行转换为ExperienceLesson"""
        return ExperienceLesson(
            id=row[0],
            timestamp=row[1],
            symbol=row[2],
            rsi_range=row[3],
            adx_range=row[4],
            macro_environment=row[5],
            failure_reason=row[6],
            lesson=row[7],
            next_action=row[8],
            usage_count=row[9],
            success_count=row[10],
            success_rate=row[11],
        )


class ExperienceRetriever:
    """经验检索器"""

    def __init__(self, db: ExperienceDB):
        """初始化检索器

        Args:
            db: 经验数据库
        """
        self.db = db

    def retrieve_relevant_experiences(
        self,
        symbol: str,
        rsi: float,
        adx: float,
        macro_environment: str,
        top_k: int = 5,
    ) -> List[ExperienceLesson]:
        """检索相关经验

        Args:
            symbol: 币种
            rsi: RSI值
            adx: ADX值
            macro_environment: 宏观环境
            top_k: 返回数量

        Returns:
            相关经验列表
        """
        # 转换为区间
        rsi_range = self._get_rsi_range(rsi)
        adx_range = self._get_adx_range(adx)

        # 搜索相关经验
        lessons = self.db.search_relevant_lessons(
            symbol=symbol,
            rsi_range=rsi_range,
            adx_range=adx_range,
            macro_environment=macro_environment,
            top_k=top_k,
        )

        logger.info(f"🔍 检索到 {len(lessons)} 条相关经验")

        return lessons

    def format_experiences_for_ai(
        self,
        lessons: List[ExperienceLesson],
    ) -> str:
        """格式化经验供AI使用

        Args:
            lessons: 经验列表

        Returns:
            格式化的经验文本
        """
        if not lessons:
            return "暂无相关经验"

        formatted = []
        for i, lesson in enumerate(lessons, 1):
            days_ago = self._days_ago(lesson.timestamp)
            formatted.append(f"""
经验{i}（{days_ago}天前）：
- 场景: {lesson.symbol}, RSI={lesson.rsi_range}, ADX={lesson.adx_range}
- 宏观: {lesson.macro_environment}
- 失败原因: {lesson.failure_reason}
- 教训: {lesson.lesson}
- 下次处理: {lesson.next_action}
- 使用次数: {lesson.usage_count}, 成功率: {lesson.success_rate:.1%}
""")

        return "\n".join(formatted)

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

    def _days_ago(self, timestamp: int) -> int:
        """计算距离今天多少天

        Args:
            timestamp: 时间戳（毫秒）

        Returns:
            天数
        """
        lesson_time = datetime.fromtimestamp(timestamp / 1000)
        now = datetime.now()
        delta = now - lesson_time
        return delta.days


# 测试代码
if __name__ == "__main__":
    import uuid

    # 创建数据库
    db = ExperienceDB()

    # 创建测试经验
    lesson = ExperienceLesson(
        id=str(uuid.uuid4()),
        timestamp=int(datetime.now().timestamp() * 1000),
        symbol="BTC-USDT",
        rsi_range="NEUTRAL",
        adx_range="HIGH",
        macro_environment="FOMC",
        failure_reason="忽略了Polymarket反向信号",
        lesson="在FOMC期间，当RSI=65、ADX=30时，如果Polymarket预测与聪明钱方向相反，应该谨慎做多",
        next_action="下次遇到Polymarket反向时，降低信心分数20%",
    )

    # 保存经验
    db.save_lesson(lesson)

    # 获取统计信息
    stats = db.get_statistics()
    print(f"📊 统计信息: {stats}")

    # 检索相关经验
    retriever = ExperienceRetriever(db)
    lessons = retriever.retrieve_relevant_experiences(
        symbol="BTC-USDT",
        rsi=65.0,
        adx=30.0,
        macro_environment="FOMC",
        top_k=5,
    )

    print(f"🔍 检索到 {len(lessons)} 条相关经验")

    # 格式化经验
    formatted = retriever.format_experiences_for_ai(lessons)
    print(f"📝 格式化经验:\n{formatted}")

    print("✅ 经验库系统测试完成")
