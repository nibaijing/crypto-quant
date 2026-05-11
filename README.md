# CryptoQuant

> 加密货币合约量化交易系统 — 对标 Hermes Agent 的七项进化升级

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

CryptoQuant 是一个模块化的加密货币量化交易系统，支持 **回测 → 模拟盘 → 实盘** 完整链路。核心策略基于多指标共振（MA + RSI + MACD + ADX），配备 AI 决策覆盖层、多源情绪聚合、Polymarket 预测市场管道、长期交易记忆和策略自进化引擎。

---

## 架构

```
                          ┌──────────────────────────────┐
                          │     Dashboard (:8899)         │
                          │  账户 / 持仓 / 信号 / 日志      │
                          └──────────────┬───────────────┘
                                         │
  ┌──────────────────────────────────────┼──────────────────────────────────────┐
  │                         CryptoQuant 核心引擎                                 │
  │                                                                             │
  │   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐                   │
  │   │  run_live_ws  │   │ run_live_okx │   │   main.py    │                   │
  │   │  (WS 高性能)   │   │  (OKX 实盘)  │   │ (回测 + 数据) │                   │
  │   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘                   │
  │          │                  │                  │                            │
  │          └──────────────────┼──────────────────┘                            │
  │                             │                                               │
  │   ┌─────────────────────────▼─────────────────────────┐                    │
  │   │              OptimizedV6 策略引擎                    │                    │
  │   │    MA(7/25/99) + RSI + MACD + ADX + ATR          │                    │
  │   │    因子偏向 + Pin Bar 过滤 + 加减仓决策              │                    │
  │   └─────────────────────────┬─────────────────────────┘                    │
  │                             │                                               │
  │   ┌─────────────────────────▼─────────────────────────┐                    │
  │   │        DecisionEngine 决策层 (AI + 风控)              │                    │
  │   │    5源情绪聚合 + Polymarket 预测市场 + 长期记忆        │                    │
  │   │    Factor Bias + Fail Memory + 4 层风控检查          │                    │
  │   └─────────────────────────┬─────────────────────────┘                    │
  │                             │                                               │
  │   ┌─────────────────────────▼─────────────────────────┐                    │
  │   │              执行器 (Executor)                       │                    │
  │   │    模拟盘: FuturesExecutor | 实盘: OKXExecutor      │                    │
  │   └───────────────────────────────────────────────────┘                    │
  │                                                                             │
  └─────────────────────────────────────────────────────────────────────────────┘
                                         │
         ┌───────────────────────────────┼───────────────────────────────┐
         │                               │                               │
  ┌──────▼──────┐  ┌──────────────┐  ┌──▼──────────┐  ┌─────────────────┐
  │ 策略自进化    │  │  每日复盘     │  │ 长期记忆      │  │  多源情绪聚合    │
  │ 三模式切换    │  │ 行为诊断      │  │ TradingMemory│  │ OKX/推特/新闻   │
  │ 五层防漂移    │  │ 因子分析      │  │ 方向级归因    │  │ Surf/KOL        │
  └─────────────┘  └──────────────┘  └──────┬───────┘  └────────┬────────┘
                                           │                    │
                                  ┌────────▼────────┐  ┌────────▼────────┐
                                  │ Polymarket 预测   │  │  经验库         │
                                  │ 三层匹配+自学习   │  │  自学习系统      │
                                  └─────────────────┘  └─────────────────┘
```

---

## 核心特性

### 1. 策略引擎 — OptimizedV6

| 组件 | 角色 |
|------|------|
| **MA(7/25/99)** | 多周期趋势判断，MA99 为牛熊分界线 |
| **RSI(14)** | 超买超卖信号，区分多/空阈值 |
| **MACD(12/26/9)** | Histogram 二次确认，过滤假突破 |
| **ADX(14)** | 趋势强度过滤，震荡市不参与 |
| **ATR(14)** | 动态止损止盈，自适应波动 |
| **凯利公式** | 仓位管理，根据胜率动态调整 |
| **动态杠杆 (5-15x)** | 高波动降低杠杆，低波动放大 |
| **Pin Bar 检测** | 冲高回落/探底回升陷阱识别 |
| **盈利加仓/亏损补仓** | 三层仓位缩放：REDUCE / PROFIT_ADD / LOSS_ADD |
| **因子偏向** | 44-因子 Alpha 集强制方向匹配 (conf≥0.8) |

