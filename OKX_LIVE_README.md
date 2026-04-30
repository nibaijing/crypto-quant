# OKX实盘交易系统使用指南

## 📋 概述

本系统是基于OKX交易所的自动化量化交易系统，支持模拟盘和实盘两种模式。

### 核心特性

- ✅ 基于OKX官方API的真实交易
- ✅ 实时风控检查（保证金、强平距离、仓位限制）
- ✅ 动态杠杆调整（5-15x）
- ✅ 完整的订单跟踪和盈亏计算
- ✅ 实时Dashboard监控
- ✅ Telegram告警推送

---

## 🚀 快速开始

### 1. 获取OKX API Key

1. 登录 [OKX官网](https://www.okx.com)
2. 进入「API管理」页面
3. 创建新的API Key
4. **重要**: 设置IP白名单（推荐）
5. **重要**: 只勾选「交易」权限，不要勾选「提现」
6. 记录以下信息：
   - API Key
   - API Secret
   - Passphrase

### 2. 配置系统

#### 方式一：环境变量（推荐）

```bash
export CQ_EXCHANGE__API_KEY="your_api_key"
export CQ_EXCHANGE__API_SECRET="your_api_secret"
export CQ_EXCHANGE__PASSPHRASE="your_passphrase"
```

#### 方式二：配置文件

编辑 `config/settings.yaml`:

```yaml
exchange:
  testnet: false  # false=实盘, true=模拟盘
  api_key: "your_api_key"
  api_secret: "your_api_secret"
  passphrase: "your_passphrase"
```

### 3. 运行系统

#### 模拟盘测试（推荐先运行）

```bash
# 确保配置文件中 testnet: true
python run_live_okx.py
```

#### 实盘运行

```bash
# 确保配置文件中 testnet: false
python run_live_okx.py
```

---

## 📊 监控Dashboard

### 启动Dashboard服务器

```bash
python dashboard_server.py
```

访问: http://localhost:8899

### Dashboard功能

- **账户总览**: 权益、可用余额、浮动盈亏
- **持仓明细**: 实时持仓、盈亏、杠杆
- **交易记录**: 最近20笔交易
- **风险指标**: 风险等级、保证金使用率
- **策略状态**: 当前信号、参数配置

---

## ⚠️ 风控规则

### 账户级别

- 单笔交易不超过总资金的 **5%**
- 总仓位不超过总资金的 **30%**
- 保证金使用率不超过 **80%**
- 强平距离至少保留 **20%**

### 持仓级别

- 单币种持仓不超过总资金的 **20%**
- 止损：**-3%**（硬止损）
- 止盈：**+10%**（软止盈）
- 最大持仓时长：**7天**

### 策略级别

- 连续亏损 **5次** 暂停
- 单日最大交易次数：**20次**
- 异常波动时暂停（波动率>5%）

---

## 📈 策略说明

### 当前策略：MATrend(7/25) + RSI + MACD + ADX

**信号逻辑**:
- MA快线(7) > MA慢线(25) → 趋势向上
- RSI < 35 → 超卖，考虑做多
- RSI > 65 → 超买，考虑做空
- MACD金叉 → 买入信号
- MACD死叉 → 卖出信号
- ADX > 25 → 趋势确认

**动态杠杆**:
- 高波动率（>2%）→ 5x
- 中波动率（1-2%）→ 10x
- 低波动率（<1%）→ 15x

---

## 🔧 系统架构

```
run_live_okx.py (主循环)
    │
    ├── OKXExecutor (实盘执行器)
    │   ├── OKXAdapter (API适配器)
    │   ├── 风控检查
    │   ├── 订单管理
    │   └── 持仓同步
    │
    ├── OptimizedStrategy (策略引擎)
    │   ├── 技术指标计算
    │   ├── 信号生成
    │   └── 动态杠杆
    │
    └── Dashboard (监控看板)
        ├── 账户信息
        ├── 持仓明细
        └── 交易记录
```

---

## 📁 文件说明

### 核心文件

- `run_live_okx.py` - 实盘运行脚本
- `execution/okx_executor.py` - OKX实盘执行器
- `core/exchange_adapter.py` - OKX API适配器
- `config/settings.yaml` - 配置文件

### 数据文件

- `data/okx_live_state.json` - 实盘状态（交易统计）
- `data/okx_orders.json` - 订单历史
- `data/okx_dashboard.html` - Dashboard HTML
- `data/okx_live_trading.log` - 运行日志

---

## 🛠️ 故障排查

### 问题1: API连接失败

**症状**: `❌ 缺少OKX API配置`

**解决**:
1. 检查环境变量是否设置
2. 检查配置文件中的API Key是否正确
3. 确认IP白名单是否配置

### 问题2: 下单失败

**症状**: `OKX下单失败`

**可能原因**:
1. 余额不足
2. 仓位超过限制
3. API权限不足
4. 网络问题

**解决**:
1. 检查账户余额
2. 检查风控规则
3. 查看日志文件

### 问题3: Dashboard无法访问

**症状**: 无法打开 http://localhost:8899

**解决**:
1. 检查dashboard_server.py是否运行
2. 检查端口8899是否被占用
3. 查看防火墙设置

---

## 🔒 安全建议

1. **API Key安全**
   - 不要将API Key提交到Git
   - 使用环境变量存储敏感信息
   - 定期更换API Key
   - 设置IP白名单

2. **资金安全**
   - 先在模拟盘测试
   - 实盘从小额开始
   - 不要投入全部资金
   - 定期检查持仓

3. **系统安全**
   - 定期备份数据文件
   - 监控系统日志
   - 设置告警通知
   - 定期更新代码

---

## 📞 支持

如有问题，请查看：
1. 日志文件: `data/okx_live_trading.log`
2. Dashboard: http://localhost:8899
3. OKX官方文档: https://www.okx.com/docs-v5/

---

## ⚡ 性能优化

### 网络优化

- 使用OKX官方API节点
- 设置合理的超时时间
- 实现重试机制

### 系统优化

- 使用systemd管理进程
- 配置日志轮转
- 定期清理历史数据

---

## 📝 更新日志

### v0.1.0 (2026-04-30)

- ✅ 完成OKX实盘执行器
- ✅ 完成实盘运行脚本
- ✅ 完成风控系统
- ✅ 完成Dashboard集成
- ✅ 完成订单跟踪

---

## 🎯 下一步计划

- [ ] WebSocket实时行情推送
- [ ] 多币种支持
- [ ] 策略回测优化
- [ ] 移动端Dashboard
- [ ] 更多技术指标

---

**免责声明**: 本系统仅供学习和研究使用，不构成投资建议。量化交易存在风险，请谨慎操作。
