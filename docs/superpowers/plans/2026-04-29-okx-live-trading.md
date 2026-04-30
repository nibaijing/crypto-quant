# OKX 实盘接入 + Dashboard 实盘监控 实施规划

> **For agentic workers:** REQUIRED: Use subagent-driven-development to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 CryptoQuant 量化系统从纯模拟盘升级为 OKX 实盘可切换双模式，Dashboard 增加实盘风控监控面板。

**Architecture:**
- 引入 **ExecutionMode** 枚举 (PAPER / LIVE / TESTNET)，统一控制所有下单路径
- `LiveExecutionEngine` — 新执行引擎，封装 OKXAdapter 的下单/平仓/查询，与现有 FuturesExecutor 接口兼容
- Dashboard 新增 **实盘监控页签**：账户权益曲线、持仓实时 PnL、强平预警、资金费率监控、API 连接状态
- 配置系统扩展：新增 `live_trading` 配置段，API Key 从环境变量/加密文件加载

**Tech Stack:** Python 3.x, OKX python-okx SDK, JSON 状态文件, Dashboard HTML/JS

---

## Chunk 1: 配置与模式系统

### Task 1.1: 扩展配置模型

**Files:**
- Modify: `core/config.py:1-155`

- [ ] **Step 1: 添加 LiveTradingConfig 和 ExecutionMode**

在 `config.py` 中新增：

```python
from enum import Enum

class ExecutionMode(str, Enum):
    PAPER = "paper"       # 本地模拟
    TESTNET = "testnet"   # OKX 模拟盘 (真实API)
    LIVE = "live"         # OKX 实盘


class LiveTradingConfig(BaseModel):
    enabled: bool = False
    mode: ExecutionMode = ExecutionMode.PAPER
    symbol: str = "BTC-USDT-SWAP"
    max_position_usd: float = 500.0
    confirm_before_order: bool = True  # 下单前需确认
    emergency_stop: bool = False       # 紧急停止


class OKXCredentials(BaseModel):
    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""


class MonitorDashboardConfig(BaseModel):
    refresh_interval_ms: int = 5000
    show_orderbook: bool = False
    price_alert_pct: float = 5.0  # 价格异动5%告警


class AppConfig(BaseSettings):
    # ... existing fields ...
    live_trading: LiveTradingConfig = LiveTradingConfig()
    okx_credentials: OKXCredentials = OKXCredentials()
    dashboard: MonitorDashboardConfig = MonitorDashboardConfig()
```

- [ ] **Step 2: 更新 YAML 默认配置**

修改 `config/settings.yaml`，新增：

```yaml
live_trading:
  enabled: false
  mode: "paper"
  symbol: "BTC-USDT-SWAP"
  max_position_usd: 500
  confirm_before_order: true
  emergency_stop: false

okx_credentials:
  api_key: ""
  api_secret: ""
  passphrase: ""
```

- [ ] **Step 3: 添加凭证安全加载**

在 `AppConfig` 中添加方法：

```python
def load_credentials_from_env(self):
    """从环境变量加载 API 凭证 (CQ_OKX_KEY / CQ_OKX_SECRET / CQ_OKX_PASSPHRASE)"""
    import os
    self.okx_credentials.api_key = os.getenv("CQ_OKX_KEY", self.okx_credentials.api_key)
    self.okx_credentials.api_secret = os.getenv("CQ_OKX_SECRET", self.okx_credentials.api_secret)
    self.okx_credentials.passphrase = os.getenv("CQ_OKX_PASSPHRASE", self.okx_credentials.passphrase)
```

- [ ] **Step 4: 验证**

```bash
cd /home/ni/crypto_quant && python3 -c "
from core.config import AppConfig, ExecutionMode
c = AppConfig.from_yaml()
print(f'Mode: {c.live_trading.mode}')
print(f'Paper? {c.live_trading.mode == ExecutionMode.PAPER}')
" 
```

Expected: `Mode: paper` / `Paper? True`

---

## Chunk 2: 实盘执行引擎

### Task 2.1: LiveExecutionEngine

**Files:**
- Create: `execution/live_engine.py`

- [ ] **Step 1: 创建引擎骨架**

