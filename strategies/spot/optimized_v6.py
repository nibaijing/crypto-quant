#!/usr/bin/env python3
"""
OptimizedV6 — 自动优化交易策略
基于 DualV5 回测最佳参数，加入:
1. 资金管理 (凯利公式 + 固定比例)
2. MACD 二次确认
3. ADX 过滤震荡
4. 动态止盈止损 (ATR-based)
5. 最大回撤熔断
"""

import numpy as np
import pandas as pd
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# === 从 Dual V5 回测得出的最佳参数 ===
BEST_LONG_MULT = 1.5    # 做多仓位倍数
BEST_SHORT_MULT = 2.5   # 做空仓位倍数 (做空信号更稀缺, 加倍)

# === 风控参数 ===
MAX_POSITION_PCT = 1.0  # 保证金占权益比例 (1.0 = 全仓)
ATR_STOP_LONG = 1.0      # 做多 ATR 止损倍数
ATR_STOP_SHORT = 1.5     # 做空 ATR 止损倍数 (做空更激进)
MAX_DRAWDOWN_PCT = 0.20  # 最大回撤 20% 熔断
MAX_HOLD_BARS = 32       # 最大持仓K线数 (8小时)
MIN_HOLD_BARS = 4        # 最小持仓K线数 (前4根K线不能RSI平仓, ATR止损除外)
COOLDOWN_BARS = 1         # 平仓后至少等1根K线，防AI反复开平

# === 信号阈值 ===
RSI_LONG_ENTRY = 35      # 做多: RSI < 35 (深度回调介入, 更安全)
RSI_LONG_MAX_ENTRY = 65  # 做多: RSI > 65 拒绝开仓 (拒绝追高)
RSI_LONG_EXIT = 75       # 做多平仓: RSI > 75 (让盈利奔跑)
RSI_SHORT_ENTRY = 55     # 做空: RSI > 55 (等待更强反弹再介入)
RSI_SHORT_MIN_ENTRY = 35 # 做空: RSI < 35 拒绝开仓 (拒绝追低)
RSI_SHORT_EXIT = 40      # 做空平仓: RSI < 40 (持有到超卖区域)
MACD_LONG_THRESHOLD = 20  # MACD_hist > 15 确认做多 (15m放宽, 从25下调)
MACD_SHORT_THRESHOLD = -15 # MACD_hist < -15 确认做空 (15m放宽, 从-25上调)
# === 信号权重 (加权评分替代硬否决) ===
# 不再要求 6/6 全过 — 核心条件权重高, RSI/VOL 为辅助
CONDITION_WEIGHTS = {
    "MA":   0.20,  # 趋势方向
    "MACD": 0.25,  # 动能确认 (核心)
    "ADX":  0.15,  # 趋势强度 — 15m BTC ADX 通常在 15-25 之间
    "Reg":  0.10,  # 市场体制
    "RSI":  0.15,  # 超买超卖 (15m 辅助信号权重调高)
    "VOL":  0.15,  # 放量确认 (重要性提高)
}
SIGNAL_THRESHOLD = 0.60  # 加权分 > 0.60 即触发信号 (ADX权重降低后同步调低)
ADX_THRESHOLD = 22       # 15m BTC 适用 (实盘ADX通常在16-25波动)
# 方向判定: MA 排列 — MA7>MA25>MA99 为牛市, MA7<MA25<MA99 为熊市


