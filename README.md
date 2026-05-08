# CryptoQuant

> 加密货币合约量化交易系统 — 从回测到实盘，从规则到自进化

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

CryptoQuant 是一个模块化的加密货币量化交易系统，支持**回测 → 模拟盘 → 实盘**的完整链路。核心策略基于多指标共振（MA + RSI + MACD + ADX），配备 AI 决策覆盖层和策略自进化引擎，让系统越跑越聪明。

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
  │   │    凯利资金管理 + 动态杠杆 (5-15x)                   │                    │
  │   └─────────────────────────┬─────────────────────────┘                    │
  │                             │                                               │
  │   ┌─────────────────────────▼─────────────────────────┐                    │
  │   │        DecisionEngine 决策层 (AI + 风控)              │                    │
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
  │ 策略自进化    │  │  每日复盘     │  │ AI 自学习     │  │  因子分析       │
  │ (Evolver)   │  │ (Daily       │  │ (Learning)   │  │  (Factor)      │
  │ 自动调参     │  │  Review)     │  │ 经验积累     │  │  市场情绪集成    │
  └─────────────┘  └──────────────┘  └─────────────┘  └─────────────────┘
```

---

## 核心特性

### 策略引擎 — OptimizedV6

| 组件 | 角色 |
|------|------|
| **MA(7/25/99)** | 多周期趋势判断，MA99 为牛熊分界线 |
| **RSI(14)** | 超买超卖信号，区分多/空阈值 |
| **MACD(12/26/9)** | Histogram 二次确认，过滤假突破 |
| **ADX(14)** | 趋势强度过滤，震荡市不参与 |
| **ATR(14)** | 动态止损止盈，自适应波动 |
| **凯利公式** | 仓位管理，根据胜率动态调整 |
| **动态杠杆 (5-15x)** | 高波动降低杠杆，低波动放大 |

### AI 决策覆盖层

规则信号触发但处于**模糊边界**时，调用 LLM 进行二次判断：

| 触发条件 | 说明 |
|----------|------|
| LGB 无意见 | ML 模型不确定但规则有信号 |
| 连亏后首信号 | 连续亏损 2 次后的第一次信号 |
| ADX 边缘 (20-25) | 震荡转趋势的模糊区 |
| 每日前 2 笔 | 开盘阶段谨慎判断 |
| RSI 近阈值 | 平仓时 RSI 接近但未触及 |

- 日限制 ≤ 12 次 LLM 调用
- 单类型 15 分钟冷却
- 超时 10 秒降级到原规则

### 自学习系统

```
交易决策 → 决策快照 → 24h 回访验证 → AI 复盘失败案例 → 经验库
                                                    ↓
                                            下次决策时检索相关经验
```

- **决策快照**: 完整记录每次交易的上下文（指标、情绪、资金方向）
- **回访验证**: 事后验证决策正确性
- **AI 复盘**: LLM 分析失败原因，提取教训
- **经验库**: 按 RSI/ADX/宏观环境索引，越跑越丰富

### 策略自进化

从每日复盘自动调整策略参数，具备**五层安全防护**防止 AI 路径依赖漂移：

| 层 | 防护机制 | 作用 |
|---|---|---|
| **PARAM_BOUNDS** | 硬边界 | 参数永远在安全范围内 (如 ADX 25-50) |
| **SMOOTH_LIMITS** | 单日跳变限制 | 每天最多 ±3-5，不会剧烈震荡 |
| **DRIFT_LOCK** | 漂移锁定 | 同一参数连续 3 天同向 → 锁定 1 天 |
| **DAILY_CAP** | 每日上限 | 每天最多调整 3 个参数 |
| **AI PROMPT** | 反路径依赖警告 | AI 被明确要求收敛而非单向推进 |

- 胜率 < 40% → 收紧信号条件 (不是放松！)
- 单笔亏损过大 → 降低杠杆 / 收紧止损
- 多空不对称 → 调整方向偏好权重
- 外部因子偏置 (conf ≥ 0.8) → 强制 AI 决策匹配偏向方向

### 每日复盘 + 行为诊断

- 解析交易日志，配对开平仓
- 计算胜率、总盈亏、最大回撤
- 四项行为诊断：处置效应、过度交易、追涨、锚定

---

## 目录结构

```
crypto_quant/
├── main.py                      # CLI 入口：fetch / backtest / list
├── run_live_ws.py               # 高性能实盘引擎 (WebSocket + SharedState)
├── run_live_okx.py              # OKX 实盘引擎 (REST轮询)
├── run_live.py                  # 基础实盘 (兼容旧版)
│
├── strategies/                  # 策略层
│   └── spot/
│       ├── optimized_v6.py      # ★ 主力策略 (多空双杀 + 动态杠杆)
│       ├── dual_v5.py           # 双策略长/短分离
│       ├── ma_rsi_macd.py       # 基础 MA 趋势
│       └── ma_rsi_v2.py         # MA+RSI 改进版
│
├── execution/                   # 执行层
│   ├── executor_v2.py           # 模拟盘执行器 (FuturesExecutor)
│   ├── okx_executor.py          # OKX 实盘执行器
│   ├── ai_override.py           # ★ AIOverride 模糊边界决策层
│   └── signals.py               # 信号处理器
│
├── core/                        # 基础设施
│   ├── config.py                # 配置加载 (YAML + 环境变量)
│   ├── exchange_adapter.py      # OKX API 适配器
│   └── binance_adapter.py       # Binance API 适配器
│
├── data/                        # 数据层
│   ├── pipeline.py              # 数据管线 (拉取/存储)
│   ├── ws_price_stream.py       # WebSocket 实时行情
│   ├── alpha_factors.py         # 44 因子 Alpha 集
│   ├── sentiment.py             # 市场情绪数据
│   └── klines/                  # K线数据存储
│
├── learning/                    # ★ AI 自学习系统
│   ├── decision_snapshot.py     # 决策快照
│   ├── verification.py          # 回访验证
│   ├── ai_reviewer.py           # AI 复盘
│   ├── experience_db.py         # 经验库
│   └── self_learning_system.py  # 集成调度
│
├── services/                    # 服务层
│   ├── factor_analysis.py       # 因子 IC/IR 检验
│   └── behavior_diagnosis.py    # 行为诊断 (Disposition/Overtrading等)
│
├── ml/                          # 机器学习
│   └── lgb_predictor.py         # LightGBM 价格方向预测
│
├── monitor/                     # 监控 & 看板
│   ├── dashboard.py             # 基础看板
│   ├── dashboard_enhanced.py    # 增强版看板 (HTML)
│   └── dashboard_server.py      # Web 服务器 (:8899)
│
├── backtest/                    # 回测引擎
│   ├── engine.py                # 回测核心
│   └── reporter.py              # 报告生成
│
├── config/
│   └── settings.yaml            # 全局配置
│
├── scripts/                     # 工具脚本
│   ├── fetch_historical.py      # 历史数据拉取
│   ├── extract_15m_data.py      # 15m K线提取
│   └── retrain_lgb.py           # LightGBM 重训练
│
├── daily_review.py              # 每日复盘
├── strategy_evolver.py          # 策略自进化
├── notifier.py                  # Telegram 通知
├── cryptoquant.service          # systemd 服务文件
└── dashboard_server.py          # 看板服务器入口
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

