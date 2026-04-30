"""决策快照系统

记录每次决策的完整上下文，用于后续复盘和学习
"""

import json
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, List
import logging

logger = logging.getLogger(__name__)


@dataclass
class DecisionSnapshot:
    """决策快照"""

    # 基本信息
    id: str
    timestamp: int
    symbol: str
    decision: str  # BUY/SELL/HOLD
    conviction: float  # 信心分数 0-100

    # 市场环境
    market_sentiment: str  # 恐贪指数
    vix: float
    macro_environment: str  # FOMC期/关税期/平静期

    # 技术指标
    rsi: float
    adx: float
    macd: float
    volume_trend: str

    # 订单簿
    order_book_imbalance: float
    smart_money_direction: str

    # Polymarket预测
    polymarket_prediction: str
    polymarket_confidence: float

    # 判断理由
    reasoning: str
    target_price: float
    stop_loss: float

    # 验证状态
    verified: bool = False
    result: str = ""  # SUCCESS/FAILURE
    actual_return: float = 0.0

    def to_dict(self):
        """转换为字典"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict):
        """从字典创建"""
        return cls(**data)


class DecisionSnapshotDB:
    """决策快照数据库"""

    def __init__(self, db_path: str = "data/decision_snapshots.db"):
        """初始化数据库"""
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """初始化数据库表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 创建决策快照表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS decision_snapshots (
                id TEXT PRIMARY KEY,
                timestamp INTEGER,
                symbol TEXT,
                decision TEXT,
                conviction REAL,
                market_sentiment TEXT,
                vix REAL,
                macro_environment TEXT,
                rsi REAL,
                adx REAL,
                macd REAL,
                volume_trend TEXT,
                order_book_imbalance REAL,
                smart_money_direction TEXT,
                polymarket_prediction TEXT,
                polymarket_confidence REAL,
                reasoning TEXT,
                target_price REAL,
                stop_loss REAL,
                verified BOOLEAN,
                result TEXT,
                actual_return REAL
            )
        """)

        # 创建索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON decision_snapshots(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_symbol ON decision_snapshots(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_verified ON decision_snapshots(verified)")

        conn.commit()
        conn.close()

    def save_snapshot(self, snapshot: DecisionSnapshot):
        """保存决策快照"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO decision_snapshots
            (id, timestamp, symbol, decision, conviction, market_sentiment, vix,
             macro_environment, rsi, adx, macd, volume_trend, order_book_imbalance,
             smart_money_direction, polymarket_prediction, polymarket_confidence,
             reasoning, target_price, stop_loss, verified, result, actual_return)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            snapshot.id, snapshot.timestamp, snapshot.symbol, snapshot.decision,
            snapshot.conviction, snapshot.market_sentiment, snapshot.vix,
            snapshot.macro_environment, snapshot.rsi, snapshot.adx, snapshot.macd,
            snapshot.volume_trend, snapshot.order_book_imbalance, snapshot.smart_money_direction,
            snapshot.polymarket_prediction, snapshot.polymarket_confidence,
            snapshot.reasoning, snapshot.target_price, snapshot.stop_loss,
            snapshot.verified, snapshot.result, snapshot.actual_return
        ))

        conn.commit()
        conn.close()

        logger.info(f"✅ 决策快照已保存: {snapshot.id}")

    def get_snapshot(self, snapshot_id: str) -> Optional[DecisionSnapshot]:
        """获取决策快照"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM decision_snapshots WHERE id = ?", (snapshot_id,))
        row = cursor.fetchone()

        conn.close()

        if row:
            return self._row_to_snapshot(row)
        return None

    def get_unverified_snapshots(self, hours: int = 24) -> List[DecisionSnapshot]:
        """获取未验证的快照"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 计算24小时前的时间戳
        cutoff_time = int((datetime.now().timestamp() - hours * 3600) * 1000)

        cursor.execute("""
            SELECT * FROM decision_snapshots
            WHERE verified = 0 AND timestamp < ?
            ORDER BY timestamp DESC
        """, (cutoff_time,))

        rows = cursor.fetchall()
        conn.close()

        return [self._row_to_snapshot(row) for row in rows]

    def verify_snapshot(self, snapshot_id: str, result: str, actual_return: float):
        """验证快照"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE decision_snapshots
            SET verified = 1, result = ?, actual_return = ?
            WHERE id = ?
        """, (result, actual_return, snapshot_id))

        conn.commit()
        conn.close()

        logger.info(f"✅ 决策快照已验证: {snapshot_id} -> {result}")

    def get_snapshots_by_symbol(self, symbol: str, limit: int = 100) -> List[DecisionSnapshot]:
        """获取指定币种的快照"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM decision_snapshots
            WHERE symbol = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (symbol, limit))

        rows = cursor.fetchall()
        conn.close()

        return [self._row_to_snapshot(row) for row in rows]

    def get_all_snapshots(self, limit: int = 100) -> List[DecisionSnapshot]:
        """获取所有快照"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM decision_snapshots
            ORDER BY timestamp DESC
            LIMIT ?
        """, (limit,))

        rows = cursor.fetchall()
        conn.close()

        return [self._row_to_snapshot(row) for row in rows]

    def get_statistics(self) -> dict:
        """获取统计信息"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 总数
        cursor.execute("SELECT COUNT(*) FROM decision_snapshots")
        total = cursor.fetchone()[0]

        # 已验证
        cursor.execute("SELECT COUNT(*) FROM decision_snapshots WHERE verified = 1")
        verified = cursor.fetchone()[0]

        # 成功
        cursor.execute("SELECT COUNT(*) FROM decision_snapshots WHERE result = 'SUCCESS'")
        success = cursor.fetchone()[0]

        # 失败
        cursor.execute("SELECT COUNT(*) FROM decision_snapshots WHERE result = 'FAILURE'")
        failure = cursor.fetchone()[0]

        # 胜率
        win_rate = success / verified if verified > 0 else 0

        conn.close()

        return {
            "total": total,
            "verified": verified,
            "success": success,
            "failure": failure,
            "win_rate": win_rate,
        }

    def _row_to_snapshot(self, row) -> DecisionSnapshot:
        """将数据库行转换为DecisionSnapshot"""
        return DecisionSnapshot(
            id=row[0],
            timestamp=row[1],
            symbol=row[2],
            decision=row[3],
            conviction=row[4],
            market_sentiment=row[5],
            vix=row[6],
            macro_environment=row[7],
            rsi=row[8],
            adx=row[9],
            macd=row[10],
            volume_trend=row[11],
            order_book_imbalance=row[12],
            smart_money_direction=row[13],
            polymarket_prediction=row[14],
            polymarket_confidence=row[15],
            reasoning=row[16],
            target_price=row[17],
            stop_loss=row[18],
            verified=bool(row[19]),
            result=row[20],
            actual_return=row[21],
        )


# 测试代码
if __name__ == "__main__":
    import uuid

    # 创建数据库
    db = DecisionSnapshotDB()

    # 创建测试快照
    snapshot = DecisionSnapshot(
        id=str(uuid.uuid4()),
        timestamp=int(datetime.now().timestamp() * 1000),
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

    # 获取统计信息
    stats = db.get_statistics()
    print(f"📊 统计信息: {stats}")

    # 获取未验证的快照
    unverified = db.get_unverified_snapshots(hours=24)
    print(f"📋 未验证快照: {len(unverified)}")

    print("✅ 决策快照系统测试完成")