### 2. 多源情绪聚合 (对标推文④)

打通 5 个低成本情绪来源，聚合为统一情绪分数 (-1.0 ~ +1.0)：

| 源 | 权重 | 说明 |
|----|------|------|
| **OKX Fear & Greed** | 30% | 恐惧贪婪指数 (fallback→alternative.me) |
| **推特/社交扫描** | 20% | GDELT + GoogleNews 关键词情绪 |
| **新闻情绪** | 25% | CryptoPanic RSS + Google News |
| **Surf 社交数据** | 15% | CoinGecko 社区投票 + Reddit 活跃度 |
| **KOL 记录** | 10% | 本地 JSON 持久化，自动/手动录入 |

情绪明细直接注入 AI 决策 prompt，让 LLM 感知市场整体氛围而非只看技术指标。

### 3. Polymarket 预测市场管道 (对标推文②)

三层处理将 Polymarket 变为高权重决策因子：

**Layer 1 — 多维度匹配**
- ticker 精确匹配 → 内置别名 → `polymarket_aliases.json` → 标题/描述/slug 关键词

**Layer 2 — 分数转换**
```
概率 → 方向判定(问题方向 + 0.5偏移) → 成交量加权 → 截断[-0.6, +0.6]
→ score≥0.08 = bullish, score≤-0.08 = bearish
```

**Layer 3 — 自学习别名**
- 匹配成功 → 从 slug 提取项目名 → 自动写回 `polymarket_aliases.json`

### 4. 长期记忆系统 (对标推文⑦)

交易结束后自动记录方向级归因，反向作用后续开仓：

| 记忆维度 | 内容 | 反向作用 |
|----------|------|----------|
| **拖累标的** | 近 7 天亏最多的币 | 仓位 × 0.75 |
| **弱侧方向** | long/short 哪个更差 | 自动降频 (30/70) |
| **失败类型** | stop_loss / take_profit / signal | AI prompt 风险提醒 |
| **进化趋势** | improving / declining / stable | 模式切换依据 |
| **回撤幅度** | 近 7 天总亏损 | 降权系数 |

### 5. 三模式策略自进化 (对标推文⑥)

| 模式 | 触发条件 | 参数调整 |
|------|----------|----------|
| 🛡️ **保守** | 胜率 < 35% 或连亏 ≥ 3 或回撤 > 10% | ADX+5, 缩仓, 收紧止损, 延长冷却 |
| ⚖️ **均衡** | 正常状态 | 保持当前参数 |
| ⚔️ **进攻** | 胜率 > 60% 且无连亏, 或 improving | ADX-3, 扩仓, 放宽止损, 缩短冷却 |

配合五层防漂移：
1. `PARAM_BOUNDS` — 硬边界 (ADX 25-50)
2. `SMOOTH_LIMITS` — 单日跳变限制
3. `DRIFT_LOCK` — 3 天同向锁定
4. `DAILY_CAP` — 每日最多调整 3 个参数
5. AI prompt 反路径依赖提醒

### 6. AI 决策覆盖层

规则信号边界模糊时，调用 LLM 进行二次判断，prompt 注入：

- 完整技术指标快照 (RSI/ADX/MACD/MA 排列)
- 多源情绪聚合 (OKX/推特/新闻/Surf/KOL)
- Polymarket 预测市场分数
- 长期记忆 (拖累标的/弱侧/失败类型)
- Factor bias 强制方向
- 近期交易记录
- 历史失败模式

| 参数 | 值 |
|------|-----|
| 每日 LLM 调用上限 | 12 次 |
| 单类型冷却 | 15 分钟 |
| 超时降级 | 10 秒 → 规则回退 |
| 自动放行阈值 | ≥75% 条件通过 (long/short) |
| AI 主动介入阈值 | HOLD 但某方向 ≥60% |

### 7. 回测与验证

- 向量化 15m K 线回测
- 44-因子 Alpha 集 IC/IR 检验
- 分层回测 (5-group quantile)
- LightGBM 方向预测双确认