```python
#!/usr/bin/env python3
"""实盘执行引擎 — 封装 OKXAdapter，向下兼容 FuturesExecutor 接口"""

import logging
import time
from datetime import datetime
from typing import Optional

from core.config import get_config, ExecutionMode
from core.exchange_adapter import OKXAdapter, OrderSide, OrderType
from execution.executor_v2 import LivePosition, LiveOrder, LiveAccount

logger = logging.getLogger(__name__)


class LiveExecutionEngine:
    """实盘/模拟盘双模式执行引擎
    
    接口与 FuturesExecutor 兼容:
      - buy(symbol, size, price) -> order_id
      - short_sell(symbol, size, price) -> order_id
      - sell(symbol, price, size) -> order_id
      - short_cover(symbol, price, size) -> order_id
      - get_account() -> LiveAccount
      - update_price(symbol, price)
      - set_leverage(lev)
    """
    
    def __init__(self):
        config = get_config()
        config.load_credentials_from_env()
        
        self._mode = config.live_trading.mode
        self._config = config
        
        self._adapter = OKXAdapter(config)
        self._state_file = config.project_root / "data" / "live_state.json"
        
        # 从 OKX 同步初始状态
        if self._mode != ExecutionMode.PAPER:
            self._sync_from_exchange()
        
        logger.info(f"实盘引擎初始化 | mode={self._mode.value}")
    
    @property
    def mode(self) -> ExecutionMode:
        return self._mode
    
    @property
    def position(self) -> Optional[LivePosition]:
        """当前持仓 (从 OKX 同步)"""
        ...

    def _sync_from_exchange(self):
        """从 OKX 同步账户和持仓"""
        ...

    def buy(self, symbol: str, size: float = None, price: float = None) -> Optional[str]:
        """做多"""
        ...

    def short_sell(self, symbol: str, size: float = None, price: float = None) -> Optional[str]:
        """做空"""
        ...

    def sell(self, symbol: str, price: float, size: float = None) -> Optional[str]:
        """平多"""
        ...

    def short_cover(self, symbol: str, price: float, size: float = None) -> Optional[str]:
        """平空"""
        ...
```

- [ ] **Step 2: 实现 PAPER 模式 (直接复用 FuturesExecutor 逻辑)**

PAPER 模式下行为与当前模拟盘完全一致，不调用 API。

- [ ] **Step 3: 实现 TESTNET/LIVE 模式**

```python
def _place_order_okx(self, symbol: str, side: str, size: float, 
                      price: float = None, pos_side: str = None) -> Optional[str]:
    """通过 OKX 下单"""
    if self._mode == ExecutionMode.PAPER:
        return self._paper_place_order(symbol, side, size, price)
    
    if self._config.live_trading.confirm_before_order:
        logger.warning(f"⚠️ 待确认: {symbol} {side} {size}张 @ {price}")
        # TODO: 通过 Telegram/Hermes 确认机制
        return None
    
    return self._adapter.place_order(
        symbol=symbol,
        side=side,
        size=size,
        price=price,
        pos_side=pos_side,
    )
```

- [ ] **Step 4: 紧急停止机制**

```python
def emergency_stop(self):
    """紧急平仓所有持仓"""
    logger.critical("🚨 紧急停止: 平仓所有合约")
    for pos in self._adapter.get_positions():
        if pos.side == "long":
            self._adapter.place_order(pos.symbol, "sell", size=pos.size)
        else:
            self._adapter.place_order(pos.symbol, "buy", size=pos.size, pos_side="short")
    self._config.live_trading.emergency_stop = True
```

- [ ] **Step 5: 测试 (PAPER 模式)**

```bash
cd /home/ni/crypto_quant && python3 -c "
from execution.live_engine import LiveExecutionEngine
e = LiveExecutionEngine()
print(f'Mode: {e.mode}')
print(f'Has position: {e.position is not None}')
"
```

---

## Chunk 3: 风控引擎

### Task 3.1: RiskManager

**Files:**
- Create: `execution/risk_manager.py`

风控规则：
1. **仓位上限** — 单边不超过 `max_position_usd`
2. **连续亏损熔断** — 连亏 N 笔自动切 PAPER
3. **强平预警** — 标记价格接近强平价时告警
4. **资金费率监控** — 费率 >0.1% 时不开新仓
5. **API 异常熔断** — OKX 返回错误连续 3 次暂停

- [ ] **Step 1: 实现**

```python
class RiskManager:
    def __init__(self, config):
        self.consecutive_losses = 0
        self.max_losses = config.risk.max_consecutive_losses
        self.max_position_usd = config.live_trading.max_position_usd
        self.api_errors = 0
        self._halted = False
    
    def can_open_position(self, side: str, size_usd: float) -> bool:
        """检查是否允许开仓"""
        ...
    
    def check_liquidation_risk(self, mark_price: float, liq_price: float) -> str:
        """检查强平风险: SAFE / WARNING / DANGER"""
        ...
    
    def record_trade(self, pnl: float):
        """记录交易结果"""
        ...
```

