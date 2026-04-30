"""事件驱动的回测引擎

支持:
- 现货 + 合约混合回测
- 模拟撮合（市价/限价）
- 滑点模拟
- 手续费扣除
- 资金费率结算（永续合约）
- 强平模拟
- 完整的性能报告
"""

import logging
from typing import Optional, Dict, List, Any, Callable, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from core.config import get_config, AppConfig, BacktestConfig

logger = logging.getLogger(__name__)


# ===== 回测事件类型 =====

class EventType(str, Enum):
    MARKET_DATA = "MARKET_DATA"    # 行情数据到达
    SIGNAL = "SIGNAL"              # 策略信号
    ORDER = "ORDER"                 # 订单
    FILL = "FILL"                  # 成交
    FUNDING = "FUNDING"            # 资金费率结算
    LIQUIDATION = "LIQUIDATION"    # 强平


@dataclass
class Event:
    """回测事件"""
    type: EventType
    timestamp: int  # ms
    data: Dict = field(default_factory=dict)


# ===== 订单 / 成交 =====

class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class SimOrder:
    """模拟订单"""
    order_id: str
    symbol: str
    side: str  # buy/sell
    order_type: str  # market/limit
    size: float
    price: Optional[float] = None  # 限价
    pos_side: Optional[str] = None  # long/short (合约)
    status: OrderStatus = OrderStatus.PENDING
    filled_price: float = 0
    commission: float = 0
    timestamp: int = 0


@dataclass
class Fill:
    """成交记录"""
    timestamp: int
    symbol: str
    side: str
    size: float
    price: float
    commission: float
    pos_side: Optional[str] = None


# ===== 模拟持仓 =====

@dataclass
class SimPosition:
    """模拟持仓"""
    symbol: str
    side: str           # long/short/net
    size: float         # 持仓数量 (币数量，非合约张数)
    avg_price: float    # 开仓均价
    leverage: int = 1   # 杠杆倍数
    margin: float = 0   # 保证金
    funding_paid: float = 0  # 累计资金费率支出
    
    @property
    def unrealized_pnl(self, current_price: float) -> float:
        """未实现盈亏"""
        if self.size == 0:
            return 0
        if self.side == "long":
            return (current_price - self.avg_price) * self.size
        elif self.side == "short":
            return (self.avg_price - current_price) * self.size
        return (current_price - self.avg_price) * self.size
    
    def calc_liquidation_price(self, buffer: float = 0.1) -> float:
        """估算强平价格"""
        if self.size == 0 or self.leverage == 1:
            return 0
        
        if self.side == "long":
            # 做多强平价 ≈ 开仓价 * (1 - 1/杠杆 + 缓冲)
            liq = self.avg_price * (1 - 1 / self.leverage + buffer)
        else:
            liq = self.avg_price * (1 + 1 / self.leverage - buffer)
        
        return liq


# ===== 回测引擎 =====

