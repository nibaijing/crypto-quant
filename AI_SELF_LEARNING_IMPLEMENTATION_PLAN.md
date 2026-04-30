# AI自学习量化交易系统 - 实现计划

## 🎯 目标

将AI自学习机制集成到我们的量化交易系统，让系统从自己的错误中学习，不断成长。

## 📋 实现步骤

### 阶段1: 决策快照系统

#### 1.1 设计决策快照数据结构

```python
@dataclass
class DecisionSnapshot:
    """决策快照"""
    # 基本信息
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
```

#### 1.2 实现决策快照存储

```python
class DecisionSnapshotDB:
    """决策快照数据库"""

    def save_snapshot(self, snapshot: DecisionSnapshot):
        """保存决策快照"""
        pass

    def get_unverified_snapshots(self, hours: int = 24):
        """获取未验证的快照"""
        pass

    def verify_snapshot(self, snapshot_id: str, result: str, actual_return: float):
        """验证快照"""
        pass
```

### 阶段2: 回访验证机制

#### 2.1 实现回访验证器

```python
class DecisionVerifier:
    """决策验证器"""

    def verify_decision(self, snapshot: DecisionSnapshot, current_price: float):
        """验证决策是否正确"""
        # 计算实际收益
        actual_return = self.calculate_return(snapshot, current_price)

        # 判断成功/失败
        if actual_return > 0:
            result = "SUCCESS"
        else:
            result = "FAILURE"

        # 更新快照
        snapshot.verified = True
        snapshot.result = result
        snapshot.actual_return = actual_return

        return result, actual_return

    def calculate_return(self, snapshot: DecisionSnapshot, current_price: float):
        """计算实际收益"""
        if snapshot.decision == "BUY":
            return (current_price - snapshot.target_price) / snapshot.target_price
        elif snapshot.decision == "SELL":
            return (snapshot.target_price - current_price) / snapshot.target_price
        else:
            return 0.0
```

#### 2.2 定期验证任务

```python
class VerificationScheduler:
    """验证调度器"""

    def verify_old_decisions(self):
        """验证24小时前的决策"""
        # 获取未验证的快照
        snapshots = self.db.get_unverified_snapshots(hours=24)

        # 验证每个快照
        for snapshot in snapshots:
            current_price = self.get_current_price(snapshot.symbol)
            result, actual_return = self.verifier.verify_decision(snapshot, current_price)
            self.db.verify_snapshot(snapshot.id, result, actual_return)

            # 如果失败，触发AI复盘
            if result == "FAILURE":
                self.trigger_ai_review(snapshot)
```

### 阶段3: AI复盘系统

#### 3.1 实现AI复盘器

```python
class AIReviewer:
    """AI复盘器"""

    def review_failure(self, snapshot: DecisionSnapshot):
        """复盘失败决策"""
        prompt = f"""
你24小时前分析{snapshot.symbol}，{snapshot.decision}，信心分数{snapshot.conviction}

当时你的依据是：
- RSI: {snapshot.rsi}
- ADX: {snapshot.adx}
- 市场情绪: {snapshot.market_sentiment}
- 宏观环境: {snapshot.macro_environment}
- 订单簿: {snapshot.order_book_imbalance}
- 聪明钱方向: {snapshot.smart_money_direction}
- Polymarket预测: {snapshot.polymarket_prediction}

判断理由: {snapshot.reasoning}

但实际结果: {snapshot.result}, 实际收益: {snapshot.actual_return:.2%}

请复盘：
1. 哪个信号判断失误？
2. 当时忽略了什么？
3. 下次遇到类似场景怎么处理？
"""

        # 调用AI进行复盘
        review = self.ai_client.chat(prompt)

        return review

    def extract_lessons(self, review: str, snapshot: DecisionSnapshot):
        """提取经验教训"""
        prompt = f"""
从以下复盘内容中提取经验教训，格式化为JSON：

复盘内容:
{review}

决策快照:
- 币种: {snapshot.symbol}
- RSI区间: {self.get_rsi_range(snapshot.rsi)}
- ADX区间: {self.get_adx_range(snapshot.adx)}
- 宏观环境: {snapshot.macro_environment}
- 失败原因: {snapshot.result}

请提取：
1. 失败原因
2. 教训
3. 下次处理方式
"""

        # 调用AI提取经验
        lessons = self.ai_client.chat(prompt)

        return lessons
```

