# Qlib 借鉴分析 & 移植方案

> 源项目：https://github.com/microsoft/qlib  
> 目标系统：CryptoQuant @ /home/ni/crypto_quant  
> 分析时间：2026-04-30

---

## 一、项目概览

Qlib 是微软开源的 AI 量化投资平台，覆盖全链路：数据处理 → 因子工程 → 模型训练 → 回测 → 执行。

与 CryptoQuant 对比：

| 维度 | Qlib | CryptoQuant |
|------|------|-------------|
| 策略类型 | ML 预测型 (LightGBM/LSTM 预测收益率 → TopK 选币) | 规则驱动型 (MA Trend + RSI + MACD + ADX) |
| 因子体系 | 158 个工程化因子 (Alpha158) | 4 个基础指标 |
| 数据管理 | Point-in-Time DB，防前视偏差 | 简单 WebSocket 流 |
| 训练方式 | 滚动训练 (Rolling Trainer) | 自进化调参 (盲调) |
| 目标市场 | A 股 (截面选股，多标的) | 加密货币 (时序交易，BTC 单标的) |
| Agent 能力 | RD-Agent (LLM 驱动因子挖掘+模型优化) | 无 |

---

## 二、可直接借鉴的 3 个核心方法

### 1. Alpha158 因子集 → 加密货币适配版（★★★★★ 最高优先级）

**Qlib 的做法：**
Alpha158DL 定义了 158 个因子，分以下几类：

| 类别 | 因子数 | 代表因子 | 适用加密货币 |
|------|--------|----------|-------------|
| K线形态（K bar） | 9 | KMID(实体/开盘), KLEN(振幅), KSFT(位置) | ✅ 完全适用 |
| 价格动量 | 35+ | ROC5/10/20/30/60, MA5/10/20/30/60, BETA | ✅ 滚动窗口改 5/15/30/60/120 |
| 波动率 | 20+ | STD5/10/20/30/60, RESI, RSQR | ✅ 直接可用 |
| 量价关系 | 15+ | CORR(价量相关), CORD(涨跌量相关) | ✅ 加密货币成交量信息丰富 |
| RSI 类 | 12 | SUMP/SUMN/SUMD（类似 RSI 的变种） | ✅ 比单一 RSI 丰富 |
| 价格位置 | 15+ | MAX/MIN/QTLU/QTLD/RANK/RSV, IMAX/IMIN | ✅ 直接可用 |
| 趋势强度 | 8 | CNTP/CNTN/CNTD（涨跌天数比例） | ✅ |

**我们现有的：**
策略只计算 ma_7, ma_25, ma_99, rsi, adx, macd, volatility 共 7 个指标。因子分析也只检验了 FG 信号。

**移植方案：**
```python
# crypto_quant/data/alpha_factors.py
# 从 Alpha158 中挑选适配加密货币 15 分钟 K 线的因子
# 用纯 Python + numpy 实现，不依赖 qlib

class AlphaFactors:
    """加密货币 Alpha 因子集（适配 15 分钟 K 线）"""
    
    # K 线形态（全部保留）
    KMID = (close - open) / open
    KLEN = (high - low) / open
    KSFT = (2*close - high - low) / (high - low + 1e-12)
    
    # 滚动因子（窗口适配 15 分钟: 4=1h, 24=6h, 96=1d, 288=3d）
    WINDOWS = [4, 12, 24, 48, 96, 288]
    
    # MACD 替代 ROC: 短/长 EMA 比值
    # SUMP/SUMN: RSI 的精确替代
    # CORR: 价量相关性（加密市场量大时往往是反转信号）
```

**预期收益：**
- 因子数量从 7 → 40+（只挑最适配的）
- 因子分析可检验的维度大幅增加
- 配合 IC/IR 系统，自动筛选有效因子

---

### 2. LightGBM 预测模型 + 滚动训练（★★★★☆）

**Qlib 的做法：**
```python
# qlib/contrib/model/gbdt.py
LGBModel(loss="mse")    # 回归: 预测未来收益率
LGBModel(loss="binary") # 分类: 预测涨跌方向

# 训练流程（滚动训练）:
# Train on (T-5y, T-1y) → Predict (T-1y, T) → Validate
# Shift 6 months → Retrain → Repeat
```