class BacktestEngine:
    """事件驱动的回测引擎
    
    使用方式:
        engine = BacktestEngine(data=df, initial_capital=10000)
        engine.register_strategy(my_strategy)
        results = engine.run()
        engine.plot_equity_curve()
        engine.generate_report()
    """
    
    def __init__(self, 
                 data: pd.DataFrame,
                 initial_capital: float = None,
                 commission: float = None,
                 slippage: float = None,
                 config: AppConfig = None):
        """
        Args:
            data: K线 DataFrame (columns: timestamp, open, high, low, close, volume)
            initial_capital: 初始资金 (USDT)
            commission: 手续费比例 (如 0.0005 = 0.05%)
            slippage: 滑点比例
        """
        self.config = config or get_config()
        bt = self.config.backtest
        
        self.data = data.copy()
        self.initial_capital = initial_capital or bt.initial_capital
        self.commission = commission if commission is not None else bt.commission
        self.slippage = slippage if slippage is not None else bt.slippage
        
        # 确保数据按时间排序
        if "timestamp" in self.data.columns:
            self.data.sort_values("timestamp", inplace=True)
            self.data.reset_index(drop=True, inplace=True)
        
        # 账户状态
        self.cash = self.initial_capital
        self.position: Optional[SimPosition] = SimPosition(
            symbol="", side="net", size=0, avg_price=0,
        )
        self.equity: List[float] = []          # 权益曲线
        self.equity_timestamps: List[int] = []   # 对应时间戳
        
        # 交易记录
        self.fills: List[Fill] = []
        self.orders: List[SimOrder] = []
        
        # 策略
        self.strategies: List[Callable] = []
        self._signal_handler: Optional[Callable] = None
        
        # 资金费率 (从配置读取)
        self.funding_rate = bt.default_funding_rate
        
        # 计数器
        self._order_counter: int = 0
        self._bar_index: int = 0
        
        # 回测统计
        self.total_commission: float = 0
        self.total_funding: float = 0
        self.total_trades: int = 0
        self.winning_trades: int = 0
        self.losing_trades: int = 0
        self._trade_pnl_history: List[float] = []
        
        logger.info(f"回测引擎初始化 | 数据: {len(data)} 条 | 初始资金: {initial_capital}")
    
    def register_strategy(self, strategy_fn: Callable):
        """注册策略函数
        
        strategy_fn(bar_data, engine) -> signal: str or None
            bar_data: 包含当前K线 + 历史数据的 dict
            engine: 回测引擎实例
        
        signal 可以为:
            - "BUY" / "buy" : 做多
            - "SELL" / "sell" : 平多 / 做空
            - None: 无信号
        """
        self.strategies.append(strategy_fn)
    
    def on_signal(self, handler: Callable):
        """注册信号处理器
        
        handler(signal, bar_data, engine) -> None
        """
        self._signal_handler = handler
    
    def run(self) -> Dict[str, Any]:
        """运行回测"""
        logger.info("=" * 60)
        logger.info("🚀 开始回测...")
        logger.info(f"数据周期: {len(self.data)} 条K线")
        logger.info(f"初始资金: {self.initial_capital:,.2f} USDT")
        logger.info("=" * 60)
        
        for i in range(len(self.data)):
            self._bar_index = i
            bar = self._get_bar(i)
            
            # 资金费率结算 (每8小时)
            if i > 0 and self.position.size > 0:
                self._settle_funding(bar)
            
            # 检查强平
            if self.position.size > 0 and self.position.leverage > 1:
                liq_price = self.position.calc_liquidation_price(
                    self.config.futures.liquidation_buffer
                )
                if self.position.side == "long" and bar["low"] <= liq_price:
                    self._liquidate(bar, liq_price)
                elif self.position.side == "short" and bar["high"] >= liq_price:
                    self._liquidate(bar, liq_price)
            
            # 执行策略
            signal = None
            for strategy in self.strategies:
                try:
                    s = strategy(self._get_bar_data(i), self)
                    if s:
                        signal = s
                        break
                except Exception as e:
                    logger.error(f"策略执行错误: {e}", exc_info=True)
            
            # 处理信号
            if signal and self._signal_handler:
                try:
                    self._signal_handler(signal, self._get_bar_data(i), self)
                except Exception as e:
                    logger.error(f"信号处理错误: {e}", exc_info=True)
            
            # 记录权益
            equity = self.cash + self._position_value(bar["close"])
            self.equity.append(equity)
            self.equity_timestamps.append(bar["timestamp"])
        
        logger.info("✅ 回测完成！")
        
        return self._generate_results()
    
    def buy(self, size: float = None, price: float = None, symbol: str = None):
        """做多 / 买入"""
        bar = self._get_bar(self._bar_index)
        fill_price = self._apply_slippage(bar["close"], "buy")
        
        # 计算购买力
        if size is None:
            # 满仓买入 (合约用杠杆)
            max_position = (self.cash * self.position.leverage) / fill_price
            size = max_position * self.config.risk.max_total_position_pct
        
        cost = size * fill_price
        
        # 资金检查
        if cost > self.cash * self.position.leverage:
            logger.warning(f"资金不足: 需要 {cost:.2f}, 可用 {self.cash * self.position.leverage:.2f}")
            return None
        
        commission = cost * self.commission
        
        # 更新持仓均价
        if self.position.size > 0 and self.position.side == "long":
            total_value = self.position.size * self.position.avg_price + cost
            new_size = self.position.size + size
            avg_price = total_value / new_size if new_size > 0 else 0
            self.position.size = new_size
            self.position.avg_price = avg_price
        else:
            self.position.symbol = symbol or self.config.symbol
            self.position.side = "long"
            self.position.size = size
            self.position.avg_price = fill_price
        
        self.position.leverage = self.config.futures.default_leverage
        
        # 扣除现金 + 手续费
        self.cash -= cost
        self.cash -= commission
        self.total_commission += commission
        
        # 记录成交
        self._order_counter += 1
        fill = Fill(
            timestamp=bar["timestamp"],
            symbol=self.position.symbol,
            side="buy",
            size=size,
            price=fill_price,
            commission=commission,
            pos_side="long",
        )
        self.fills.append(fill)
        self.total_trades += 1
        
        return fill
    
    def sell(self, size: float = None, price: float = None):
        """卖出 / 平仓"""
        if self.position.size <= 0:
            logger.warning("无持仓可卖")
            return None
        
        bar = self._get_bar(self._bar_index)
        fill_price = self._apply_slippage(bar["close"], "sell")
        
        sell_size = size if size else self.position.size
        if sell_size > self.position.size:
            sell_size = self.position.size
        
        revenue = sell_size * fill_price
        commission = revenue * self.commission
        
        # 计算盈亏
        if self.position.side == "long":
            trade_pnl = (fill_price - self.position.avg_price) * sell_size
        
        # 更新持仓
        self.position.size -= sell_size
        
        # 回笼现金
        self.cash += revenue - commission
        self.total_commission += commission
        
        # 记录成交
        self._order_counter += 1
        fill = Fill(
            timestamp=bar["timestamp"],
            symbol=self.position.symbol,
            side="sell",
            size=sell_size,
            price=fill_price,
            commission=commission,
        )
        self.fills.append(fill)
        
        # 跟踪盈亏
        if "trade_pnl" in dir():
            self._trade_pnl_history.append(trade_pnl)
            if trade_pnl > 0:
                self.winning_trades += 1
            else:
                self.losing_trades += 1
        
        # 全部平仓后重置
        if self.position.size == 0:
            self.position.leverage = 1
            self.position.funding_paid = 0
        
        return fill
    
    def short_sell(self, size: float = None, price: float = None):
        """做空 (合约)"""
        bar = self._get_bar(self._bar_index)
        fill_price = self._apply_slippage(bar["close"], "sell")
        
        if size is None:
            max_position = (self.cash * self.position.leverage) / fill_price
            size = max_position * self.config.risk.max_single_position_pct
        
        cost = size * fill_price
        commission = cost * self.commission
        
        # 开空：保证金冻结
        margin = cost / self.config.futures.default_leverage
        
        if margin > self.cash:
            logger.error(f"保证金不足: 需要 {margin:.2f}, 可用 {self.cash:.2f}")
            return None
        
        # 更新持仓
        if self.position.size > 0 and self.position.side == "short":
            total_value = self.position.size * self.position.avg_price + cost
            new_size = self.position.size + size
            avg_price = total_value / new_size if new_size > 0 else 0
            self.position.size = new_size
            self.position.avg_price = avg_price
        else:
            self.position.symbol = self.config.symbol
            self.position.side = "short"
            self.position.size = size
            self.position.avg_price = fill_price
        
        self.position.leverage = self.config.futures.default_leverage
        
        self.cash -= commission
        self.total_commission += commission
        
        # 记录
        self._order_counter += 1
        fill = Fill(
            timestamp=bar["timestamp"],
            symbol=self.position.symbol,
            side="sell",
            size=size,
            price=fill_price,
            commission=commission,
            pos_side="short",
        )
        self.fills.append(fill)
        self.total_trades += 1
        
        return fill
    
    def short_cover(self, size: float = None, price: float = None):
        """平空"""
        if self.position.size <= 0 or self.position.side != "short":
            return None
        
        bar = self._get_bar(self._bar_index)
        fill_price = self._apply_slippage(bar["close"], "buy")
        
        cover_size = size if size else self.position.size
        if cover_size > self.position.size:
            cover_size = self.position.size
        
        cost = cover_size * fill_price
        commission = cost * self.commission
        
        # 空头盈亏: 做空价 - 平仓价
        trade_pnl = (self.position.avg_price - fill_price) * cover_size
        
        # 更新持仓
        self.position.size -= cover_size
        
        self.cash -= commission
        self.total_commission += commission
        
        # 回笼保证金 + 盈亏
        # (简化: 直接调整 cash 来反映盈亏)
        self.cash += trade_pnl
        
        # 记录
        self._order_counter += 1
        fill = Fill(
            timestamp=bar["timestamp"],
            symbol=self.position.symbol,
            side="buy",
            size=cover_size,
            price=fill_price,
            commission=commission,
            pos_side=None,
        )
        self.fills.append(fill)
        
        self._trade_pnl_history.append(trade_pnl)
        if trade_pnl > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1
        
        if self.position.size == 0:
            self.position.leverage = 1
            self.position.funding_paid = 0
        
        return fill
    
    def set_leverage(self, leverage: int):
        """设置杠杆倍数"""
        max_lev = self.config.futures.max_leverage
        self.position.leverage = min(leverage, max_lev)
        logger.info(f"杠杆设置为: {self.position.leverage}x")
    
    # ===== 内部方法 =====
    
    def _get_bar(self, index: int) -> Dict:
        """获取单根K线数据"""
        row = self.data.iloc[index]
        return {
            "timestamp": row["timestamp"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
        }
    
    def _get_bar_data(self, index: int) -> Dict:
        """获取策略所需的完整数据 (当前K线 + 历史切片)"""
        bar = self._get_bar(index)
        
        # 提供历史数据 (最多500根)
        lookback = 500
        start = max(0, index - lookback)
        history = self.data.iloc[start:index + 1].copy()
        
        bar["history"] = history
        bar["index"] = index
        bar["position"] = self.position
        
        # 提供常用技术指标值 (如果已预计算)
        row = self.data.iloc[index]
        for col in ["ma_7", "ma_25", "ma_99", "rsi", "macd", "macd_signal", 
                     "macd_hist", "volatility", "volume_ma"]:
            if col in self.data.columns:
                val = row.get(col)
                if pd.notna(val):
                    bar[col] = val
        
        return bar
    
    def _apply_slippage(self, price: float, side: str) -> float:
        """应用滑点"""
        if side == "buy":
            return price * (1 + self.slippage)
        else:
            return price * (1 - self.slippage)
    
    def _position_value(self, current_price: float) -> float:
        """计算持仓市值"""
        if self.position.size <= 0:
            return 0
        return self.position.size * current_price
    
    def _settle_funding(self, bar: Dict):
        """资金费率结算 (每8小时)"""
        # 简化: 每根K线检测是否需要结算
        # 实际OKX是每8小时结算一次 (00:00, 08:00, 16:00 UTC)
        ts = bar["timestamp"]
        dt = datetime.fromtimestamp(ts / 1000)
        
        if dt.hour % 8 == 0 and dt.minute == 0:
            position_value = self.position.size * bar["close"]
            funding_payment = position_value * self.funding_rate
            
            if self.position.side == "long":
                # 多头支付资金费率 (正费率时)
                self.cash -= funding_payment
            elif self.position.side == "short":
                # 空头收取资金费率 (正费率时)
                self.cash += funding_payment
            
            self.position.funding_paid += abs(funding_payment)
            self.total_funding += abs(funding_payment)
    
    def _liquidate(self, bar: Dict, liq_price: float):
        """强平处理"""
        logger.warning(f"🛑 触发强平! 强平价: {liq_price:.2f}, 当前: {bar['close']:.2f}")
        
        # 记录强平前的持仓价值归零
        self.position.size = 0
        self.position.leverage = 1
        self.position.funding_paid = 0
        
        # 扣除强平罚金 (约2%的仓位价值)
        # penalty = self.cash * 0.02
        # self.cash -= penalty
        # self.total_commission += penalty
    
    # ===== 结果和报告 =====
    
    def _generate_results(self) -> Dict[str, Any]:
        """生成回测结果摘要"""
        final_equity = self.equity[-1] if self.equity else self.initial_capital
        total_return = (final_equity - self.initial_capital) / self.initial_capital
        
        # 计算最大回撤
        equity_series = pd.Series(self.equity)
        peak = equity_series.cummax()
        drawdown = (equity_series - peak) / peak
        max_drawdown = drawdown.min()
        
        # 计算夏普比率 (无风险利率假设为0)
        daily_returns = equity_series.pct_change().dropna()
        sharpe = 0
        if len(daily_returns) > 1 and daily_returns.std() > 0:
            # 年化: 假设每小时数据，一年8760个小时
            sharpe = (daily_returns.mean() / daily_returns.std()) * np.sqrt(8760)
        
        # 胜率
        win_rate = self.winning_trades / max(self.total_trades, 1)
        
        # 盈亏比
        avg_win = np.mean([p for p in self._trade_pnl_history if p > 0]) if any(p > 0 for p in self._trade_pnl_history) else 0
        avg_loss = abs(np.mean([p for p in self._trade_pnl_history if p < 0])) if any(p < 0 for p in self._trade_pnl_history) else 1
        profit_factor = avg_win / avg_loss if avg_loss > 0 else float("inf")
        
        results = {
            "initial_capital": self.initial_capital,
            "final_equity": final_equity,
            "total_return_pct": total_return * 100,
            "max_drawdown_pct": max_drawdown * 100,
            "sharpe_ratio": sharpe,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate_pct": win_rate * 100,
            "profit_factor": profit_factor,
            "total_commission": self.total_commission,
            "total_funding": self.total_funding,
            "equity_curve": list(zip(self.equity_timestamps, self.equity)),
            "fills": self.fills,
        }
        
        # 打印结论
        logger.info("=" * 60)
        logger.info("📊 回测结果")
        logger.info(f"   最终权益:   {final_equity:>10,.2f} USDT")
        logger.info(f"   总收益率:   {total_return * 100:>10.2f}%")
        logger.info(f"   最大回撤:   {max_drawdown * 100:>10.2f}%")
        logger.info(f"   夏普比率:   {sharpe:>10.2f}")
        logger.info(f"   交易次数:   {self.total_trades:>10}")
        logger.info(f"   胜率:       {win_rate * 100:>10.1f}%")
        logger.info(f"   盈亏比:     {profit_factor:>10.2f}")
        logger.info(f"   总手续费:   {self.total_commission:>10,.2f} USDT")
        logger.info(f"   总资金费率: {self.total_funding:>10,.2f} USDT")
        logger.info("=" * 60)
        
        return results
    
    def plot_equity_curve(self, save_path: str = None, show: bool = False):
        """绘制权益曲线"""
        if not self.equity:
            logger.warning("无权益数据可绘制")
            return
        
        fig, axes = plt.subplots(2, 1, figsize=(14, 10), 
                                  gridspec_kw={"height_ratios": [3, 1]})
        
        # 权益曲线
        ax1 = axes[0]
        dates = [datetime.fromtimestamp(ts / 1000) for ts in self.equity_timestamps]
        ax1.plot(dates, self.equity, color="#2962FF", linewidth=1.5, label="权益曲线")
        ax1.axhline(y=self.initial_capital, color="gray", linestyle="--", 
                     alpha=0.5, label=f"初始资金 ({self.initial_capital:,.0f})")
        ax1.fill_between(dates, self.initial_capital, self.equity,
                          where=np.array(self.equity) >= self.initial_capital,
                          color="green", alpha=0.1, label="盈利区间")
        ax1.fill_between(dates, self.initial_capital, self.equity,
                          where=np.array(self.equity) < self.initial_capital,
                          color="red", alpha=0.1, label="亏损区间")
        
        ax1.set_title("权益曲线 (Equity Curve)", fontsize=14, fontweight="bold")
        ax1.set_ylabel("权益 (USDT)", fontsize=12)
        ax1.legend(loc="upper left")
        ax1.grid(True, alpha=0.3)
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
        
        # 回撤曲线
        ax2 = axes[1]
        equity_series = pd.Series(self.equity)
        peak = equity_series.cummax()
        drawdown = (equity_series - peak) / peak * 100
        ax2.fill_between(dates, drawdown, 0, color="red", alpha=0.3)
        ax2.plot(dates, drawdown, color="#D32F2F", linewidth=1)
        ax2.set_title("回撤 (Drawdown)", fontsize=12)
        ax2.set_ylabel("回撤 (%)", fontsize=12)
        ax2.set_xlabel("日期", fontsize=12)
        ax2.grid(True, alpha=0.3)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
        
        # 添加最大回撤标注
        max_dd_idx = drawdown.idxmin()
        if pd.notna(max_dd_idx):
            ax2.annotate(f"最大回撤: {drawdown[max_dd_idx]:.1f}%",
                         xy=(dates[max_dd_idx], drawdown[max_dd_idx]),
                         xytext=(dates[max_dd_idx], drawdown[max_dd_idx] - 5),
                         arrowprops=dict(arrowstyle="->", color="black"),
                         fontsize=10)
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches="tight")
            logger.info(f"权益曲线已保存: {save_path}")
        
        if show:
            plt.show()
        
        return fig
    
    def plot_trades(self, save_path: str = None, show: bool = False):
        """绘制成交点位标注在价格图上"""
        if not self.fills:
            logger.warning("无成交记录")
            return
        
        fig, ax = plt.subplots(figsize=(14, 7))
        
        # 价格曲线
        dates = [datetime.fromtimestamp(ts / 1000) for ts in self.data["timestamp"]]
        ax.plot(dates, self.data["close"], color="#2962FF", linewidth=1, alpha=0.7, label="收盘价")
        
        # 标注买卖点
        buy_dates, buy_prices = [], []
        sell_dates, sell_prices = [], []
        
        for fill in self.fills:
            dt = datetime.fromtimestamp(fill.timestamp / 1000)
            if fill.side == "buy":
                buy_dates.append(dt)
                buy_prices.append(fill.price)
            else:
                sell_dates.append(dt)
                sell_prices.append(fill.price)
        
        if buy_dates:
            ax.scatter(buy_dates, buy_prices, color="green", marker="^", 
                       s=80, zorder=5, label=f"买入 ({len(buy_dates)}次)")
        if sell_dates:
            ax.scatter(sell_dates, sell_prices, color="red", marker="v",
                       s=80, zorder=5, label=f"卖出 ({len(sell_dates)}次)")
        
        ax.set_title("成交点位图 (Trade Points)", fontsize=14, fontweight="bold")
        ax.set_ylabel("价格 (USDT)", fontsize=12)
        ax.legend(loc="upper left")
        ax.grid(True, alpha=0.3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches="tight")
        
        if show:
            plt.show()
        
        return fig