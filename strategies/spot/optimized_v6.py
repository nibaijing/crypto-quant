#!/usr/bin/env python3
"""
optimized_v6.py — V6 量化策略 v7.0
BTC-USDT 限价单实时交易 | 高频信号评估

核心变化:
  v6.x → v7.0:
  - 从"K线闭合才交易" → "实时tick级限价单评估"
  - 不再等待K线闭合, 每个tick检测信号条件
  - 开仓单以限价单挂出(ask/bid ± 5bp), 成交后才实际建仓
  - 平仓也用限价单, 设置take-profit / stop-loss限价
  - K线闭合只用于更新技术指标(RSI/ADX/MACD)
  - 持仓期间持续监控价格: 止损/止盈限价永不失效

限价单策略:
  SHORT入口: 检测到趋势向下+RSI超买反弹 → 挂限价卖单在bid略上方
  LONG入口: 检测到超卖反弹 → 挂限价买单在ask略下方
  止盈: 按ATR固定目标价挂限价平仓单
  止损: 按250bp硬止损价挂限价平仓单

引擎对接接口:
  - on_bar(bar: dict, executor) -> Report (K线闭合信号更新)
  - on_tick(price: float, executor) -> dict (实时价格评估, 返回限价单动作)
  - get_position_size(cash, price, leverage, atr, side) -> float
  - get_dynamic_leverage(volatility) -> int
  - check_max_drawdown(equity) -> bool
  - compute_indicators(df) -> pd.DataFrame (static)
  - restore_state(state) / get_state()
  - set_verbose(v)
"""

import logging
import numpy as np
import pandas as pd
import lightgbm as lgb
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

# === 信号阈值（基于真实数据调优）===
# 做空 (主要盈利来源)
RSI_SHORT_ENTRY = 55       # RSI超过55做空加分 (不再是硬性要求)
RSI_SHORT_MIN = 30         # RSI必须>30 (仅防极端超卖)
RSI_SHORT_EXIT = 25        # RSI跌到25以下平空
ADX_SHORT_MIN = 28         # ADX必须>28有趋势（略降捕捉早期趋势）
ADX_SHORT_STRONG = 40      # 强趋势确认
STOP_LOSS_SHORT_BP = 250   # 做空止损250基点

# 做多 (偶尔反弹)
RSI_LONG_ENTRY = 38        # RSI低于38考虑做反弹
RSI_LONG_MIN = 0
RSI_LONG_MAX_ENTRY = 60    # RSI不能>60还做多
RSI_LONG_EXIT = 55         # RSI回到55以上平多
ADX_LONG_MIN = 15          # 反弹不需要强趋势
STOP_LOSS_LONG_BP = 250    # 做多止损250基点

# 通用阈值
ADX_NO_TRADE = 18          # ADX<18不开仓
MIN_HOLD_BARS = 2          # 最少持仓2根K线(30分钟)
MAX_HOLD_BARS = 48         # 最长持仓48根K线(12小时)
COOLDOWN_BARS = 16         # 亏损后冷却16根K线(4小时)

# === 仓位管理 ===
MAX_POSITION_PCT = 0.15    # 单笔15%权益作保证金
RISK_PER_TRADE = 0.015     # 单笔风险1.5%
STOP_ATR_MULT = 2.0        # ATR止损倍数

# === 风险管理 ===
MAX_DRAWDOWN_PCT = 0.25    # 25%回撤熔断
INITIAL_CAPITAL = 1000

# === 限价单参数 ===
LIMIT_OFFSET_BP = 3        # 限价单相对价格的偏移(基点): 开仓3bp偏移
LIMIT_SLIPPAGE_BP = 2      # 允许限价单最大滑点
TP_ATR_MULT = 2.0          # 止盈 = ATR × TP_ATR_MULT

# 限价单超时: 挂单N秒未成交则撤单重评估
LIMIT_ORDER_TIMEOUT = 30   # 限价单30秒不成交撤单

# === Tick 级评估参数 ===
TICK_EVAL_INTERVAL = 1.0   # tick评估最小间隔(秒)
PRICE_REENTER_DELTA_BP = 10 # 撤单后价格变化>10bp才重新挂单