**对 CryptoQuant 的价值：**
不是在交易策略层面替代 MATrend，而是：
1. **用 LightGBM 做多因子收益率预测**——输入 Alpha 因子 + FG + 新闻情绪，输出未来 N 根 K 线的方向/幅度
2. **滚动训练**——每 30 天用最新数据重新训练一次
3. **与现有策略融合**——当 LightGBM 的预测信号与 MATrend 一致时，才开仓（双重确认）

```
               MATrend 信号
                    |
    Alpha因子 ─→ LightGBM ─→ 置信度 ─→ 融合 ─→ 开仓决策
                    |                      |
    外部信号 ───────┘               只有两者同向才执行
```

**移植成本：** 
- 安装 lightgbm (~30MB pip install)
- 实现 adapter 桥接 LightGBM 输出到现有策略引擎
- 滚动训练脚本：每周六凌晨跑一次

**优先级：** 中等。先做 Alpha 因子集，再喂给 LightGBM。

---

### 3. Point-in-Time 数据处理（★★★☆☆）

**Qlib 的做法：**
PIT (Point-in-Time) 数据库确保训练时**不会用到未来数据**。比如处理季报数据时，Q1 的财务数据只有在 Q1 结束后才可用——不会让模型"作弊"看到未来。

**对加密货币的适用性：**
加密货币没有季报/年报的前视偏差问题——价格数据是纯时序的。**但有一个类似问题需要关注：**

当我们计算因子（如 MA_96，即过去 24 小时的均价）时，**不能在训练集边界处把未来的价格信息泄露进去**。这在我们手动做回测时需要特别注意。

**建议：** 
不移植完整的 PIT 数据库（成本太高），但在 `alpha_factors.py` 中加一个 `drop_future_leak()` 函数：
```python
def drop_future_leak(df, train_end_idx):
    """删除训练集最后 N 行中使用了未来数据的因子，防止前视偏差"""
```

---

## 三、RD-Agent（暂不做）

Qlib 的 RD-Agent 是一个独立的项目（https://github.com/microsoft/RD-Agent），用 LLM 自动：
- 从论文中挖掘新因子公式
- 用多 Agent 协作优化模型参数

**对 CryptoQuant 的适用性：** 低。原因：
1. RD-Agent 依赖完整的 Qlib 数据管道（PIT DB + DataHandler + Model Registry），移植成本极高
2. RD-Agent 面向 A 股多因子选股场景（截面 Alpha），加密货币是单标的时序预测
3. 我们的策略自进化系统（`strategy_evolver.py`）已经实现了类似逻辑，只是还没接入 LLM

**未来方向（等 Alpha 因子体系成熟后）：**
- 用 LLM 帮助发现新的加密货币专属因子（如链上数据因子）
- 用 LLM 自动编写因子计算代码（而不是手写 KMID/KLEN）

---

## 四、实施路线图

```
Phase 1（本周）：Alpha 因子集
  ├─ crypto_quant/data/alpha_factors.py
  ├─ 从 Alpha158 挑选 30-40 个适配加密的因子
  ├─ 纯 numpy 实现，支持 15 分钟 K 线
  └─ IC/IR 检验每个因子的预测力

Phase 2（下周）：LightGBM 预测模型
  ├─ crypto_quant/ml/lgb_predictor.py
  ├─ 输入 Alpha 因子 + 外部信号
  ├─ 输出方向预测 + 置信度
  └─ 滚动训练 + 融合到策略引擎

Phase 3（后续）：因子自动筛选
  ├─ 每月/每周自动跑 IC 检验
  ├─ IC 不显著的因子自动降级
  ├─ 新因子上线后自动评估
  └─ 保持因子池健康
```

---

## 五、总结

Qlib 最值得借鉴的是**因子体系**和**ML 预测范式**。

CryptoQuant 现有的是规则交易 + 基础指标 + 自进化调参。引入 Qlib 的思路后：

| 组件 | 现有 | Qlib 启发后 |
|------|------|------------|
| 因子数量 | 7 个 | 40+ 个（Alpha 因子集） |
| 因子检验 | FG 单一因子 | 全因子 IC/IR 检验 |
| 信号来源 | MATrend + RSI/MACD/ADX | MATrend + LightGBM 融合 |
| 模型训练 | 无（盲调参） | 滚动训练，每月更新 |
| 策略优化 | 复盘 → 调参 | 因子淘汰 → 模型重训 → 策略调整 |

**行动：**
1. ✅ 先做 Alpha 因子集（最低成本，最大收益）
2. ✅ 再做 LightGBM（ML 提升预测精度）
3. ⏸ RD-Agent 暂缓