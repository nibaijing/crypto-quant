#!/usr/bin/env python3
"""
CryptoQuant 高性能实盘引擎

架构:
  WebSocket 线程 ─→ SharedMarketState (价格+K线)
                         │
  主线程 ─→ 每秒更新executor价格 ─→ 每根K线闭合时跑策略
  
刷新频率:
  - 实时价格: 逐笔成交推送 (ms级)
  - K线闭合: 15分钟产生一根完整K线后触发策略
  - Dashboard状态: 每秒更新 executor 价格
"""

import sys
import os
import time
import signal
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import numpy as np

from core.config import init_config
from execution.executor_v2 import FuturesExecutor, LiveAccount, LivePosition, LiveOrder
from execution.signals import SignalReport
from strategies.spot.optimized_v6 import OptimizedV6 as OptimizedStrategy
import strategies.spot.optimized_v6 as strat_mod
from data.ws_price_stream import SharedMarketState, BinanceWebSocket, create_price_stream
from data.alpha_factors import AlphaFactors
from ml.lgb_predictor import LGBAdapter
from notifier import notify_trade

# ===== 日志 =====
log_file = Path(__file__).parent / "data" / "live_trading.log"
log_file.parent.mkdir(parents=True, exist_ok=True)

fh = logging.FileHandler(log_file, encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

root_logger = logging.getLogger()
root_logger.handlers.clear()
root_logger.addHandler(fh)
root_logger.setLevel(logging.INFO)

logger = logging.getLogger("CryptoQuant.Live")

# ===== 全局 =====
market_state: SharedMarketState = None
ws_client: BinanceWebSocket = None
executor: FuturesExecutor = None
strategy: OptimizedStrategy = None
alpha_factors: AlphaFactors = None
running = True
start_time: datetime = None
kline_count = 0
last_kline_close_time = 0
_last_kline_key = None  # K线去重
last_hist_retry = 0  # 历史K线重试计时器
PRICE_SNAPSHOT = Path(__file__).parent / "data" / "ws_price_snapshot.json"


def signal_handler(sig, frame):
    global running
    logger.info("🛑 停止信号, 正在关闭...")
    running = False


def get_uptime() -> str:
    if not start_time:
        return "00:00:00"
    delta = datetime.now() - start_time
    h, r = divmod(delta.seconds, 3600)
    m, s = divmod(r, 60)
    return f"{delta.days}d {h:02d}:{m:02d}:{s:02d}"


def fetch_historical_klines(strategy_obj=None) -> pd.DataFrame:
    """拉取历史200根K线用于策略初始化 (3次重试)"""
    klines = None
    for attempt in range(3):
        klines = executor.get_klines("BTC-USDT", "15m", 200)
        if klines:
            break
        logger.warning(f"K线获取失败 (第{attempt+1}/3次)，5秒后重试...")
        time.sleep(5)
    if not klines:
        logger.error("❌ 3次重试后仍无法获取历史K线，将依赖WS在线构建")
        return pd.DataFrame()
    df = pd.DataFrame(klines)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")

    if len(df) >= 26:
        # 使用策略统一的 compute_indicators (单一数据源)
        if strategy_obj and hasattr(strategy_obj, 'compute_indicators'):
            df = strategy_obj.compute_indicators(df)
        else:
            from strategies.spot.optimized_v6 import OptimizedV6 as OptimizedStrategy
            df = OptimizedStrategy().compute_indicators(df)

        # Alpha 因子集
        df = alpha_factors.compute(df)

    return df


def run_strategy_on_closed_bar(df: pd.DataFrame):
    """K线闭合时运行策略"""
    global kline_count, last_kline_close_time

    if len(df) < 30:
        return

    # 最新闭合K线的索引
    latest_idx = len(df) - 1
    row = df.iloc[latest_idx]
    close_price = float(row["close"])

    symbol = "BTC-USDT"

    # 更新 executor 杠杆
    if "volatility" in df.columns:
        vol = float(row["volatility"])
        if not np.isnan(vol):
            lev = strategy.get_dynamic_leverage(vol)
            executor.set_leverage(lev)

    # 构建 bar 数据
    bar = {
        "timestamp": int(row["timestamp"]),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": close_price,
        "volume": float(row["volume"]),
        "history": df.iloc[: latest_idx + 1].copy(),
        "index": latest_idx,
        "position": None,
    }

    for col in ["ma_7", "ma_25", "ma_99", "rsi", "macd", "macd_signal", "macd_hist", "volatility", "volume_ma"]:
        if col in df.columns:
            val = row.get(col)
            if pd.notna(val):
                bar[col] = float(val)

    if executor.position:
        pos = executor.position
        bar["position"] = type("obj", (object,), {
            "size": pos.size, "side": pos.side,
            "avg_price": pos.entry_price, "leverage": pos.leverage,
            "bars_held": pos.bars_held,
        })()

    # 执行策略 — 返回 SignalReport
    report: SignalReport = None
    try:
        report = strategy.on_bar(bar, executor)
        executor.update_bars_held()
    except Exception as e:
        logger.error(f"策略错误: {e}", exc_info=True)

    # === 最大回撤熔断检查 ===
    if executor.position and strategy.check_max_drawdown(executor.equity):
        logger.warning(f"🛑 最大回撤超限 ({strat_mod.MAX_DRAWDOWN_PCT:.0%}), 强制平仓")
        if executor.position.side == "long":
            executor.sell(symbol, price=close_price)
            notify_trade("SELL", close_price, f"熔断平多仓(回撤)")
        elif executor.position.side == "short":
            executor.short_cover(symbol, price=close_price)
            notify_trade("COVER", close_price, f"熔断平空仓(回撤)")
        # 重置峰值以允许后续重新入场
        strategy.peak_equity = 0
        return

    # === 决策: 限价单模式 — K线闭合只更新指标, 入场由on_tick挂限价单 ===
    signal = "HOLD"
    if report is not None:
        raw = report.raw_signal or "HOLD"
        if raw in ("LONG", "SHORT"):
            # 开仓信号: 记录但不直接交易, 由on_tick挂限价单
            logger.info(f"📡 信号: {raw} (限价单模式 — 由on_tick挂单)")
        elif raw in ("SELL", "COVER"):
            # 平仓信号: K线闭合时直接市价平仓(风控保护)
            signal = raw
        elif raw in ("ADD_LONG", "ADD_SHORT", "REDUCE"):
            signal = raw
    if signal and signal != "HOLD":
        logger.info(f"📡 信号: {signal} | close=${close_price:,.0f}")

        pos_side = executor.position.side if executor.position else None
        pos_size = executor.position.size if executor.position else 0

        # 计算计划止损价（ATR 动态止损）
        atr_val = float(row.get('atr', 0)) if pd.notna(row.get('atr', 0)) else 0

        if signal == "LONG" and (not executor.position or executor.position.size == 0):
            gated_size = strategy.get_position_size(
                cash=executor.cash, price=close_price,
                leverage=executor._sim_leverage, atr=atr_val, side='long'
            )
            result = executor.buy(symbol, size=gated_size, price=close_price)
            if result:
                notify_trade("LONG", close_price, f"开多仓 {executor._sim_leverage}x")

        elif signal == "SELL" and executor.position and executor.position.side == "long":
            lev = executor.position.leverage
            pnl_pct = (close_price - executor.position.entry_price) / executor.position.entry_price * 100 * lev
            result = executor.sell(symbol, price=close_price)
            if result:
                # 记录平仓盈亏到策略(用于冷却+连续亏损保护)
                raw_pnl = (close_price - executor.position.entry_price) / executor.position.entry_price
                if hasattr(strategy, 'record_trade_result'):
                    strategy.record_trade_result(raw_pnl)
                notify_trade("SELL", close_price, f"平多仓 | PnL={pnl_pct:+.2f}% ({lev}x)")
                if hasattr(strategy, 'last_exit_bar'):
                    strategy.last_exit_bar = latest_idx

        elif signal == "SHORT" and (not executor.position or executor.position.size == 0):
            gated_size = strategy.get_position_size(
                cash=executor.cash, price=close_price,
                leverage=executor._sim_leverage, atr=atr_val, side='short'
            )
            result = executor.short_sell(symbol, size=gated_size, price=close_price)
            if result:
                notify_trade("SHORT", close_price, f"开空仓 {executor._sim_leverage}x")

        elif signal == "COVER" and executor.position and executor.position.side == "short":
            lev = executor.position.leverage
            pnl_pct = (executor.position.entry_price - close_price) / executor.position.entry_price * 100 * lev
            result = executor.short_cover(symbol, price=close_price)
            if result:
                # 记录平仓盈亏到策略(用于冷却+连续亏损保护)
                raw_pnl = (executor.position.entry_price - close_price) / executor.position.entry_price
                if hasattr(strategy, 'record_trade_result'):
                    strategy.record_trade_result(raw_pnl)
                notify_trade("COVER", close_price, f"平空仓 | PnL={pnl_pct:+.2f}% ({lev}x)")
                if hasattr(strategy, 'last_exit_bar'):
                    strategy.last_exit_bar = latest_idx

        elif signal == "ADD_LONG" and executor.position and executor.position.side == "long":
            add_size = executor.position.size * 0.5  # 加仓50%
            result = executor.add_to_long(symbol, add_size, close_price)
            if result:
                notify_trade("ADD_LONG", close_price, f"加多仓 +50% | 均价→{executor.position.entry_price:,.0f}")

        elif signal == "ADD_SHORT" and executor.position and executor.position.side == "short":
            add_size = executor.position.size * 0.5
            result = executor.add_to_short(symbol, add_size, close_price)
            if result:
                notify_trade("ADD_SHORT", close_price, f"加空仓 +50% | 均价→{executor.position.entry_price:,.0f}")

        elif signal == "REDUCE" and executor.position:
            reduce_size = executor.position.size * 0.5  # 减仓50%
            if executor.position.side == "long":
                result = executor.sell(symbol, price=close_price, size=reduce_size)
                if result:
                    pnl = close_price - executor.position.entry_price
                    notify_trade("REDUCE", close_price, f"减多仓 -50% | 均价→{executor.position.entry_price:,.0f}")
            else:
                result = executor.short_cover(symbol, price=close_price, size=reduce_size)
                if result:
                    notify_trade("REDUCE", close_price, f"减空仓 -50% | 均价→{executor.position.entry_price:,.0f}")

    # === Kline(signal) 详情行 (供 Dashboard 解析) ===
    sig = report.raw_signal if report else "HOLD"
    rsi_v = float(row.get("rsi", 50))
    adx_v = float(row.get("adx", 20))
    macdh = float(row.get("macd_hist", 0))
    regime = strategy._detect_regime(row) if hasattr(strategy, '_detect_regime') else "neutral"
    # 评分
    long_score = int(strategy._signal_ctx.get("long_score", 0)) if hasattr(strategy, '_signal_ctx') else 0
    short_score = int(strategy._signal_ctx.get("short_score", 0)) if hasattr(strategy, '_signal_ctx') else 0
    trend = strategy._trend if hasattr(strategy, '_trend') else "neutral"
    logger.info(
        f"📏 Kline(signal) | RSI={rsi_v:.0f} ADX={adx_v:.0f} MACDh={macdh:.1f}{'✗' if macdh<0 else '✓'}"
        f" | regime={regime} | trend={trend}"
        f" | SHORT={short_score}/9{'✓' if short_score>=6 else '✗'}"
        f" LONG={long_score}/9{'✓' if long_score>=6 else '✗'}"
    )

    # 状态日志
    status = [f"${close_price:,.0f}", f"Eq=${executor.equity:,.0f}"]
    if executor.position:
        status.append(f"Pos={executor.position.unrealized_pnl_pct:+.2f}%")
    if signal:
        status.append(f"SIG={signal}")
    status.append(f"K#{kline_count}")

    logger.info(f"📊 {' | '.join(status)}")

    last_kline_close_time = time.time()

    # 更新 Dashboard 指标
    indicators = {
        "ma7": round(float(row.get("ma_7", 0)), 2),
        "ma25": round(float(row.get("ma_25", 0)), 2),
        "ma99": round(float(row.get("ma_99", 0)), 2),
        "rsi": round(float(row.get("rsi", 50)), 1),
        "adx": round(float(row.get("adx", 20)), 1),
        "macd_hist": round(float(row.get("macd_hist", 0)), 2),
        "volatility": round(float(row.get("volatility", 0)) * 100, 2),
    }
    market_state.set_indicators(indicators)


def main():
    global market_state, ws_client, executor, strategy, alpha_factors, start_time, kline_count, last_hist_retry, _last_kline_key

    init_config()
    start_time = datetime.now()

    logger.info("=" * 60)
    logger.info("🚀 CryptoQuant 高性能实盘引擎")
    logger.info(f"   策略: OptimizedV6 | 合约 | 动态杠杆5-15x")
    logger.info(f"   数据: WebSocket 实时推送 (trade + kline_15m)")
    logger.info(f"   模式: K线闭合触发策略 | 秒级价格更新")
    logger.info(f"   因子: Alpha 因子集 v1.0 (44个因子)")
    logger.info(f"   初始资金: $1000")
    logger.info("=" * 60)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 初始化组件
    executor = FuturesExecutor()
    executor.set_leverage(10)
    strategy = OptimizedStrategy()
    # 用实际权益初始化 peak_equity, 避免回撤误报
    strategy.peak_equity = max(executor.equity, 500)
    logger.info(f"📊 初始权益: ${executor.equity:.0f} | peak_equity={strategy.peak_equity:.0f}")
    alpha_factors = AlphaFactors()

    # 加载 LightGBM 模型 (可选, 如果不存在则降级为纯信号驱动)
    lgb_adapter = LGBAdapter(horizon=24)
    MIN_LGB_AUC = 0.65  # AUC低于此值的模型是噪声, 跳过
    if lgb_adapter.is_loaded():
        lgb_auc = float(lgb_adapter.predictor.metrics.get('auc', 0))
        if lgb_auc >= MIN_LGB_AUC:
            strategy.lgb_adapter = lgb_adapter
            logger.info(f"🧠 LightGBM 双确认已启用 | AUC={lgb_auc:.3f}")
        else:
            logger.info(f"⚠️ LightGBM AUC={lgb_auc:.3f} < {MIN_LGB_AUC}, 跳过 (AUC过低=噪声)")
    else:
        logger.info("⚠️ LightGBM 模型未找到, 仅依赖信号驱动")

    # 启动 WebSocket
    market_state, ws_client = create_price_stream()
    ws_client.start()

    # 等 WS 连上
    logger.info("等待 WebSocket 连接...")
    for _ in range(30):
        if ws_client.connected:
            break
        time.sleep(1)

    if not ws_client.connected:
        logger.error("WebSocket 连接失败, 退出")
        return

    # 等首笔价格
    for _ in range(20):
        if market_state.get_price() > 0:
            break
        time.sleep(0.5)
    logger.info(f"✅ 实时价格: ${market_state.get_price():,.0f}")

    # 拉历史K线初始化策略
    logger.info("预热: 拉取历史K线...")
    df = fetch_historical_klines(strategy)
    if not df.empty:
        logger.info(f"初始数据: {len(df)} 条K线 | {df['datetime'].min()} ~ {df['datetime'].max()}")

        # 初始化 Dashboard 指标（用最后一根K线的值）
        last_row = df.iloc[-1]
        init_indicators = {}
        for key in ["ma_7", "ma_25", "ma_99", "rsi", "adx", "macd_hist", "volatility"]:
            val = last_row.get(key)
            if pd.notna(val):
                init_indicators[key] = round(float(val), 2) if key != "adx" else round(float(val), 1)
        if not init_indicators.get("adx"):
            init_indicators["adx"] = 20.0
        market_state.set_indicators(init_indicators)

        # 重要: 用最后一根K线初始化策略的 latest_indicators
        # 否则策略的 ma20=0 rsi=50 导致第一个信号前所有条件都失败
        try:
            strategy._update_indicators_from_row(last_row)
            logger.info(f"📊 策略指标初始化: RSI={strategy.latest_indicators.get('rsi', 'N/A'):.1f} "
                        f"ADX={strategy.latest_indicators.get('adx', 'N/A'):.1f} "
                        f"MA20={strategy.latest_indicators.get('ma20', 'N/A'):.0f}")
        except Exception as e:
            logger.warning(f"策略指标初始化失败: {e}")

    # 记录当前K线时间，避免重复触发
    init_kline = market_state.get_kline()
    if init_kline:
        last_close = init_kline.close_time / 1000
    else:
        last_close = time.time()

    logger.info(f"🔁 运行中 — 等待K线闭合 (15m间隔)...")

    tick_count = 0
    last_heartbeat = time.time()
    last_minute_log = time.time()
    last_snapshot = 0
    last_tick_time = 0  # tick 级评估计时器

    while running:
        now = time.time()

        # 1. 更新实时价格到 executor
        live_price = market_state.get_price()
        if live_price > 0:
            executor.update_price("BTC-USDT", live_price)

        # === Tick 级实时评估 (每1秒, 限价单检查 + 信号评估) ===
        if live_price > 0 and now - last_tick_time > 1.0:
            last_tick_time = now
            try:
                tick_decision = strategy.on_tick(live_price, executor)
                action = tick_decision.get("action", "HOLD")

                if action in ("PLACE_LIMIT",):
                    # 策略要求挂限价单 — 通过 executor 现有方法以指定价成交
                    side = tick_decision["side"]
                    lp = tick_decision["limit_price"]
                    sz = tick_decision["size"]
                    if side == "short_sell":
                        executor.short_sell("BTC-USDT", size=sz, price=lp)
                        tick_count += 1
                        logger.info(f"🔖 LIMIT SHORT {sz:.6f} @ ${lp:.0f}")
                    elif side == "buy":
                        executor.buy("BTC-USDT", size=sz, price=lp)
                        tick_count += 1
                        logger.info(f"🔖 LIMIT LONG {sz:.6f} @ ${lp:.0f}")

                elif action in ("EXECUTE",):
                    # 策略要求直接平仓 (限价单成交 or 止损)
                    side = tick_decision["side"]
                    if side == "sell":
                        if executor.position and executor.position.side == "long":
                            executor.sell("BTC-USDT", price=live_price)
                            tick_count += 1
                            pnl_raw = (live_price - executor.position.entry_price) / executor.position.entry_price if executor.position else 0
                            if hasattr(strategy, 'record_trade_result'):
                                strategy.record_trade_result(pnl_raw)
                    elif side == "short_cover":
                        if executor.position and executor.position.side == "short":
                            executor.short_cover("BTC-USDT", price=live_price)
                            tick_count += 1
                            pnl_raw = (executor.position.entry_price - live_price) / executor.position.entry_price if executor.position else 0
                            if hasattr(strategy, 'record_trade_result'):
                                strategy.record_trade_result(pnl_raw)

                elif action in ("LONG_FILLED", "SHORT_FILLED"):
                    # 限价单成交 — 记录事件
                    logger.info(f"✅ LIMIT FILLED: {action} @ ${live_price:,.0f}")

            except Exception as e:
                logger.error(f"Tick 评估异常: {e}", exc_info=True)

# 每5秒保存价格快照 (供 Dashboard)
        if now - last_snapshot > 5.0:
            market_state.save_snapshot(str(PRICE_SNAPSHOT))
            # 同时更新 executor state（即使无持仓，保证 Dashboard 读到最新权益）
            if hasattr(executor, '_save_state'):
                executor._save_state()
            last_snapshot = now

        # 每5分钟心跳 (证明进程存活)
        if now - last_minute_log > 300:
            k = market_state.get_kline()
            limit_order_status = ""
            if hasattr(strategy, 'active_limit_order') and strategy.active_limit_order:
                lo = strategy.active_limit_order
                limit_order_status = f" | Limit: {lo.side} @ ${lo.price:.0f}"
            kline_info = f"K:{k.close:.0f}|closed={k.is_closed}" if k else "K:waiting"
            logger.info(f"💚 存活 | ${live_price:,.0f} | {kline_info}{limit_order_status}")
            last_minute_log = now

        # 2. 等待 K 线闭合 (不阻塞, timeout=1s 让 tick 循环跑起来)
        closed = market_state.wait_kline_closed(timeout=1.0)
        if closed:
            # 去重: 确保同一K线只处理一次
            kline_key = (closed.open_time, closed.close_time)
            if kline_key == _last_kline_key:
                continue
            _last_kline_key = kline_key

            kline_count += 1
            logger.info(f"📦 K线闭合 #{kline_count}: O={closed.open:.0f} H={closed.high:.0f} L={closed.low:.0f} C={closed.close:.0f}")

            # 如果 df 为空（初始化拉历史K线失败），用 WS 闭合K线增量构建
            if df.empty:
                row_dict = {
                    "timestamp": closed.open_time, "open": closed.open, "high": closed.high,
                    "low": closed.low, "close": closed.close, "volume": closed.volume,
                    "datetime": pd.to_datetime(closed.open_time, unit="ms"),
                }
                df = pd.DataFrame([row_dict])
                logger.info(f"🔧 WS 在线构建: df 从第1根K线开始累积 (需攒100根, ~25h)")
                continue  # 攒够阈值前不跑策略

            # 追加新K线到 DataFrame（用 loc 而非 concat，保留因子列）
            if not df.empty:
                new_idx = len(df)
                # 用 loc 追加，保留所有现有列
                row_dict = {
                    "timestamp": closed.open_time,
                    "open": closed.open,
                    "high": closed.high,
                    "low": closed.low,
                    "close": closed.close,
                    "volume": closed.volume,
                    "datetime": pd.to_datetime(closed.open_time, unit="ms"),
                }
                df.loc[new_idx] = row_dict

                # 用策略统一的 compute_indicators 重算指标 (单数据源, 避免重复实现)
                if len(df) >= 26:
                    try:
                        df = strategy.compute_indicators(df)
                    except Exception as e:
                        logger.warning(f"指标计算失败: {e}")

                    # Alpha 因子增量更新 (重算最后 ~140 行)
                    try:
                        df = alpha_factors.compute_incremental(df, row_dict)
                    except Exception as e:
                        logger.warning(f"Alpha因子更新失败: {e}")

                # DF 太长就裁剪
                if len(df) > 300:
                    df = df.iloc[-250:]

            # 策略 (最低 100 根K线, 对应 strategy.on_bar 的硬要求)
            if len(df) >= 100:
                run_strategy_on_closed_bar(df)

        # 3. 心跳
        if now - last_heartbeat > 3600:
            account = executor.get_account()
            logger.info(f"💓 心跳 | 运行: {get_uptime()} | 权益: ${account.total_equity:,.0f} | 交易: {account.total_trades}次 | K线: {kline_count}")
            last_heartbeat = now

        # 4. 历史K线定期重试 (WS-only模式时每5分钟尝试一次, 直到攒够数据)
        if len(df) < 100 and now - last_hist_retry > 300:
            last_hist_retry = now
            logger.info(f"🔄 历史K线重试 (当前 {len(df)}/30 根)...")
            try:
                hist_df = fetch_historical_klines()
                if not hist_df.empty and len(hist_df) > len(df):
                    df = hist_df
                    last_row = df.iloc[-1]
                    init_indicators = {}
                    for key in ["ma_7", "ma_25", "ma_99", "rsi", "adx", "macd_hist", "volatility"]:
                        val = last_row.get(key)
                        if pd.notna(val):
                            init_indicators[key] = round(float(val), 2) if key != "adx" else round(float(val), 1)
                    if not init_indicators.get("adx"):
                        init_indicators["adx"] = 20.0
                    market_state.set_indicators(init_indicators)
                    logger.info(f"✅ 历史K线获取成功: {len(df)} 根")
            except Exception as e:
                logger.debug(f"历史K线重试失败: {e}")

    # 清理
    ws_client.stop()
    account = executor.get_account()
    total_return = (account.total_equity - 1000) / 1000 * 100
    logger.info("=" * 60)
    logger.info(f"⏹️ 已停止 | 运行: {get_uptime()} | K线: {kline_count}")
    logger.info(f"💰 最终权益: ${account.total_equity:,.2f} ({total_return:+.2f}%)")
    logger.info(f"📋 总交易: {account.total_trades} | 胜率: {account.win_rate:.1f}%")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()