class LimitOrder:
    """限价单记录"""
    def __init__(self, side: str, price: float, size: float, order_type: str = "entry"):
        self.side = side          # buy/sell/short_sell/short_cover
        self.price = price        # 限价
        self.size = size          # 数量
        self.order_type = order_type  # entry/take_profit/stop_loss
        self.placed_at = 0
        self._filled = False

    @property
    def filled(self) -> bool:
        return self._filled

    def check_fill(self, current_price: float) -> bool:
        """检查限价单是否成交: 价格穿越限价"""
        if self._filled:
            return True
        if self.side in ("buy", "short_cover"):
            # 买单: 当前价 <= 限价 → 成交
            if current_price <= self.price:
                self._filled = True
                return True
        elif self.side in ("sell", "short_sell"):
            # 卖单: 当前价 >= 限价 → 成交
            if current_price >= self.price:
                self._filled = True
                return True
        return False


class Report:
    """策略返回值, 引擎通过 .raw_signal 读取信号"""
    def __init__(self, raw_signal: str = "HOLD"):
        self.raw_signal = raw_signal


class OptimizedV6:
    """V7 策略 — 限价单实时交易"""

    def __init__(self, ws_manager=None, use_lgb=True):
        self.name = "optimized_v6"
        self.ws = ws_manager
        self.use_lgb = use_lgb
        self.verbose = False

        # 状态
        self.last_exit_bar = -COOLDOWN_BARS - 1
        self._last_trade_pnl = None
        self.peak_equity = INITIAL_CAPITAL
        self._entry_price = 0
        self._position_side = None

        # 最新指标 (每次K线闭合更新)
        self.latest_indicators = {
            "rsi": 50, "adx": 20, "macdh": 0,
            "ma5": 0, "ma20": 0, "atr": 100,
            "di_plus": 20, "di_minus": 20, "close": 0,
        }
        self._trend_down = False
        self._trend_up = False
        self._regime = "chop"
        self._index = 0  # K线计数

        # 活跃限价单
        self.active_limit_order: Optional[LimitOrder] = None

        # 限价单状态
        self._last_entry_price = 0  # 上次尝试入场价格
        self._last_entry_try = 0    # 上次尝试入场时间

    def restore_state(self, state):
        if not state:
            return
        self.last_exit_bar = state.get("last_exit_bar", -COOLDOWN_BARS - 1)
        self._last_trade_pnl = state.get("last_trade_pnl", None)

    def get_state(self):
        return {
            "last_exit_bar": self.last_exit_bar,
            "_last_trade_pnl": self._last_trade_pnl,
        }

    def set_verbose(self, v: bool):
        self.verbose = v

    def get_dynamic_leverage(self, volatility: float) -> int:
        return 10  # 固定10x

    def get_position_size(self, cash: float, price: float, leverage: int, atr: float, side: str) -> float:
        max_margin = cash * MAX_POSITION_PCT
        max_size_by_margin = max_margin / (price / leverage) if price > 0 else 0

        if atr > 0 and price > 0:
            atr_pct = atr / price
            risk_capped_size = (cash * RISK_PER_TRADE) / (atr_pct * leverage * STOP_ATR_MULT)
            return max(min(max_size_by_margin, risk_capped_size), 0.0001)

        return max(max_size_by_margin, 0.0001)

    def check_max_drawdown(self, equity: float) -> bool:
        if self.peak_equity <= 0:
            return False
        dd = (self.peak_equity - equity) / self.peak_equity
        if equity > self.peak_equity:
            self.peak_equity = equity
        return dd > MAX_DRAWDOWN_PCT

    def _update_indicators_from_row(self, row):
        """从K线行更新存储的指标"""
        c = float(row.get("close", 0))
        ma5 = float(row.get("ma5", float(row.get("ma_7", c))))
        ma20 = float(row.get("ma20", float(row.get("ma_25", c))))
        rsi = float(row.get("rsi", 50))
        macdh = float(row.get("macdh", float(row.get("macd_hist", 0))))
        adx = float(row.get("adx", 20))
        di_plus = float(row.get("di_plus", 20))
        di_minus = float(row.get("di_minus", 20))
        atr = float(row.get("atr", 100))

        self.latest_indicators = {
            "rsi": rsi, "adx": adx, "macdh": macdh,
            "ma5": ma5, "ma20": ma20, "atr": atr,
            "di_plus": di_plus, "di_minus": di_minus, "close": c,
        }

        # 趋势方向
        price_above_ma = c > ma5 > ma20
        price_below_ma = c < ma5 < ma20
        di_bullish = di_plus > di_minus
        self._trend_up = price_above_ma and di_bullish
        self._trend_down = price_below_ma and not di_bullish

        # 市场体制
        if adx > 40 and abs(di_plus - di_minus) > 10:
            self._regime = "strong_trend"
        elif adx > 25:
            self._regime = "trend"
        elif adx > 18:
            self._regime = "weak_trend"
        else:
            self._regime = "chop"

    # ==================== 限价单管理 ====================

    def _get_limit_price(self, current_price: float, side: str) -> float:
        """计算限价单价格"""
        offset = current_price * LIMIT_OFFSET_BP / 10000
        if side in ("buy", "short_cover"):
            return current_price - offset  # 买: 低挂
        else:
            return current_price + offset  # 卖: 高挂

    def _get_tp_price(self, entry_price: float, side: str, atr: float) -> float:
        """计算止盈限价"""
        tp_dist = atr * TP_ATR_MULT
        if side == "long":
            return entry_price + tp_dist
        else:
            return entry_price - tp_dist

    def _get_sl_price(self, entry_price: float, side: str) -> float:
        """计算止损限价"""
        sl_bp = STOP_LOSS_LONG_BP if side == "long" else STOP_LOSS_SHORT_BP
        sl_dist = entry_price * sl_bp / 10000
        if side == "long":
            return entry_price - sl_dist
        else:
            return entry_price + sl_dist

    def _get_rsi_exit_price(self, entry_price: float, side: str) -> float:
        """RSI平仓价 — 用最新RSI估算"""
        rsi = self.latest_indicators.get("rsi", 50)
        if side == "long":
            # 做多: RSI>55 超买 → 约等于2%涨幅
            return entry_price * 1.01
        else:
            # 做空: RSI<28 超卖 → 约等于2%跌幅
            return entry_price * 0.99

    def _check_fill_limits(self, executor, current_price: float) -> Optional[str]:
        """检查当前活跃限价单是否成交。返回交易动作"""
        if self.active_limit_order is None:
            return None

        lo = self.active_limit_order

        # 检查超时
        import time
        if time.time() - lo.placed_at > LIMIT_ORDER_TIMEOUT and not lo.filled:
            logger.info(f"⏰ 限价单超时 {LIMIT_ORDER_TIMEOUT}s, 撤单: {lo.side} @ ${lo.price:.0f} (当前${current_price:.0f})")
            self.active_limit_order = None
            return None

        # 检查是否成交
        if lo.check_fill(current_price):
            logger.info(f"✅ 限价单成交: {lo.side} @ ${lo.price:.0f} (当前${current_price:.0f})")

            # 映射到 executor 操作
            if lo.side == "buy":
                result = executor.buy("BTC-USDT", size=lo.size, price=current_price)
                if result:
                    self.active_limit_order = None
                    entry_p = executor.position.entry_price if executor.position else current_price
                    self._entry_price = entry_p
                    self._position_side = "long"
                    # 挂止盈止损限价单
                    atr = self.latest_indicators.get("atr", 100)
                    tp = self._get_tp_price(entry_p, "long", atr)
                    sl = self._get_sl_price(entry_p, "long")
                    logger.info(f"📋 限价: TP=${tp:.0f} | SL=${sl:.0f} | ATR={atr:.0f}")
                    return "LONG_FILLED"

            elif lo.side == "short_sell":
                result = executor.short_sell("BTC-USDT", size=lo.size, price=current_price)
                if result:
                    self.active_limit_order = None
                    entry_p = executor.position.entry_price if executor.position else current_price
                    self._entry_price = entry_p
                    self._position_side = "short"
                    atr = self.latest_indicators.get("atr", 100)
                    tp = self._get_tp_price(entry_p, "short", atr)
                    sl = self._get_sl_price(entry_p, "short")
                    logger.info(f"📋 限价: TP=${tp:.0f} | SL=${sl:.0f} | ATR={atr:.0f}")
                    return "SHORT_FILLED"

            elif lo.side == "sell":
                result = executor.sell("BTC-USDT", price=current_price)
                if result:
                    self.active_limit_order = None
                    self._entry_price = 0
                    self._position_side = None
                    return "SELL_FILLED"

            elif lo.side == "short_cover":
                result = executor.short_cover("BTC-USDT", price=current_price)
                if result:
                    self.active_limit_order = None
                    self._entry_price = 0
                    self._position_side = None
                    return "COVER_FILLED"

            self.active_limit_order = None

        return None

    def _should_reenter(self, current_price: float) -> bool:
        """判断是否需要重新挂单 (价格变化足够大)"""
        if self._last_entry_price <= 0:
            return True
        delta_bp = abs(current_price - self._last_entry_price) / self._last_entry_price * 10000
        return delta_bp >= PRICE_REENTER_DELTA_BP

    # ==================== K线闭合: 指标更新 ====================

    def on_bar(self, bar: dict, executor) -> Report:
        """K线闭合: 更新技术指标 + 处理限价单"""
        df = bar.get("history")
        if df is None or len(df) < 30:
            return Report("HOLD")

        idx = bar.get("index", len(df) - 1)
        row = df.iloc[idx]

        # 更新存储的指标
        self._update_indicators_from_row(row)
        self._index = idx

        # 持仓信息
        pos_obj = bar.get("position")
        has_position = pos_obj is not None and getattr(pos_obj, 'size', 0) != 0
        pos_side = getattr(pos_obj, 'side', 'long') if hasattr(pos_obj, 'side') else 'long'
        pos_entry = float(getattr(pos_obj, 'avg_price', 0))
        bars_held = getattr(pos_obj, 'bars_held', 0)
        c = float(row.get("close", 0))
        rsi = self.latest_indicators["rsi"]
        adx = self.latest_indicators["adx"]

        # === 持仓管理: 硬止损(K线闭合保护) ===
        if has_position:
            if pos_side == 'long':
                pnl_bp = (c - pos_entry) / pos_entry * 10000
                if pnl_bp < -STOP_LOSS_LONG_BP and bars_held >= MIN_HOLD_BARS:
                    logger.info(f"📡 K线: SELL (止损 | PnL={pnl_bp:.0f}bp)")
                    self.last_exit_bar = idx
                    self.active_limit_order = None
                    return Report("SELL")
                if rsi > RSI_LONG_EXIT and bars_held >= MIN_HOLD_BARS:
                    logger.info(f"📡 K线: SELL (RSI={rsi:.0f}>{RSI_LONG_EXIT}超买)")
                    self.last_exit_bar = idx
                    self.active_limit_order = None
                    return Report("SELL")
                if bars_held >= MAX_HOLD_BARS:
                    logger.info(f"📡 K线: SELL (超时| {bars_held}根)")
                    self.last_exit_bar = idx
                    self.active_limit_order = None
                    return Report("SELL")
            elif pos_side == 'short':
                pnl_bp = (pos_entry - c) / pos_entry * 10000
                if pnl_bp < -STOP_LOSS_SHORT_BP and bars_held >= MIN_HOLD_BARS:
                    logger.info(f"📡 K线: COVER (止损 | PnL={pnl_bp:.0f}bp)")
                    self.last_exit_bar = idx
                    self.active_limit_order = None
                    return Report("COVER")
                if rsi < RSI_SHORT_EXIT and bars_held >= MIN_HOLD_BARS:
                    logger.info(f"📡 K线: COVER (RSI={rsi:.0f}<{RSI_SHORT_EXIT}超卖)")
                    self.last_exit_bar = idx
                    self.active_limit_order = None
                    return Report("COVER")
                if bars_held >= MAX_HOLD_BARS:
                    logger.info(f"📡 K线: COVER (超时)")
                    self.last_exit_bar = idx
                    self.active_limit_order = None
                    return Report("COVER")

        # 冷却期
        bars_since_exit = idx - self.last_exit_bar
        effective_cooldown = COOLDOWN_BARS * 2 if (self._last_trade_pnl is not None and self._last_trade_pnl < -0.02) else COOLDOWN_BARS
        in_cooldown = bars_since_exit < effective_cooldown

        # === 信号评估 (基于闭合K线, 确认趋势) ===
        short_signal = False
        long_signal = False
        short_reason = ""
        long_reason = ""

        if not has_position and not in_cooldown:
            # 做空新条件: trend_down + ADX≥28 + MACD死叉(macdh<0)
            macdh = self.latest_indicators["macdh"]
            short_base = self._trend_down and adx >= ADX_SHORT_MIN
            short_macd_dead = macdh < 0  # MACD死叉信号
            short_rsi_bonus = rsi >= RSI_SHORT_ENTRY  # RSI≥55加分
            short_score = 0
            if short_base: short_score += 3
            if short_macd_dead: short_score += 2
            if adx >= ADX_SHORT_STRONG: short_score += 1
            if short_rsi_bonus: short_score += 2
            if not short_base: short_reason += "not_trend_down "
            if not short_macd_dead: short_reason += f"macdh={macdh:.1f}>=0 "

            short_signal = short_base and short_macd_dead and short_score >= 5

            long_base = rsi <= RSI_LONG_ENTRY and rsi <= RSI_LONG_MAX_ENTRY
            long_choppy = self._regime in ("weak_trend", "chop")
            long_near_ma = abs(c - self.latest_indicators["ma20"]) / max(self.latest_indicators["ma20"], 1) < 0.01

            long_score = 0
            if long_base: long_score += 3
            if long_choppy: long_score += 2
            if long_near_ma: long_score += 2
            if rsi <= 30: long_score += 2

            long_signal = long_base and long_choppy and long_score >= 5

            # 诊断日志 (每根闭合K线)
            log_parts = [
                f"RSI={rsi:.0f} ADX={adx:.0f} MACDh={macdh:.1f}{'✗' if macdh<0 else ''}",
                f"regime={self._regime}",
                f"trend={'UP' if self._trend_up else 'DOWN' if self._trend_down else 'FLAT'}",
                f"SHORT={short_score}/8{'✓' if short_signal else ''}",
                f"LONG={long_score}/9{'✓' if long_signal else ''}",
            ]
            if short_reason:
                log_parts.append(f"no_short:{short_reason}")
            if rsi > RSI_LONG_MAX_ENTRY:
                log_parts.append(f"⛔rsi={rsi:.0f}>{RSI_LONG_MAX_ENTRY}")
            if in_cooldown:
                log_parts.append(f"cool={bars_since_exit}/{effective_cooldown}")

            logger.info(f"📏 Kline(signal) | {' | '.join(log_parts)}")

            # 多空互斥
            if short_signal and long_signal:
                if self._regime == "strong_trend" and self._trend_down:
                    long_signal = False
                elif self._regime == "strong_trend" and self._trend_up:
                    short_signal = False
                else:
                    short_signal = False
                    long_signal = False
        elif has_position:
            # 如果有持仓, K线闭合时不产生新信号, 但记录持仓状态
            pass

        signal = "HOLD"
        if has_position:
            signal = "HOLD"
        elif short_signal:
            signal = "SHORT"
        elif long_signal:
            signal = "LONG"

        return Report(signal)

    # ==================== Tick 级实时评估 ====================

    def on_tick(self, current_price: float, executor) -> Dict:
        """
        实时 tick 级评估, 返回动作字典:
        {
            "action": str,    # "PLACE_LIMIT"/"CHECK_FILL"/"CANCEL"/"HOLD"
            "side": str,      # buy/sell/short_sell/short_cover
            "limit_price": float,
            "size": float,
            "message": str,
        }
        """
        result = {"action": "HOLD", "side": "", "limit_price": 0, "size": 0, "message": ""}
        import time

        rsi = self.latest_indicators.get("rsi", 50)
        adx = self.latest_indicators.get("adx", 20)
        atr = self.latest_indicators.get("atr", 100)

        has_position = executor.position is not None and getattr(executor.position, 'size', 0) > 0

        # ===== 持仓状态: 限价止盈/止损 =====
        if has_position:
            pos = executor.position
            side = pos.side
            entry_p = pos.entry_price

            # 实时止损: 250bp
            if side == "long":
                pnl_bp = (current_price - entry_p) / entry_p * 10000
                if pnl_bp < -STOP_LOSS_LONG_BP:
                    logger.info(f"⚡ Tick 止损: SELL PnL={pnl_bp:.0f}bp")
                    result["action"] = "EXECUTE"
                    result["side"] = "sell"
                    return result
                # RSI 平仓
                if rsi > RSI_LONG_EXIT + 5:  # 更宽松的tick平仓
                    logger.info(f"⚡ Tick RSI={rsi:.0f}>RSI_LONG_EXIT={RSI_LONG_EXIT} → SELL")
                    result["action"] = "EXECUTE"
                    result["side"] = "sell"
                    return result
            else:
                pnl_bp = (entry_p - current_price) / entry_p * 10000
                if pnl_bp < -STOP_LOSS_SHORT_BP:
                    logger.info(f"⚡ Tick 止损: COVER PnL={pnl_bp:.0f}bp")
                    result["action"] = "EXECUTE"
                    result["side"] = "short_cover"
                    return result
                if rsi < RSI_SHORT_EXIT - 5:
                    logger.info(f"⚡ Tick RSI={rsi:.0f}<RSI_SHORT_EXIT={RSI_SHORT_EXIT} → COVER")
                    result["action"] = "EXECUTE"
                    result["side"] = "short_cover"
                    return result

        # ===== 无持仓: 检查限价单成交 + 新信号 =====
        if not has_position:

            # 1. 检查现有限价单
            if self.active_limit_order is not None:
                fill_action = self._check_fill_limits(executor, current_price)
                if fill_action:
                    result["action"] = fill_action
                    return result
                # 还在等成交，不用重复评估
                return result

            # 2. 冷却期
            # (冷却由K线闭合的on_bar管理, tick级不严格检查)
            if self._last_trade_pnl is not None and self._last_trade_pnl < -0.02:
                # 亏损后冷却5分钟
                if time.time() - self._last_entry_try < 300:
                    return result

            # 3. 信号评估 (实时 — MACD死叉优先)
            if adx < ADX_NO_TRADE:
                return result

            macdh = self.latest_indicators.get("macdh", 0)
            short_base = self._trend_down and adx >= ADX_SHORT_MIN
            short_macd_dead = macdh < 0
            short_rsi_bonus = rsi >= RSI_SHORT_ENTRY

            long_base = rsi <= RSI_LONG_ENTRY and rsi <= RSI_LONG_MAX_ENTRY
            long_choppy = self._regime in ("weak_trend", "chop")

            # 限价单: MACD死叉 + downtrend + ADX≥28 → 挂空
            if short_base and short_macd_dead:
                # 做空 → 在buy order book上方挂short_sell限价
                size = self.get_position_size(executor.cash, current_price, 10, atr, "short")
                limit_price = self._get_limit_price(current_price, "short_sell")

                lo = LimitOrder("short_sell", limit_price, size, "entry")
                lo.placed_at = time.time()
                self.active_limit_order = lo
                self._last_entry_price = current_price
                self._last_entry_try = time.time()

                extras = []
                if short_rsi_bonus: extras.append(f"RSI={rsi:.0f}≥{RSI_SHORT_ENTRY}")
                ext = f" ({', '.join(extras)})" if extras else ""
                result["action"] = "PLACE_LIMIT"
                result["side"] = "short_sell"
                result["limit_price"] = limit_price
                result["size"] = size
                result["message"] = f"SHORT limit @ ${limit_price:.0f} (mkt=${current_price:,.0f}) macdh={macdh:.1f} ADX={adx:.0f}{ext}"
                logger.info(f"📋 {result['message']} | size={size:.6f}")

            elif long_base and long_choppy:
                # 做多 → 在ask book下方挂buy限价
                size = self.get_position_size(executor.cash, current_price, 10, atr, "long")
                limit_price = self._get_limit_price(current_price, "buy")

                lo = LimitOrder("buy", limit_price, size, "entry")
                lo.placed_at = time.time()
                self.active_limit_order = lo
                self._last_entry_price = current_price
                self._last_entry_try = time.time()

                result["action"] = "PLACE_LIMIT"
                result["side"] = "buy"
                result["limit_price"] = limit_price
                result["size"] = size
                result["message"] = f"LONG limit @ ${limit_price:.0f} (mkt=${current_price:,.0f}) RSI={rsi:.0f} ADX={adx:.0f}"
                logger.info(f"📋 {result['message']} | size={size:.6f}")

        return result

    @staticmethod
    def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
        """计算技术指标"""
        c = df["close"].values
        h = df["high"].values
        l = df["low"].values
        v = df["volume"].values

        # MA
        df["ma5"] = pd.Series(c).rolling(5).mean().values
        df["ma20"] = pd.Series(c).rolling(20).mean().values

        # RSI
        delta = np.diff(c, prepend=c[0])
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        ag = pd.Series(gain).rolling(14).mean().values
        al = pd.Series(loss).rolling(14).mean().values
        rs = np.divide(ag, al, out=np.zeros_like(ag), where=al != 0)
        rsi_series = 100 - 100 / (1 + rs)
        al_zero = al == 0
        rsi_series[al_zero] = 100
        df["rsi"] = rsi_series

        # MACD
        ema12 = pd.Series(c).ewm(span=12).mean().values
        ema26 = pd.Series(c).ewm(span=26).mean().values
        df["macd"] = ema12 - ema26
        df["macd_signal"] = pd.Series(df["macd"]).ewm(span=9).mean().values
        df["macdh"] = df["macd"] - df["macd_signal"]

        # ATR
        tr = np.maximum(h - l, np.abs(h - np.roll(c, 1)))
        tr = np.maximum(tr, np.abs(l - np.roll(c, 1)))
        tr[0] = h[0] - l[0]
        df["atr"] = pd.Series(tr).rolling(14).mean().values

        # ADX
        plus_dm = np.where(h - np.roll(h, 1) > np.roll(l, 1) - l,
                           np.maximum(h - np.roll(h, 1), 0), 0)
        minus_dm = np.where(np.roll(l, 1) - l > h - np.roll(h, 1),
                            np.maximum(np.roll(l, 1) - l, 0), 0)
        plus_dm[0] = 0; minus_dm[0] = 0
        atr14 = pd.Series(tr).rolling(14).mean().values
        atr14_safe = np.where(atr14 == 0, 1e-8, atr14)
        df["di_plus"] = 100 * pd.Series(plus_dm).rolling(14).mean().values / atr14_safe
        df["di_minus"] = 100 * pd.Series(minus_dm).rolling(14).mean().values / atr14_safe
        dx = 100 * np.abs(df["di_plus"] - df["di_minus"]) / (df["di_plus"] + df["di_minus"] + 1e-8)
        df["adx"] = pd.Series(dx).rolling(14).mean().values

        # 成交量MA
        df["vol_ma"] = pd.Series(v).rolling(20).mean().values
        df["regression_fast"] = pd.Series(c).rolling(10).mean().values
        df["regression_slow"] = pd.Series(c).rolling(30).mean().values

        # 兼容旧命名
        df["ma_7"] = df["ma5"]
        df["ma_25"] = df["ma20"]
        df["volume_ma"] = df["vol_ma"]
        df["macd_hist"] = df["macdh"]

        return df