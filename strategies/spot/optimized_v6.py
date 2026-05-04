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
MAX_POSITION_PCT = 0.15  # 最大仓位比例 30%
ATR_STOP_LONG = 1.2      # 做多 ATR 止损倍数
ATR_STOP_SHORT = 1.5     # 做空 ATR 止损倍数 (做空更激进)
MAX_DRAWDOWN_PCT = 0.20  # 最大回撤 20% 熔断
MAX_HOLD_BARS = 32       # 最大持仓K线数 (8小时)
MIN_HOLD_BARS = 4        # 最小持仓K线数 (前4根K线不能RSI平仓, ATR止损除外)
COOLDOWN_BARS = 8        # 冷却K线数 (90分钟, 避免频繁交易)

# === 信号阈值 ===
RSI_LONG_ENTRY = 35      # 做多: RSI < 35 (深度回调介入, 更安全)
RSI_LONG_MAX_ENTRY = 65  # 做多: RSI > 65 拒绝开仓 (拒绝追高)
RSI_LONG_EXIT = 75       # 做多平仓: RSI > 75 (让盈利奔跑)
RSI_SHORT_ENTRY = 55     # 做空: RSI > 55 (等待更强反弹再介入)
RSI_SHORT_MIN_ENTRY = 35 # 做空: RSI < 35 拒绝开仓 (拒绝追低)
RSI_SHORT_EXIT = 40      # 做空平仓: RSI < 40 (持有到超卖区域)
MACD_LONG_THRESHOLD = 25 # MACD_hist > 20 才确认做多 (严格过滤15m噪音)
MACD_SHORT_THRESHOLD = -25 # MACD_hist < -20 才确认做空
ADX_THRESHOLD = 35       # ADX 须 > 35 过滤震荡 (15m需要更强趋势)
# 方向判定: 不再用 price/MA99 偏离 (15mK线偏差2%太苛刻且与RSI互斥)
# 改用 MA 排列 — MA7>MA25>MA99 为牛市, MA7<MA25<MA99 为熊市


