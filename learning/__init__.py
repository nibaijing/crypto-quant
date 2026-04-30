"""AI自学习系统

让AI从自己的错误中学习，不断成长

核心功能:
1. 决策快照 - 记录每次决策的完整上下文
2. 回访验证 - 定期验证历史决策
3. AI复盘 - 让AI分析失败原因
4. 经验库 - 存储和检索历史经验
5. 智能调取 - 下次决策时自动调取经验
"""

from .decision_snapshot import DecisionSnapshot, DecisionSnapshotDB
from .verification import DecisionVerifier, VerificationScheduler
from .ai_reviewer import AIReviewer, ReviewScheduler
from .experience_db import ExperienceLesson, ExperienceDB, ExperienceRetriever
from .self_learning_system import SelfLearningSystem

__all__ = [
    "DecisionSnapshot",
    "DecisionSnapshotDB",
    "DecisionVerifier",
    "VerificationScheduler",
    "AIReviewer",
    "ReviewScheduler",
    "ExperienceLesson",
    "ExperienceDB",
    "ExperienceRetriever",
    "SelfLearningSystem",
]

__version__ = "0.1.0"
