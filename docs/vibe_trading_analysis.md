# Vibe-Trading 借鉴分析 & 移植方案

> 源项目：https://github.com/HKUDS/Vibe-Trading  
> 目标系统：CryptoQuant @ /home/ni/crypto_quant  
> 分析时间：2026-04-30

---

## 一、项目概览

Vibe-Trading 是一个"AI-native"的量化交易研究平台，核心理念是**将 LLM Agent 嵌入到交易流程的每个环节**——不是让 AI 写策略，而是让 AI **理解交易者的行为模式（Shadow Account），从交易日志中提炼策略，并用多 Agent Swarm 并行做回测验证。**

与我们 CryptoQuant 的最大区别：
- CryptoQuant：**规则驱动**，策略参数由人工定义 → 自进化调参
- Vibe-Trading：**数据驱动**，从用户的真实交易行为中提取策略参数 → 回测验证

两种方法的交集是「复盘 → 自我优化」，Vibe-Trading 在这一环节做得远比我们成熟。

---

## 二、可直接借鉴的 5 个核心方法

### 1. Shadow Account — 交易行为画像（★★★★★ 最高优先级）

**Vibe-Trading 的做法：**
- 解析用户的 CSV/Excel 交易日志 → 识别买卖配对（FIFO）→ 计算每个 roundtrip 的 PnL
- 从**盈利的 roundtrips** 中提取特征：持有天数、进场时段、PnL%、品种
- KMeans 聚类（k=2~5）→ 每个簇生成一条 ShadowRule → 用 Decision Tree 提取入场条件
- 最终产出 `ShadowProfile`：5 条以内的人类可读交易规则

**我们现在的做法：**
- `daily_review.py` 只做了基础统计（win_rate, PnL）
- `strategy_evolver.py` 只调参数（RSI 阈值、ADX 阈值），不理解「交易行为模式」

**移植方案：**
```python
# 新建 crypto_quant/services/shadow_account.py
# 核心流程：
# 1. 解析 position_tracker.json 中的 trade_history
# 2. FIFO 配对（已有的 daily_review._pair_trades 可复用）
# 3. 只取 profitable roundtrips
# 4. 提取特征：hold_hours, entry_hour, pnl_pct, volatility_at_entry
# 5. KMeans 聚类 → 每个簇提炼一条 Rule
# 6. 输出 ShadowProfile（JSON），供 strategy_evolver 使用
```

**预期收益：**
- 不再盲调参数，而是基于「你的盈利交易有什么模式」来优化
- 比如发现"凌晨 2-4 点的做空交易胜率 80%"→ 自动加入时间过滤

---

### 2. 行为偏差诊断（★★★★★）

**Vibe-Trading 的做法：**
`trade_journal_tool._compute_behavior()` 做 4 项诊断：

| 诊断 | 方法 | 对策略的价值 |
|------|------|-------------|
| **Disposition Effect** | 对比赢单/亏单的持有时间比 | 如果亏损单持有时间远长于盈利单 → 需要收紧止损 |
| **Overtrading** | 对比高频交易日 vs 低频交易日的平均 PnL | 如果高频日 PnL 更差 → 降低开仓频率阈值 |
| **Chasing Momentum** | 统计追涨买入的比例 | 如果追涨比例 >40% → 增加趋势确认过滤器 |
| **Anchoring** | 同一品种的交易价格变异系数 | 如果在窄区间反复交易 → 需要突破式入场而非区间入场 |

**我们现有的：** 零。`daily_review.py` 只输出 win_rate / PnL / sharpe，不给行为建议。

**移植方案：**
```python
# 在 daily_review.py 的 _build_report() 后面追加：
# _diagnose_disposition(trades) → 输出到报告
# _diagnose_overtrading(trades) → 输出到报告
# _diagnose_chasing(trades, price_data) → 需要 price_data
# _diagnose_anchoring(trades) → 输出到报告
```

**核心代码可直接从 Vibe-Trading 搬：**
- `_disposition_effect()` — 171 行，纯算术
- `_overtrading()` — 190 行，纯算术
- `_chasing_momentum()` — 228 行，用到 price data 的 shift(3)
- `_anchoring()` — 266 行，纯算术

---

### 3. Delta-PnL 归因分析（★★★★☆）

**Vibe-Trading 的做法：**
`backtester._compute_attribution()` 将用户真实 PnL 与 Shadow（理想化策略）的 PnL 差异拆解为 5 个因子：

| 因子 | 含义 | 符号 |
|------|------|------|
| noise_trades_pnl | 不符合规则的交易带来的 PnL（shadow 避免的） | -PnL |
| early_exit_pnl | 盈利单提前离场少赚的部分 | +PnL |
| late_exit_pnl | 亏损单持有过久多亏的部分 | +PnL |
| overtrading_pnl | 超额交易频率的影响 | -PnL |
| missed_signals_pnl | 剩余无法解释的差异 | residual |

**我们现有的：** 只有总 PnL 对比。

