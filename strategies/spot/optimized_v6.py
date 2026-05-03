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
MAX_HOLD_BARS = 48       # 最大持仓K线数 (12小时)

# === 信号阈值 ===
RSI_LONG_ENTRY = 48      # 做多: RSI < 40 (回调到位)
RSI_LONG_EXIT = 60       # 做多: RSI > 75 (过热平仓) — 放宽
RSI_SHORT_ENTRY = 50     # 做空: RSI > 35 (不超卖时才空) — 放宽
RSI_SHORT_EXIT = 45      # 做空: RSI < 25 (超卖平空)
MACD_LONG_THRESHOLD = 5  # MACD_hist > 5 即确认 (原15太严)
MACD_SHORT_THRESHOLD = -5  # MACD_hist < -5 即确认 (原-15太严)
ADX_THRESHOLD = 23       # ADX 须 > 18 过滤震荡 (原20太严)
DEVIATION_BULL = 0.02    # 高于 MA99 2% → 牛市
DEVIATION_BEAR = -0.02   # 低于 MA99 2% → 熊市


class OptimizedStrategy:
    """优化版多空双杀策略 — 向量化 + 实时信号"""

    def __init__(self, lgb_adapter=None):
        self.name = "OptimizedV6"
        self.last_signal = None
        self.peak_equity = 0
        self.position_size = 0
        self.lgb_adapter = lgb_adapter  # LightGBM 双确认适配器 (可选)
        
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
        
        # 市场状态
        dev = c / max(ma99, 1) - 1
        regime = "bull" if dev > DEVIATION_BULL else ("bear" if dev < DEVIATION_BEAR else "neutral")
        
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
            bars_held = getattr(pos, 'bars_held', 0) if hasattr(pos, 'bars_held') else 0
            
            # ATR 止损
            if pos_side == 'long' and c <= pos_entry - atr_val * ATR_STOP_LONG:
                return "SELL"
            elif pos_side == 'short' and c >= pos_entry + atr_val * ATR_STOP_SHORT:
                return "COVER"
            
            # 最大持仓时间
            if bars_held >= MAX_HOLD_BARS:
                return "SELL" if pos_side == 'long' else "COVER"
        
        # === 平仓信号 ===
        if has_position:
            if pos_side == 'long':
                if rsi_val > RSI_LONG_EXIT or (trend_down and strong_trend):
                    return "SELL"
            elif pos_side == 'short':
                if rsi_val < RSI_SHORT_EXIT or (trend_up and strong_trend):
                    return "COVER"
        
        # === 开仓信号 (含 LightGBM 双确认) ===
        if not has_position:
            # 做空: MA死叉 + MACD看跌 + 强势趋势
            short_signal = (m7 < m25 and macdh < MACD_SHORT_THRESHOLD and strong_trend and
                           regime != "bull" and rsi_val > RSI_SHORT_ENTRY)
            # 做多: MA金叉 + MACD看涨 + 强势趋势
            long_signal = (m7 > m25 and macdh > MACD_LONG_THRESHOLD and strong_trend and
                           regime != "bear" and rsi_val < RSI_LONG_EXIT - 5)

            # LightGBM 双确认
            if self.lgb_adapter and self.lgb_adapter.is_loaded():
                if short_signal:
                    confirm = self.lgb_adapter.confirm(row, "SHORT")
                    if confirm == "agree":
                        return "SHORT"
                    elif confirm == "disagree":
                        return "HOLD"  # LGB明确反对，不开仓
                    # no_opinion → 仅依赖MATrend
                if long_signal:
                    confirm = self.lgb_adapter.confirm(row, "LONG")
                    if confirm == "agree":
                        return "LONG"
                    elif confirm == "disagree":
                        return "HOLD"  # LGB明确反对，不开仓
                    # no_opinion → 仅依赖MATrend

            # MATrend 信号 (LGB agree/no_opinion 时生效)

            # 无 LGB 适配器时，原逻辑
            if short_signal:
                return "SHORT"
            if long_signal:
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