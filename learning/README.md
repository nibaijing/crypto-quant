# AI自学习量化交易系统

让AI从自己的错误中学习，不断成长，越跑越聪明！

## 🎯 核心思想

**传统量化**: 人写规则 → 静态系统
**AI自学习**: 自己积累经验 → 动态成长

## 🔄 核心闭环

1. **决策留痕** - 记录每次决策的完整上下文
2. **24小时后回访** - 自动验证判断是否正确
3. **AI自己复盘** - 让AI分析为什么对/错
4. **经验库积累** - 归档经验，带上标签
5. **智能调取经验** - 下次决策时自动调取相关经验

## 📊 系统架构

```
市场数据 → 决策引擎 → 决策快照 → 24小时回访 → AI复盘 → 经验库
                                              ↓                    ↓
                                         场景识别 ← 智能检索 ← 反馈机制
                                              ↓
                                         改进决策
```

## 🚀 快速开始

### 1. 安装依赖

```bash
cd /home/ni/crypto_quant
pip install -r requirements.txt
```

### 2. 测试系统

```bash
python test_self_learning.py
```

### 3. 使用系统

```python
from learning.self_learning_system import SelfLearningSystem

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

# 验证旧决策
system.verify_old_decisions(hours=24)

# 获取相关经验
experiences = system.format_experiences_for_decision(
    symbol="BTC-USDT",
    rsi=65.0,
    adx=30.0,
    macro_environment="FOMC",
    top_k=5,
)

print(experiences)
```

## 📁 文件结构

```
/home/ni/crypto_quant/
├── learning/
│   ├── __init__.py                    # 模块初始化
│   ├── decision_snapshot.py           # 决策快照系统
│   ├── verification.py                 # 回访验证机制
│   ├── ai_reviewer.py                 # AI复盘系统
│   ├── experience_db.py               # 经验库系统
│   └── self_learning_system.py        # 集成系统
├── data/
│   ├── decision_snapshots.db         # 决策快照数据库
│   └── experience_lessons.db          # 经验数据库
└── test_self_learning.py              # 测试脚本
```

## 🎯 成长路径

- **第1周**: 按基础逻辑判断
- **第4周**: 积累几百条经验，能调取经验
- **第3个月**: 经验库丰富，胜率指数级提升
- **第6个月**: 成为"老手"

**理论上，操作越多，胜率越高。**

## 💡 实际效果

根据原作者的经验：
- 本金：1000u
- 运行时间：半个多月
- 当前：1600+u
- 收益率：+60%

## 📊 数据库设计

### decision_snapshots 表

```sql
CREATE TABLE decision_snapshots (
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
);
```

### experience_lessons 表

```sql
CREATE TABLE experience_lessons (
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
);
```

## 🔧 下一步计划

### P0 (必须实现)
- ✅ 决策快照系统
- ✅ 回访验证机制
- ✅ AI复盘系统
- ✅ 经验库

### P1 (重要)
- ⏳ 智能经验检索
- ⏳ 场景识别
- ⏳ 集成到决策引擎

### P2 (优化)
- ⏳ 反馈机制
- ⏳ 权重调整
- ⏳ 自动化调度

## 🎓 学习要点

1. **每次错误都不是浪费** - 而是在喂给未来的判断力
2. **不是忍受规则，而是训练系统** - AI的每个想法都在改进系统
3. **场景识别很重要** - 不是机械套用经验
4. **经验库要丰富** - 越跑越聪明
5. **反馈机制要有效** - 让AI改进规则

## 📝 文档

- [学习笔记](AI_SELF_LEARNING_NOTES.md)
- [实现计划](AI_SELF_LEARNING_IMPLEMENTATION_PLAN.md)
- [总结文档](AI_SELF_LEARNING_SUMMARY.md)

## 📄 许可证

MIT License

## 🤝 贡献

欢迎提交Issue和Pull Request！

---

**让AI从自己的错误中学习，不断成长！** 🚀