#### 3.2 存储经验教训

```python
@dataclass
class ExperienceLesson:
    """经验教训"""
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

    # 时间戳
    timestamp: int

    # 使用次数
    usage_count: int = 0

    # 成功率
    success_rate: float = 0.0


class ExperienceDB:
    """经验数据库"""

    def save_lesson(self, lesson: ExperienceLesson):
        """保存经验教训"""
        pass

    def search_relevant_lessons(self, snapshot: DecisionSnapshot, top_k: int = 5):
        """搜索相关经验"""
        pass

    def update_lesson_stats(self, lesson_id: str, success: bool):
        """更新经验统计"""
        pass
```

### 阶段4: 经验库集成

#### 4.1 智能经验检索

```python
class ExperienceRetriever:
    """经验检索器"""

    def retrieve_relevant_experiences(self, snapshot: DecisionSnapshot, top_k: int = 5):
        """检索相关经验"""
        # 构建查询向量
        query = {
            "symbol": snapshot.symbol,
            "rsi_range": self.get_rsi_range(snapshot.rsi),
            "adx_range": self.get_adx_range(snapshot.adx),
            "macro_environment": snapshot.macro_environment,
        }

        # 搜索相关经验
        lessons = self.experience_db.search_relevant_lessons(snapshot, top_k)

        # 按相关性排序
        sorted_lessons = self.sort_by_relevance(lessons, query)

        return sorted_lessons[:top_k]

    def format_experiences_for_ai(self, lessons: List[ExperienceLesson]):
        """格式化经验供AI使用"""
        formatted = []
        for i, lesson in enumerate(lessons, 1):
            formatted.append(f"""
经验{i}（{self.days_ago(lesson.timestamp)}天前）：
- 场景: {lesson.symbol}, RSI={lesson.rsi_range}, ADX={lesson.adx_range}
- 宏观: {lesson.macro_environment}
- 失败原因: {lesson.failure_reason}
- 教训: {lesson.lesson}
- 下次处理: {lesson.next_action}
- 使用次数: {lesson.usage_count}, 成功率: {lesson.success_rate:.1%}
""")

        return "\n".join(formatted)
```

#### 4.2 集成到决策系统

```python
class EnhancedDecisionEngine:
    """增强版决策引擎"""

    def make_decision(self, snapshot: DecisionSnapshot):
        """做出决策（带经验）"""
        # 检索相关经验
        relevant_experiences = self.experience_retriever.retrieve_relevant_experiences(snapshot)

        # 格式化经验
        experience_context = self.experience_retriever.format_experiences_for_ai(relevant_experiences)

        # 构建决策提示
        prompt = f"""
现在分析{snapshot.symbol}，参考你的历史经验：

{experience_context}

当前市场环境：
- RSI: {snapshot.rsi}
- ADX: {snapshot.adx}
- 市场情绪: {snapshot.market_sentiment}
- 宏观环境: {snapshot.macro_environment}
- 订单簿: {snapshot.order_book_imbalance}
- 聪明钱方向: {snapshot.smart_money_direction}

请结合这些经验给出判断：
1. 决策方向 (BUY/SELL/HOLD)
2. 信心分数 (0-100)
3. 判断理由
4. 目标价格
5. 止损价格
"""

        # 调用AI决策
        decision = self.ai_client.chat(prompt)

        return decision
```

### 阶段5: 反馈机制

#### 5.1 实现反馈系统

```python
class FeedbackSystem:
    """反馈系统"""

    def collect_feedback(self, snapshot: DecisionSnapshot, decision: str):
        """收集反馈"""
        # 如果AI觉得门槛过严
        if self.is_threshold_too_strict(snapshot, decision):
            feedback = {
                "type": "threshold_too_strict",
                "snapshot_id": snapshot.id,
                "timestamp": int(time.time()),
            }
            self.save_feedback(feedback)

        # 如果AI觉得规则不合适
        if self.is_rule_inappropriate(snapshot, decision):
            feedback = {
                "type": "rule_inappropriate",
                "snapshot_id": snapshot.id,
                "timestamp": int(time.time()),
            }
            self.save_feedback(feedback)

    def analyze_feedback(self):
        """分析反馈"""
        # 获取所有反馈
        feedbacks = self.get_all_feedback()

        # 统计反馈
        threshold_strict_count = sum(1 for f in feedbacks if f["type"] == "threshold_too_strict")
        rule_inappropriate_count = sum(1 for f in feedbacks if f["type"] == "rule_inappropriate")

        # 分析胜率
        threshold_strict_success_rate = self.calculate_success_rate("threshold_too_strict")
        rule_inappropriate_success_rate = self.calculate_success_rate("rule_inappropriate")

        # 调整参数
        if threshold_strict_count > 10 and threshold_strict_success_rate > 0.55:
            self.lower_threshold()

        if rule_inappropriate_count > 10 and rule_inappropriate_success_rate < 0.45:
            self.raise_threshold()
```