---

## Chunk 4: Dashboard 实盘监控

### Task 4.1: 实盘 API 端点

**Files:**
- Create: `dashboard_live_data.py`

新增 `/api/live` 端点，返回实盘专属数据：

```python
def get_live_metrics():
    return {
        "mode": "paper",  # paper / testnet / live
        "connected": True,
        "account": {
            "total_equity": 1016.63,
            "available_balance": 1016.63,
            "unrealized_pnl": 0,
            "margin_ratio": 0,
        },
        "positions": [...],
        "risk": {
            "liquidation_risk": "SAFE",  # SAFE / WARNING / DANGER
            "consecutive_losses": 0,
            "funding_rate": 0.0001,
            "funding_next_time": "2026-04-29 22:00:00",
        },
        "api_status": "ok",  # ok / degraded / disconnected
    }
```

- [ ] **Step 1: 实现数据采集**

- [ ] **Step 2: 注册到 dashboard_server.py**

```python
elif self.path == '/api/live':
    data = get_live_metrics()
    ...
```

### Task 4.2: Dashboard HTML 实盘面板

**Files:**
- Modify: `data/dashboard.html`

- [ ] **Step 1: 模式指示器 (SIM → LIVE/TESTNET 切换图标)**

```
┌─────────────────────────────────────────┐
│ ● LIVE  ●●●●● OKX 已连接    [紧急停止]   │
│ 权益 $1,016.63  |  可用 $1,016.63       │
│ 保证金率 0%  |  未实现盈亏 $0            │
├─────────────────────────────────────────┤
│ 风控状态                                 │
│ 🟢 强平风险: SAFE                        │
│ 🟢 资金费率: 0.01% (下次 3h后)           │
│ 🟢 API 状态: 正常                        │
│ 🟢 连续亏损: 0/5                         │
├─────────────────────────────────────────┤
│ 活跃订单                                 │
│ (空)                                     │
└─────────────────────────────────────────┘
```

- [ ] **Step 2: 实现 JS 渲染**

- [ ] **Step 3: 添加 5 秒自动刷新 (实盘面板专属)**

---

## Chunk 5: 集成 run_live_ws → 实盘引擎

### Task 5.1: 模式切换

**Files:**
- Modify: `run_live_ws.py`

- [ ] **Step 1: executor 工厂函数**

```python
def create_executor():
    config = get_config()
    if config.live_trading.mode == ExecutionMode.PAPER:
        return FuturesExecutor()
    else:
        return LiveExecutionEngine()
```

- [ ] **Step 2: 替换 executor 初始化**

```python
executor = create_executor()
```

- [ ] **Step 3: 确认信号处理兼容**

`run_live_ws` 中信号 → executor 调用路径不变 (`executor.buy/sell/short_sell/short_cover`)，`LiveExecutionEngine` 实现了相同接口。

- [ ] **Step 4: 集成测试**

```bash
cd /home/ni/crypto_quant && python3 run_live_ws.py
# 确认 PAPER 模式下行为与之前完全一致
```

---

## Chunk 6: 实盘前安全检查清单

- [ ] API Key 只读模式先行验证（查询余额/持仓，不下单）
- [ ] 最小仓位测试（0.001 BTC 级别）
- [ ] 设置 `confirm_before_order: true`，所有下单需确认
- [ ] Telegram 通知集成（Hermes → Telegram 桥接）
- [ ] 紧急停止按钮测试（Dashboard 一键平仓）
- [ ] 网络断开自动切 PAPER（连续 3 次 API 超时）
- [ ] 资金费率定时检查（每小时）
- [ ] 写一份 README-LIVE.md 操作手册

---

## 执行顺序

1. **Chunk 1** — 配置先行 (30 min)
2. **Chunk 2** — 引擎核心 (1h)
3. **Chunk 3** — 风控 (30 min)
4. **Chunk 4** — Dashboard (1h)
5. **Chunk 5** — 集成 (30 min)
6. **Chunk 6** — 安全检查 (持续)

**总计预估：4-5 小时**

---

> **安全第一原则：** 实盘模式默认关闭 (`enabled: false`)，需显式配置 + 环境变量 API Key 才会激活。PAPER 模式下所有改动零风险。