class OptimizedStrategy:
    """优化版多空双杀策略 — 向量化 + 实时信号"""

    def __init__(self, lgb_adapter=None):
        self.name = "OptimizedV6"
        self.last_signal = None
        self.peak_equity = 0
        self.position_size = 0
        self.lgb_adapter = lgb_adapter  # LightGBM 双确认适配器 (可选)
        self._last_lgb_opinion = 'no_opinion'  # 供 AIOverride 读取
        self.last_exit_bar = -1      # 上次平仓的K线索引
        self.last_entry_bar = -1  # 上次开仓的K线索引
        
    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算所有指标 (无副作用)"""
        
        if len(df) < 100:
            return df
        
        closes = df['close'].values
        highs = df['high'].values
        lows = df['low'].values
        
        # MA 线
        df['ma_7'] = df['close'].ewm(span=7, adjust=False).mean()    # EMA, 更灵敏
        df['ma_25'] = df['close'].ewm(span=25, adjust=False).mean()  # EMA
        df['ma_99'] = df['close'].ewm(span=99, adjust=False).mean()  # EMA
        
        # RSI(14)
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        df['rsi'] = 100 - (100 / (1 + gain / loss.replace(0, 1)))
        
        # MACD
        ema12 = df['close'].ewm(span=12, adjust=False).mean()
        ema26 = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = ema12 - ema26
        df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        df['macd_hist'] = df['macd'] - df['macd_signal']
        
        # ATR(14)
        high_low = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift(1)).abs()
        low_close = (df['low'] - df['close'].shift(1)).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df['atr'] = tr.rolling(14).mean()
        
        # ADX - 简化版 (DI+ / DI-)
        up_move = df['high'].diff()
        down_move = (-df['low'].diff())
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
        atr_14 = tr.rolling(14).mean()
        plus_di = 100 * pd.Series(plus_dm).rolling(14).mean() / atr_14
        minus_di = 100 * pd.Series(minus_dm).rolling(14).mean() / atr_14
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, 1)) * 100
        df['adx'] = dx.rolling(14).mean()
        
        # 波动率 (用于杠杆)
        df['volatility'] = df['close'].pct_change().rolling(20).std()
        
        # 成交量确认
        df['volume_ma'] = df['volume'].rolling(20).mean()
        df['volume_surge'] = df['volume'] > df['volume_ma'] * 1.5
        
        return df
    
    def on_bar(self, bar: Dict[str, Any], account: Any = None) -> "SignalReport":
        """
        基于一根 K 线生成结构化信号报告，供决策层消费。

        bar: {
            'close': float, 'high': float, 'low': float,
            'history': DataFrame (所有历史 K 线到当前),
            'index': int (当前在 history 中的索引),
            'position': Optional[LivePosition]
        }

        返回: SignalReport (含条件得分 + 指标快照)
        """
        from execution.signals import SignalReport

        # ── 辅助: 构建 SignalReport ──
        def _build_report(raw_signal="HOLD", exit_signal=None):
            bars_since_exit = idx - self.last_exit_bar
            is_cooldown = bars_since_exit < COOLDOWN_BARS

            # 各条件单独判定
            cond_short = {
                "MA": bool(m7 < m25),
                "MACD": bool(macdh < MACD_SHORT_THRESHOLD),
                "ADX": bool(strong_trend),
                "Reg": bool(regime != "bull"),
                "RSI": bool(RSI_SHORT_MIN_ENTRY < rsi_val < RSI_SHORT_ENTRY + 20),
                "VOL": bool(vol_surge),
            }
            cond_long = {
                "MA": bool(m7 > m25),
                "MACD": bool(macdh > MACD_LONG_THRESHOLD),
                "ADX": bool(strong_trend),
                "Reg": bool(regime != "bear"),
                "RSI": bool(RSI_LONG_ENTRY < rsi_val < RSI_LONG_MAX_ENTRY),
                "VOL": bool(vol_surge),
            }
            # 加权评分 — 替代 6/6 硬否决
            def _weighted_score(conds: dict) -> float:
                """计算加权分: 已通过条件的权重之和"""
                total = 0.0
                for name, passed in conds.items():
                    if passed:
                        total += CONDITION_WEIGHTS.get(name, 0.0)
                return total

            short_score_w = _weighted_score(cond_short)
            long_score_w = _weighted_score(cond_long)
            # 保持向后兼容: 旧版 score 仍按 6 分制
            n_conditions = 6
            short_score = sum(1 for v in cond_short.values() if v) / n_conditions
            long_score = sum(1 for v in cond_long.values() if v) / n_conditions

            # 价格趋势 (近5根K线)
            if idx >= 5:
                closes_5 = df['close'].iloc[idx-4:idx+1].values
                price_chg_5 = (closes_5[-1] - closes_5[0]) / closes_5[0] if closes_5[0] > 0 else 0
                if price_chg_5 > 0.005:
                    price_trend = "up"
                elif price_chg_5 < -0.005:
                    price_trend = "down"
                else:
                    price_trend = "sideways"
            else:
                price_trend = "sideways"

            # RSI 趋势
            if idx >= 5:
                rsi_vals = df['rsi'].iloc[idx-4:idx+1].dropna()
                if len(rsi_vals) >= 2:
                    rsi_delta = rsi_vals.iloc[-1] - rsi_vals.iloc[0]
                    if rsi_delta > 3:
                        rsi_trend = "rising"
                    elif rsi_delta < -3:
                        rsi_trend = "falling"
                    else:
                        rsi_trend = "flat"
                else:
                    rsi_trend = "flat"
            else:
                rsi_trend = "flat"

            # ADX 趋势
            if idx >= 5:
                adx_vals = df['adx'].iloc[idx-4:idx+1].dropna()
                if len(adx_vals) >= 2:
                    adx_delta = adx_vals.iloc[-1] - adx_vals.iloc[0]
                    if adx_delta > 2:
                        adx_trend = "rising"
                    elif adx_delta < -2:
                        adx_trend = "falling"
                    else:
                        adx_trend = "flat"
                else:
                    adx_trend = "flat"
            else:
                adx_trend = "flat"

            return SignalReport(
                timestamp=int(row.get('timestamp', 0)),
                price=c,
                raw_signal=raw_signal,
                exit_signal=exit_signal,
                long_score=long_score,
                short_score=short_score,
                long_score_w=long_score_w,
                short_score_w=short_score_w,
                conditions_long=cond_long,
                conditions_short=cond_short,
                rsi=rsi_val,
                adx=adx_val,
                macd_hist=macdh,
                ma7=m7,
                ma25=m25,
                ma99=m99,
                volatility=float(row.get('volatility', 0.003)) if pd.notna(row.get('volatility')) else 0.003,
                volume_surge=vol_surge,
                regime=regime,
                price_trend_5bars=price_trend,
                rsi_trend=rsi_trend,
                adx_trend=adx_trend,
                bars_since_last_trade=bars_since_exit,
                is_cooldown=is_cooldown,
                lgb_opinion=self._last_lgb_opinion,
                is_pinbar=is_pinbar,
                pinbar_direction=pinbar_direction,
                factor_bias=_factor_bias or {"bias": "neutral", "confidence": 0.0, "active_factors": []},
            )

        # ── 主逻辑 ──

        df = bar.get('history')
        idx = bar.get('index', -1)

        # 早期退出: 数据不足 → 直接返回 HOLD (不调 _build_report, 避免闭包变量未定义)
        if df is None or len(df) < 100 or idx < 99:
            from execution.signals import SignalReport
            return SignalReport(
                timestamp=int(bar.get('timestamp', 0)),
                price=float(bar.get('close', 0)),
                raw_signal="HOLD",
            )

        # 因子偏向 — 提前初始化 (ATR/MAX_HOLD_BARS 退出也可能引用)
        _factor_bias = None
        try:
            from services.factor_analysis import get_active_factor_bias
            _factor_bias = get_active_factor_bias()
        except Exception:
            _factor_bias = {"bias": "neutral", "confidence": 0.0, "active_factors": []}

        # 计算指标 (如果还没算)
        if 'rsi' not in df.columns or 'adx' not in df.columns:
            df = self.compute_indicators(df)

        row = df.iloc[idx]

        c = float(row['close'])
        h = float(row['high'])
        l = float(row['low'])
        rsi_val = float(row.get('rsi', 50))
        atr_val = float(row.get('atr', 0))
        adx_val = float(row.get('adx', 0))
        macdh = float(row.get('macd_hist', 0))
        m7 = float(row.get('ma_7', c))
        m25 = float(row.get('ma_25', c))
        m99 = float(row.get('ma_99', c))
        vol_surge = bool(row.get('volume_surge', False))

        if np.isnan(rsi_val) or np.isnan(adx_val):
            from execution.signals import SignalReport
            return SignalReport(price=c, raw_signal="HOLD")

        # === Pin Bar 检测 ===
        # 定义: 影线占比 > 50%, 实体占比 < 50% (比 40%/60% 更严格, 减少小碎针误杀)
        # 只有真正冲高回落/探底回升的 K 线才触发
        o = float(row.get('open', c))
        candle_range = h - l
        is_pinbar = False
        pinbar_direction = "none"
        if candle_range > 0:
            upper_wick = h - max(o, c)
            lower_wick = min(o, c) - l
            body = abs(c - o)
            upper_wick_pct = upper_wick / candle_range
            lower_wick_pct = lower_wick / candle_range
            body_pct = body / candle_range
            close_position = (c - l) / candle_range  # 0=底部 1=顶部

            # Bearish pin bar: 长上影(>50%), 实体不主导(<50%), 收盘偏下(<0.5)
            if upper_wick_pct > 0.50 and body_pct < 0.50 and close_position < 0.50:
                is_pinbar = True
                pinbar_direction = "bearish"
            # Bullish pin bar: 长下影(>50%), 实体不主导(<50%), 收盘偏上(>0.5)
            elif lower_wick_pct > 0.50 and body_pct < 0.50 and close_position > 0.50:
                is_pinbar = True
                pinbar_direction = "bullish"

        # 市场状态
        regime = "bull" if (m7 > m25 and m25 > m99) else ("bear" if (m7 < m25 and m25 < m99) else "neutral")

        # 趋势方向
        trend_up = m7 > m25 and macdh > -50
        trend_down = m7 < m25 and macdh < 50
        strong_trend = adx_val > ADX_THRESHOLD

        # === 加权评分 (提前计算, 持仓中也要用) ===
        cond_short = {
            "MA": bool(m7 < m25),
            "MACD": bool(macdh < MACD_SHORT_THRESHOLD),
            "ADX": bool(strong_trend),
            "Reg": bool(regime != "bull"),
            "RSI": bool(RSI_SHORT_MIN_ENTRY < rsi_val < RSI_SHORT_ENTRY + 20),
            "VOL": bool(vol_surge),
        }
        cond_long = {
            "MA": bool(m7 > m25),
            "MACD": bool(macdh > MACD_LONG_THRESHOLD),
            "ADX": bool(strong_trend),
            "Reg": bool(regime != "bear"),
            "RSI": bool(RSI_LONG_ENTRY < rsi_val < RSI_LONG_MAX_ENTRY),
            "VOL": bool(vol_surge),
        }
        short_score_w = sum(CONDITION_WEIGHTS.get(n, 0.0) for n, p in cond_short.items() if p)
        long_score_w = sum(CONDITION_WEIGHTS.get(n, 0.0) for n, p in cond_long.items() if p)

        # 当前持仓
        pos = bar.get('position')
        has_position = pos is not None and getattr(pos, 'size', 0) > 0
        pos_side = getattr(pos, 'side', None) if has_position else None
        pos_entry = getattr(pos, 'avg_price', 0) if has_position else 0

        # 持仓bars计数
        bars_held = 1
        if has_position:
            if hasattr(pos, 'bars_held'):
                bars_held = max(1, int(pos.bars_held))
            elif pos_entry > 0 and hasattr(pos, 'entry_bar') and pos.entry_bar >= 0:
                bars_held = max(1, idx - pos.entry_bar)

        # === 风控检查: 持仓时 ===
        if has_position and pos_entry > 0:
            # ATR 止损（需要有效 ATR）
            if atr_val > 0:
                if pos_side == 'long' and c <= pos_entry - atr_val * ATR_STOP_LONG:
                    self.last_exit_bar = idx
                    return _build_report("SELL", "SELL")
                elif pos_side == 'short' and c >= pos_entry + atr_val * ATR_STOP_SHORT:
                    self.last_exit_bar = idx
                    return _build_report("COVER", "COVER")

            # 最大持仓时间（不依赖 ATR）
            if bars_held >= MAX_HOLD_BARS:
                if pos_side == 'long':
                    pnl_pct = (c - pos_entry) / pos_entry
                else:
                    pnl_pct = (pos_entry - c) / pos_entry
                max_bars = MAX_HOLD_BARS if pnl_pct < 0 else MAX_HOLD_BARS * 2
                if bars_held >= max_bars:
                    logger.info(f"⏰ 最大持仓时间平仓 | bars={bars_held} | PnL={pnl_pct:+.2%}")
                    self.last_exit_bar = idx
                    exit_sig = "SELL" if pos_side == 'long' else "COVER"
                    return _build_report(exit_sig, exit_sig)

        # === 平仓信号 ===
        if has_position:
            if pos_side == 'long':
                if (rsi_val > RSI_LONG_EXIT and bars_held >= MIN_HOLD_BARS) or (trend_down and strong_trend):
                    self.last_exit_bar = idx
                    return _build_report("SELL", "SELL")
            elif pos_side == 'short':
                if (rsi_val < RSI_SHORT_EXIT and bars_held >= MIN_HOLD_BARS) or (trend_up and strong_trend):
                    self.last_exit_bar = idx
                    return _build_report("COVER", "COVER")

        # === 仓位缩放: 持仓中 (加仓/减仓) ===
        if has_position and pos_entry > 0:
            pos_leverage = getattr(pos, 'leverage', 10)
            pos_size = getattr(pos, 'size', 0)
            pos_addition_count = getattr(pos, 'addition_count', 0)
            max_additions = getattr(account, 'MAX_ADDITIONS', 2) if account else 2

            # 计算未实现盈亏 (杠杆回报)
            if pos_side == 'long':
                unrealized_pnl_pct = (c - pos_entry) / pos_entry * pos_leverage * 100
            else:
                unrealized_pnl_pct = (pos_entry - c) / pos_entry * pos_leverage * 100

            # REDUCE: 盈利 > 30% 杠杆回报 → 减半锁定利润
            if unrealized_pnl_pct > 30:
                logger.info(f"💰 减仓止盈: {pos_side} | PnL={unrealized_pnl_pct:+.1f}% | 减50%锁定利润")
                return _build_report("REDUCE")

            # PROFIT_ADD: 浮盈 > 5% 且评分仍强 → 顺势加码 (盈利奔跑)
            # 条件: 盈利确认方向 + 信号强 + 持仓前半段 + 未达到加仓上限
            if unrealized_pnl_pct > 5 and pos_addition_count < max_additions:
                if bars_held < MAX_HOLD_BARS // 2:
                    # 盈利加仓门槛略放宽 (SIGNAL_THRESHOLD - 0.05): 市场已用盈利确认方向
                    profit_add_threshold = SIGNAL_THRESHOLD - 0.05
                    if pos_side == 'long' and long_score_w >= profit_add_threshold:
                        logger.info(f"🚀 盈利加仓: LONG | PnL={unrealized_pnl_pct:+.1f}% | 评分{long_score_w:.3f} | 顺势加码")
                        return _build_report("ADD_LONG")
                    elif pos_side == 'short' and short_score_w >= profit_add_threshold:
                        logger.info(f"🚀 盈利加仓: SHORT | PnL={unrealized_pnl_pct:+.1f}% | 评分{short_score_w:.3f} | 顺势加码")
                        return _build_report("ADD_SHORT")

            # LOSS_ADD: 浮亏 > 5% 但评分仍超阈值 → 顺势补仓摊低成本
            if unrealized_pnl_pct < -5 and pos_addition_count < max_additions:
                if bars_held < MAX_HOLD_BARS // 2:  # 仅在持仓前半段加仓
                    if pos_side == 'long' and long_score_w >= SIGNAL_THRESHOLD:
                        logger.info(f"📈 亏损补仓: LONG | 浮亏{unrealized_pnl_pct:+.1f}% | 评分{long_score_w:.3f}")
                        return _build_report("ADD_LONG")
                    elif pos_side == 'short' and short_score_w >= SIGNAL_THRESHOLD:
                        logger.info(f"📉 亏损补仓: SHORT | 浮亏{unrealized_pnl_pct:+.1f}% | 评分{short_score_w:.3f}")
                        return _build_report("ADD_SHORT")

        # === 开仓信号 (含 LightGBM 双确认) ===
        if not has_position:
            # === Pin Bar 方向过滤 ===
            # Pin bar 不再全部挡死，而是只挡方向冲突的一方。
            # Bearish pin bar(冲高回落) → 对 LONG 是陷阱，对 SHORT 反而是确认
            # Bullish pin bar(探底回升) → 对 SHORT 是陷阱，对 LONG 反而是确认
            # 冲突方的信号被强制设为 HOLD 且降低 raw_signal 权重。
            pinbar_block_long = is_pinbar and pinbar_direction == "bearish"
            pinbar_block_short = is_pinbar and pinbar_direction == "bullish"

            if is_pinbar:
                direction_name = "Bearish" if pinbar_direction == "bearish" else "Bullish"
                wick_key = "upper wick" if pinbar_direction == "bearish" else "lower wick"
                wick_val = (h - max(o, c)) if pinbar_direction == "bearish" else (min(o, c) - l)
                wick_pct = wick_val / max(candle_range, 1) * 100
                blocked_side = "LONG" if pinbar_direction == "bearish" else "SHORT"
                allowed_side = "SHORT" if pinbar_direction == "bearish" else "LONG"
                logger.warning(
                    f"🕯 {direction_name} pin bar @ ${c:,.0f} — "
                    f"{wick_key}={wick_pct:.0f}% of range, "
                    f"blocking {blocked_side} (trap), allowing {allowed_side}"
                )

            # === 加权评分判定 (替代 6/6 硬否决) ===
            local_threshold = SIGNAL_THRESHOLD

            # 因子 bias 动态调阈 — short_bias 时降低 SHORT 门槛, long_bias 时降低 LONG 门槛
            bias = _factor_bias or {}
            if bias and bias.get("confidence", 0) > 0.8:
                    if bias["bias"] == "short_bias":
                        local_threshold -= 0.08  # SHORT 门槛从 0.65 → 0.57
                        logger.debug(f"🎯 short_bias(conf={bias['confidence']}): SHORT threshold→{local_threshold:.2f}")
                    elif bias["bias"] == "long_bias":
                        local_threshold -= 0.08
                        logger.debug(f"🎯 long_bias(conf={bias['confidence']}): LONG threshold→{local_threshold:.2f}")

            # 用加权分判定信号
            short_signal = short_score_w >= local_threshold
            long_signal = long_score_w >= local_threshold

            # Pin bar 方向阻断: 冲突方信号降级
            if pinbar_block_long:
                long_signal = False
            if pinbar_block_short:
                short_signal = False

            # Pin bar 同向加码: 对齐方降低阈值 (顺势做入确认方向)
            if is_pinbar:
                if pinbar_direction == "bearish":
                    short_signal = short_signal or short_score_w >= local_threshold - 0.05
                elif pinbar_direction == "bullish":
                    long_signal = long_signal or long_score_w >= local_threshold - 0.05

            # LightGBM 双确认
            if self.lgb_adapter and self.lgb_adapter.is_loaded():
                if short_signal:
                    confirm = self.lgb_adapter.confirm(row, "SHORT")
                    self._last_lgb_opinion = confirm
                    if confirm == "agree":
                        self.last_entry_bar = idx
                        return _build_report("SHORT")
                    elif confirm == "disagree":
                        return _build_report("HOLD")
                if long_signal:
                    confirm = self.lgb_adapter.confirm(row, "LONG")
                    self._last_lgb_opinion = confirm
                    if confirm == "agree":
                        self.last_entry_bar = idx
                        return _build_report("LONG")
                    elif confirm == "disagree":
                        return _build_report("HOLD")
            else:
                self._last_lgb_opinion = 'no_opinion'

            # 无 LGB 适配器时 / LGB no_opinion 时
            if short_signal:
                self.last_entry_bar = idx
                return _build_report("SHORT")
            if long_signal:
                self.last_entry_bar = idx
                return _build_report("LONG")

            # HOLD — 诊断日志内置在 SignalReport.summary() 中
            report = _build_report("HOLD")
            logger.info(f"🔍 {report.summary()}")
            return report

        return _build_report("HOLD")
    
    def get_position_size(self, cash: float, price: float, leverage: int,
                          atr: float = 0, side: str = 'long') -> float:
        """
        资金管理：凯利公式 + 固定比例（期货语义：cash × leverage = 购买力）。
        MAX_POSITION_PCT = 保证金占权益比例，杠杆放大后得实际仓位。
        """
        
        if side == 'long':
            mult = BEST_LONG_MULT
        else:
            mult = BEST_SHORT_MULT
        
        # 期货购买力 = 现金 × 杠杆
        buying_power = cash * leverage
        
        # 基础仓位（保证金占比 × 购买力 × Kelly 乘数）
        base_size = (buying_power * MAX_POSITION_PCT * mult) / price
        
        # 硬上限: 保证金不能超过现金 × MAX_POSITION_PCT (executor风控的前提)
        # margin = size * price / leverage <= cash * MAX_POSITION_PCT
        max_size = (cash * MAX_POSITION_PCT * leverage) / price
        base_size = min(base_size, max_size)
        
        # ATR 调整: 高波动降仓位
        if atr > 0 and price > 0:
            atr_pct = atr / price
            if atr_pct > 0.008:
                base_size *= 0.7
            elif atr_pct > 0.004:
                base_size *= 0.85
        
        return max(base_size, 0.001)
    
    def get_dynamic_leverage(self, volatility: float) -> int:
        """动态杠杆: 低波动加杠杆, 高波动降杠杆"""
        if np.isnan(volatility):
            return 10
        if volatility > 0.008:
            return 5
        elif volatility > 0.004:
            return 10
        else:
            return 15
    
    def check_max_drawdown(self, current_equity: float) -> bool:
        """检查最大回撤是否超限"""
        if self.peak_equity == 0:
            self.peak_equity = current_equity
            return False
        
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity
        
        dd = (self.peak_equity - current_equity) / max(self.peak_equity, 1)
        return dd > MAX_DRAWDOWN_PCT