#### 5.2 定期分析任务

```python
class FeedbackScheduler:
    """反馈调度器"""

    def analyze_weekly_feedback(self):
        """每周分析反馈"""
        # 分析反馈
        self.feedback_system.analyze_feedback()

        # 生成报告
        report = self.generate_feedback_report()

        # 发送报告
        self.send_report(report)
```

### 阶段6: 场景识别

#### 6.1 实现场景识别器

```python
class MarketSceneRecognizer:
    """市场场景识别器"""

    def recognize_scene(self, snapshot: DecisionSnapshot):
        """识别市场场景"""
        # 趋势市 vs 震荡市
        if snapshot.adx > 25:
            market_type = "TREND"
        else:
            market_type = "RANGE"

        # 超买 vs 超卖 vs 中性
        if snapshot.rsi > 70:
            rsi_state = "OVERBOUGHT"
        elif snapshot.rsi < 30:
            rsi_state = "OVERSOLD"
        else:
            rsi_state = "NEUTRAL"

        # 宏观环境
        macro_env = snapshot.macro_environment

        # 组合场景
        scene = {
            "market_type": market_type,
            "rsi_state": rsi_state,
            "macro_environment": macro_env,
        }

        return scene

    def is_scene_similar(self, scene1: dict, scene2: dict):
        """判断场景是否相似"""
        # 比较场景特征
        similar = (
            scene1["market_type"] == scene2["market_type"]
            and scene1["rsi_state"] == scene2["rsi_state"]
            and scene1["macro_environment"] == scene2["macro_environment"]
        )

        return similar
```

#### 6.2 场景权重调整

```python
class ExperienceWeightAdjuster:
    """经验权重调整器"""

    def adjust_experience_weight(self, lesson: ExperienceLesson, current_scene: dict):
        """调整经验权重"""
        # 获取经验场景
        lesson_scene = {
            "market_type": self.get_market_type_from_lesson(lesson),
            "rsi_state": lesson.rsi_range,
            "macro_environment": lesson.macro_environment,
        }

        # 判断场景是否相似
        similar = self.scene_recognizer.is_scene_similar(lesson_scene, current_scene)

        # 如果场景相似，权重高
        if similar:
            weight = 1.0
        else:
            weight = 0.5

        return weight
```

## 📊 系统架构

```
市场数据 → 决策引擎 → 决策快照 → 24小时回访 → AI复盘 → 经验库
                                              ↓                    ↓
                                         场景识别 ← 智能检索 ← 反馈机制
                                              ↓
                                         改进决策
```

## 🚀 实现优先级

### P0 (必须实现)
1. 决策快照系统
2. 回访验证机制
3. AI复盘系统
4. 经验库

### P1 (重要)
5. 智能经验检索
6. 场景识别

### P2 (优化)
7. 反馈机制
8. 权重调整

## 📝 数据库设计

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
    symbol TEXT,
    rsi_range TEXT,
    adx_range TEXT,
    macro_environment TEXT,
    failure_reason TEXT,
    lesson TEXT,
    next_action TEXT,
    timestamp INTEGER,
    usage_count INTEGER,
    success_rate REAL
);
```

### feedback 表
```sql
CREATE TABLE feedback (
    id TEXT PRIMARY KEY,
    type TEXT,
    snapshot_id TEXT,
    timestamp INTEGER
);
```

## 🎯 预期效果

- 第1周：按基础逻辑判断
- 第4周：积累几百条经验，能调取经验
- 第3个月：经验库丰富，胜率指数级提升
- 第6个月：成为"老手"

**理论上，操作越多，胜率越高。**

---

**核心思想**: 让AI从自己的错误中学习，不断成长，越跑越聪明！