# 可选：LightGBM 模型
pip install lightgbm  # 需要 libomp，macOS: brew install libomp
```

### 回测

```bash
# 拉取历史数据
python main.py fetch --symbols BTC-USDT --timeframe 1H --days 180

# 运行回测
python main.py backtest --strategy spot --data BTC_USDT_1H.parquet --capital 10000
```

### 模拟盘 (WebSocket 高性能引擎)

```bash
# 启动引擎 (Binance WebSocket + 模拟执行)
python run_live_ws.py

# 另一个终端启动 Dashboard
python dashboard_server.py
# 访问 http://localhost:8899
```

### systemd 服务 (生产部署)

```bash
# 安装服务
sudo cp cryptoquant.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now crypto-quant.service

# 查看状态
systemctl --user status crypto-quant.service

# 查看日志
journalctl --user -u crypto-quant.service -f
```

---

## 配置

编辑 `config/settings.yaml`：

```yaml
exchange:
  name: "okx"
  testnet: true          # true=模拟盘, false=实盘
  api_key: ""            # 或设置环境变量 CQ_EXCHANGE__API_KEY
  api_secret: ""
  passphrase: ""

backtest:
  initial_capital: 1000
  commission: 0.0005

risk:
  max_position_pct: 0.5
  max_drawdown_pct: 0.15
  stop_loss_pct: 0.03
  max_consecutive_losses: 5

futures:
  default_leverage: 3
  max_leverage: 10
  margin_mode: "isolated"
```

> API Key 建议使用环境变量，避免提交到 Git。

---

## 策略迭代历史

| 版本 | 演进 |
|------|------|
| **V1** | 基础 MA 趋势 (MA7/25 金叉死叉) |
| **V2** | 加入 RSI 超买超卖过滤 |
| **V3** | 自适应参数 + ATR 动态止损 |
| **V4** | 网格搜索最优参数 |
| **V5** | 多空分离策略 (DualV5) |
| **V6** | ★ 当前主力：MACD双确认 + ADX过滤 + 凯利资金管理 + 动态杠杆 |

---

## 风控体系

### 策略级

- 最大回撤 20% 熔断
- 连续亏损 5 次暂停
- 最大持仓 48 根 K 线 (12h)
- ADX < 23 震荡市过滤
- Pin Bar 三级过滤 (LONG/SHORT 陷阱检测)
- Factor Bias 强制方向匹配 (conf ≥ 0.8)

### 执行级 (模拟盘)

- 单笔 ≤ 总权益 20%
- 总仓位 ≤ 总权益 50%
- 保证金使用率 ≤ 95% 现金 (留 5% 缓冲)
- 反向持仓拦截 (持 long 禁开 short et vice versa)

### 账户级 (OKX 实盘)

- 单笔 ≤ 总资金 15%
- 总仓位 ≤ 50%
- 保证金使用率 ≤ 80%
- 强平距离 ≥ 20% 缓冲

### 进化级 (策略自优化)

- 参数硬边界 + 跳变限制 + 漂移锁定 + 每日上限 + AI 反路径依赖提醒
- 五层防护防止「一条道走到黑」的参数漂移

---

## 安全

- API Key 不入库 (`.gitignore` 已配置)
- 使用环境变量存储敏感信息
- 实盘前务必模拟盘充分测试
- 建议 OKX API 设置 IP 白名单
- **免责声明**: 本系统仅供学习研究，不构成投资建议。量化交易存在亏损风险。

---

## 待办

- [ ] OKX 实盘接入（等胜率 80%+）
- [ ] WebSocket 引擎直连 OKX
- [ ] 多币种支持
- [ ] 策略热切换
- [ ] 移动端看板

---

## License

MIT