**移植方案：**
```python
# 在 daily_review.py 加入 _attribute_delta_pnl()
# 需要:
# - 真实交易 roundtrips （已有）
# - "理想策略"的规则（从 ShadowProfile 提取的 holding_days_range）
# 核心逻辑：遍历每个 roundtrip，判断是否在规则范围内，计算每个因子的 PnL
```

---

### 4. 因子分析（IC/IR）（★★★★☆）

**Vibe-Trading 的做法：**
`factor_analysis_tool.py` — 完整的因子检验流程：
- 输入：因子值 CSV + 收益率 CSV
- 计算 Spearman Rank IC（日频截面相关性）
- 计算 IC Mean / IC Std / IR (Information Ratio) / IC>0 比例
- 分层回测：按因子值分 5 组，等权持有，看各组的累计 NAV

**我们现有的：** 零。

**对 CryptoQuant 的适用场景：**
我们的 `sentiment.py` 采集了恐惧贪婪指数、新闻情绪。可以用因子分析方法**检验这些外部信号的预测能力**——恐惧贪婪指数对未来 24h 收益率的 IC 是多少？分层效果如何？

**移植方案：**
```python
# crypto_quant/data/factor_analysis.py
# 输入: sentiment.py 产出的 sentiment_score 时间序列 + BTC 日收益率
# 输出: IC 统计 + 分层净值曲线
# 如果 IC 显著（如 IC>0.05 且 t>2），自动纳入 strategy_evolver 的优化空间
```

---

### 5. 策略代码生成（Codegen）（★★★☆☆）

**Vibe-Trading 的做法：**
`shadow_account/codegen.py` 使用 Jinja2 模板从 `ShadowProfile` 自动生成 `signal_engine.py`，然后 `ast.parse` 静态验证。生成的代码可以直接跑回测。

**我们现有的：**
`strategy_evolver.py` 通过字符串替换改 `optimized_v6.py` 中的参数。没有代码生成层。

**对 CryptoQuant 的价值：** 中等。我们的策略参数空间还比较小（十几个参数），不需要完整的代码生成。但如果你未来想**自动探索全新的策略结构**（不只是调参），codegen 就是必须的。

**建议：** 暂不移植，保留设计参考。等 Shadow Account 做完、发现策略结构需要变化时再做。

---

## 三、不做的事项（暂时）

| Vibe-Trading 功能 | 不做原因 |
|---|---|
| Swarm 多 Agent 回测 | 我们只有加密货币一个市场，不需要跨市场并行回测 |
| PineScript 查看器 | 前端功能，非量化核心 |
| vnpy 对接 | 我们用的是 Binance API，不走 vnpy |
| A 股/期货回测引擎 | 市场不匹配 |

---

## 四、实施路线图

```
Phase 1（本周，1-2天）：行为偏差诊断
  ├─ 从 trade_journal_tool.py 搬 4 个诊断函数到 daily_review.py
  ├─ 接入已有的 trade_history 数据
  └─ 复盘报告增加「行为诊断」section

Phase 2（下周，2-3天）：Shadow Account 画像
  ├─ 新建 shadow_account.py
  ├─ KMeans 聚类 + Rule 提取
  ├─ 输出 ShadowProfile → 喂给 strategy_evolver
  └─ 实现 Delta-PnL 归因

Phase 3（后续）：因子分析
  ├─ 新建 factor_analysis.py
  ├─ 检验 sentiment.py 产出的情绪指标 IC
  └─ 显著的因子自动纳入策略空间
```

---

## 五、关键代码片段速查

### 行为诊断入口（直接搬）
```python
# 来自 trade_journal_tool.py，纯算术，无需外部数据
_disposition_effect(roundtrips_df)  # → {severity, ratio, evidence}
_overtrading(trades_df, roundtrips_df)  # → {severity, busy_avg, quiet_avg}
_chasing_momentum(trades_df)  # → {severity, chase_ratio}
_anchoring(trades_df)  # → {severity, cv_ratio}
```

### Shadow Rule 提取（核心逻辑）
```python
# 来自 extractor.py _extract_rules()
# 1. KMeans(n_clusters=auto, via silhouette score)
# 2. 每个簇 → p10-p90 区间 → ShadowRule(entry_condition, holding_days_range)
```

### Delta-PnL 归因（5 因子分解）
```python
# 来自 backtester.py _compute_attribution()
# 输入: roundtrips + ShadowProfile.rules 的 holding_days_range
# 输出: {noise, early_exit, late_exit, overtrading, missed_signals}
```

---

## 六、总结

Vibe-Trading 最值得借鉴的不是策略本身，而是**"理解你的交易行为 → 提炼可复现的模式 → 回测验证"**这套方法论。

CryptoQuant 已经有完整的数据采集 + 策略执行 + 复盘 + 自进化流水线，缺的是从**行为层面**理解交易质量的环节。

**行动：**
1. ✅ 先搬行为诊断（最低成本，最高价值）
2. ✅ 再做 Shadow Account（核心竞争力）
3. ✅ 最后上因子分析（锦上添花）