class OptimizedStrategy:
    """优化版多空双杀策略 — 向量化 + 实时信号"""

    def __init__(self, lgb_adapter=None):
        self.name = "OptimizedV6"
        self.last_signal = None
        self.peak_equity = 0
        self.position_size = 0
        self.lgb_adapter = lgb_adapter  # LightGBM 双确认适配器 (可选)
        self._last_lgb_opinion = 'no_opinion'  # 供 AIOverride 读取
        self.last_exit_bar = -COOLDOWN_BARS  # 上次平仓的K线索引 (初始化为足够早, 允许首笔交易)
        self.last_entry_bar = -1  # 上次开仓的K线索引
        
    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算所有指标 (无副作用)"""
        
        if len(df) < 100:
            return df
        
        closes = df['close'].values
        highs = df['high'].values
        lows = df['low'].values
        
        # MA 线
        df['ma_7'] = df['close'].rolling(7).mean()
        df['ma_25'] = df['close'].rolling(25).mean()
        df['ma_99'] = df['close'].rolling(99).mean()
        
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
    
    def on_bar(self, bar: Dict[str, Any], account: Any = None) -> Optional[str]:
        """
        基于一根 K 线生成信号。
        
        bar: {
            'close': float, 'high': float, 'low': float,
            'history': DataFrame (所有历史 K 线到当前),
            'index': int (当前在 history 中的索引),
            'position': Optional[LivePosition]
        }
        
        返回: "LONG" | "SHORT" | "COVER" | "SELL" | "HOLD" | None
        """
        
        df = bar.get('history')
        if df is None or len(df) < 100:
            return "HOLD"
        
        idx = bar.get('index', -1)
        if idx < 99:
            return "HOLD"
        
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
        ma99 = float(row.get('ma_99', c))
        m7 = float(row.get('ma_7', c))
        m25 = float(row.get('ma_25', c))
        vol_surge = bool(row.get('volume_surge', False))
        
        if np.isnan(rsi_val) or np.isnan(adx_val):
            return "HOLD"
        
        # 市场状态 — 用 MA 排列判断方向，不用 MA99 偏离
        m99 = float(row.get('ma_99', c))
        regime = "bull" if (m7 > m25 and m25 > m99) else ("bear" if (m7 < m25 and m25 < m99) else "neutral")
        
        # 趋势方向
        trend_up = m7 > m25 and macdh > -50     # MA金叉 + MACD不严重看跌
        trend_down = m7 < m25 and macdh < 50     # MA死叉 + MACD不严重看涨
        strong_trend = adx_val > ADX_THRESHOLD
        
        # MACD 趋势强度 (独立于MA)
        macd_bearish = macdh < -20
        macd_bullish = macdh > 20
        
        # 当前持仓
        pos = bar.get('position')
        has_position = pos is not None and getattr(pos, 'size', 0) > 0
        pos_side = getattr(pos, 'side', None) if has_position else None
        pos_entry = getattr(pos, 'avg_price', 0) if has_position else 0
        pos_leverage = getattr(pos, 'leverage', 1) if has_position else 1
        
        # === 风控检查: 持仓时 ===
        if has_position and pos_entry > 0 and atr_val > 0:
            # bars_held: 优先从 position.bars_held; 次选 entry_bar 计算; 兜底 1
            bars_held = 1
            if hasattr(pos, 'bars_held'):
                bars_held = max(1, int(pos.bars_held))
            elif pos_entry > 0 and hasattr(pos, 'entry_bar') and pos.entry_bar >= 0:
                bars_held = max(1, idx - pos.entry_bar)
            
            # ATR 止损
            if pos_side == 'long' and c <= pos_entry - atr_val * ATR_STOP_LONG:
                self.last_exit_bar = idx
                return "SELL"
            elif pos_side == 'short' and c >= pos_entry + atr_val * ATR_STOP_SHORT:
                self.last_exit_bar = idx
                return "COVER"
            
            # 最大持仓时间: 浮亏加速平仓, 浮盈延长持有
            if bars_held >= MAX_HOLD_BARS:
                # 计算当前盈亏
                if pos_side == 'long':
                    pnl_pct = (c - pos_entry) / pos_entry
                else:
                    pnl_pct = (pos_entry - c) / pos_entry
                # 浮亏 → 立即平仓; 浮盈 → 放宽到 2x 时间
                max_bars = MAX_HOLD_BARS if pnl_pct < 0 else MAX_HOLD_BARS * 2
                if bars_held >= max_bars:
                    logger.info(f"⏰ 最大持仓时间平仓 | bars={bars_held} | PnL={pnl_pct:+.2%}")
                    self.last_exit_bar = idx
                    return "SELL" if pos_side == 'long' else "COVER"
        
        # === 平仓信号 ===
        if has_position:
            if pos_side == 'long':
                if (rsi_val > RSI_LONG_EXIT and bars_held >= MIN_HOLD_BARS) or (trend_down and strong_trend):
                    self.last_exit_bar = idx
                    return "SELL"
            elif pos_side == 'short':
                if (rsi_val < RSI_SHORT_EXIT and bars_held >= MIN_HOLD_BARS) or (trend_up and strong_trend):
                    self.last_exit_bar = idx
                    return "COVER"
        
        # === 开仓信号 (含 LightGBM 双确认) ===
        if not has_position:
            # 冷却检查: 上次平仓后必须等待 COOLDOWN_BARS 根K线
            bars_since_exit = idx - self.last_exit_bar
            if bars_since_exit < COOLDOWN_BARS:
                return "HOLD"

            # 做空: MA死叉 + MACD看跌 + 强势趋势 + regime非牛 + RSI不过低 + 放量
            short_signal = (m7 < m25 and macdh < MACD_SHORT_THRESHOLD and strong_trend and
                           regime != "bull" and RSI_SHORT_MIN_ENTRY < rsi_val < RSI_SHORT_ENTRY + 20 and
                           vol_surge)
            # 做多: MA金叉 + MACD看涨 + 强势趋势 + regime非熊 + RSI不过高 + 放量
            long_signal = (m7 > m25 and macdh > MACD_LONG_THRESHOLD and strong_trend and
                           regime != "bear" and RSI_LONG_ENTRY < rsi_val < RSI_LONG_MAX_ENTRY and
                           vol_surge)

            # LightGBM 双确认
            if self.lgb_adapter and self.lgb_adapter.is_loaded():
                if short_signal:
                    confirm = self.lgb_adapter.confirm(row, "SHORT")
                    self._last_lgb_opinion = confirm
                    if confirm == "agree":
                        self.last_entry_bar = idx
                        return "SHORT"
                    elif confirm == "disagree":
                        return "HOLD"  # LGB明确反对，不开仓
                    # no_opinion → 仅依赖MATrend
                if long_signal:
                    confirm = self.lgb_adapter.confirm(row, "LONG")
                    self._last_lgb_opinion = confirm
                    if confirm == "agree":
                        self.last_entry_bar = idx
                        return "LONG"
                    elif confirm == "disagree":
                        return "HOLD"  # LGB明确反对，不开仓
                    # no_opinion → 仅依赖MATrend
            else:
                self._last_lgb_opinion = 'no_opinion'

            # MATrend 信号 (LGB agree/no_opinion 时生效)

            # 无 LGB 适配器时，原逻辑
            if short_signal:
                self.last_entry_bar = idx
                return "SHORT"
            if long_signal:
                self.last_entry_bar = idx
                return "LONG"
        
        return "HOLD"
    
    def get_position_size(self, cash: float, price: float, leverage: int,
                          atr: float = 0, side: str = 'long') -> float:
        """
        资金管理：凯利公式 + 固定比例
        """
        
        if side == 'long':
            mult = BEST_LONG_MULT
        else:
            mult = BEST_SHORT_MULT
        
        # 基础仓位
        base_size = (cash * MAX_POSITION_PCT * mult) / price
        
        # ATR 调整: 高波动降仓位
        if atr > 0 and price > 0:
            atr_pct = atr / price
            if atr_pct > 0.008:
                base_size *= 0.7
            elif atr_pct > 0.004:
                base_size *= 0.85
        
        # 杠杆下调整 (合约: cash * 杠杆 = 购买力)
        adjusted = base_size / leverage
        
        return max(adjusted, 0.001)
    
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