---

## 目录结构

```
crypto_quant/
├── main.py                      # CLI 入口：fetch / backtest / list
├── run_live_ws.py               # ★ 高性能实盘引擎 (WebSocket + DecisionEngine)
├── run_live_okx.py              # OKX 实盘引擎 (REST轮询)
├── run_live.py                  # 基础实盘 (兼容旧版)
│
├── strategies/spot/
│   ├── optimized_v6.py          # ★ 主力策略 (多空双杀 + 加减仓 + Pin Bar)
│   ├── dual_v5.py / bear_v4.py  # 旧版策略
│   └── ...
│
├── execution/
│   ├── executor_v2.py           # 模拟盘执行器 (FuturesExecutor)
│   ├── okx_executor.py          # OKX 实盘执行器
│   ├── ai_override.py           # ★ DecisionEngine (AI + 情绪 + PM + 风控)
│   └── signals.py               # SignalReport + FinalDecision 数据模型
│
├── data/
│   ├── ws_price_stream.py       # WebSocket 实时行情 (Binance)
│   ├── alpha_factors.py         # 44-因子 Alpha 集
│   ├── sentiment.py             # 市场情绪 (旧版)
│   ├── sentiment_enhanced.py    # ★ 5源情绪聚合器 (新版)
│   └── polymarket_scanner.py    # ★ Polymarket 三层扫描
│
├── learning/
│   ├── trading_memory.py        # ★ 长期记忆系统 (方向级归因)
│   ├── decision_snapshot.py     # 决策快照 (旧)
│   └── self_learning_system.py  # 自学习集成 (旧)
│
├── services/
│   ├── factor_analysis.py       # 因子 IC/IR 检验 + 偏向检测
│   ├── behavior_diagnosis.py    # 行为诊断
│   └── ...
│
├── ml/
│   └── lgb_predictor.py         # LightGBM 价格方向预测
│
├── monitor/
│   └── dashboard_server.py      # Web 看板 (:8899)
│
├── daily_review.py              # ★ 每日复盘 (含行为诊断 + 因子)
├── strategy_evolver.py          # ★ 三模式自进化 + 五层防漂移
├── notifier.py                  # Telegram 通知
└── cryptoquant.service          # systemd 服务文件
```

---

## 快速开始

### 环境要求

- Python 3.10+
- pip 依赖：`pandas`, `numpy`, `pyyaml`, `requests`

### 安装

```bash
git clone https://github.com/nibaijing/crypto-quant.git
cd crypto-quant

# 安装依赖
pip install pandas numpy pyyaml requests websocket-client

# 可选：LightGBM 方向预测
pip install lightgbm
```

### 模拟盘运行

```bash
# 启动引擎 (Binance WebSocket + 模拟执行)
python run_live_ws.py

# 启动 Dashboard
python dashboard_server.py
# 访问 http://localhost:8899
```

### systemd 部署

```bash
cp cryptoquant.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now crypto-quant.service
systemctl --user status crypto-quant.service
journalctl --user -u crypto-quant.service -f
```

---

## 风控体系

### 策略级
- 最大回撤 20% 熔断
- 最大持仓 32 根 K 线 (8h)
- ADX < 23 震荡市过滤
- Pin Bar 三级过滤 (LONG/SHORT 陷阱检测)
- Factor Bias 强制方向匹配 (conf ≥ 0.8)

### 执行级
- 单笔仓位上限 (保证金 × 购买力)
- 单标的总仓位上限
- 余额不足自动缩小仓位重试
- 反向持仓拦截

### 记忆级 (Phase 3)
- 拖累标的自动降权 (×0.75)
- 弱侧方向自动降频 (30/70)
- 连续亏损 → 保守模式

### 进化级
- PARAM_BOUNDS 硬边界
- SMOOTH_LIMITS 单日跳变
- DRIFT_LOCK 漂移锁定
- DAILY_CAP 每日上限
- AI 反路径依赖提醒

---

## 安全

- API Key 不入库 (`.gitignore` 已配置)
- 使用环境变量存储敏感信息
- OKX API 设置 IP 白名单
- **免责声明**: 本系统仅供学习研究，不构成投资建议

---

## License

MIT