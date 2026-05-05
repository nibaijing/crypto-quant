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
from execution.ai_override import get_decision_engine
from execution.signals import SignalReport, FinalDecision
from strategies.spot.optimized_v6 import OptimizedStrategy
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


def fetch_historical_klines() -> pd.DataFrame:
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
        df["ma_7"] = df["close"].rolling(7).mean()
        df["ma_25"] = df["close"].rolling(25).mean()
        df["ma_99"] = df["close"].rolling(99).mean()

        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        df["rsi"] = 100 - (100 / (1 + gain / loss.replace(0, 1)))

        ema12 = df["close"].ewm(span=12, adjust=False).mean()
        ema26 = df["close"].ewm(span=26, adjust=False).mean()
        df["macd"] = ema12 - ema26
        df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
        df["macd_hist"] = df["macd"] - df["macd_signal"]
        df["volatility"] = df["close"].pct_change().rolling(20).std()
        df["volume_ma"] = df["volume"].rolling(20).mean()

        # ADX (简版)
        tr = pd.concat([df['high'] - df['low'], (df['high'] - df['close'].shift(1)).abs(), (df['low'] - df['close'].shift(1)).abs()], axis=1).max(axis=1)
        atr_14 = tr.rolling(14).mean()
        plus_dm = ((df['high'].diff() > df['low'].diff() * -1) & (df['high'].diff() > 0)).astype(float) * df['high'].diff()
        minus_dm = ((df['low'].diff() * -1 > df['high'].diff()) & (df['low'].diff() * -1 > 0)).astype(float) * (-df['low'].diff())
        plus_di = 100 * plus_dm.rolling(14).mean() / atr_14
        minus_di = 100 * minus_dm.rolling(14).mean() / atr_14
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, 1)) * 100
        df["adx"] = dx.rolling(14).mean()

        # Alpha 因子集
        df = alpha_factors.compute(df)

    return df


def run_strategy_on_closed_bar(df: pd.DataFrame):
    """K线闭合时运行策略"""
    global kline_count, last_kline_close_time

    if len(df) < 100:
        return

    # 最新闭合K线的索引
    latest_idx = len(df) - 1
    row = df.iloc[latest_idx]
    close_price = float(row["close"])

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

    # === DecisionEngine 决策层 ===
    if report is not None:
        # 注入当前持仓状态, 供 DecisionEngine 感知
        if executor.position and executor.position.size > 0:
            report.current_position = executor.position.side  # "long" or "short"
        engine = get_decision_engine()
        decision = engine.decide(report)

        # 最终决策
        final_action = decision.action
        if decision.source == "risk_management":
            logger.info(f"🚨 风控平仓: {final_action} | reason={decision.reasoning[:60]}")
        elif decision.source == "auto_clear":
            if final_action != "HOLD":
                logger.info(f"⚡ 自动放行: {final_action} | confidence={decision.confidence:.0%} | {decision.reasoning[:60]}")
            else:
                logger.debug(f"⏸️ 决策引擎: HOLD | {decision.reasoning[:60]}")
        elif decision.source == "ai_decision":
            logger.info(f"{'✅' if final_action != 'HOLD' else '⏸️'} AI决策: {final_action} | "
                       f"confidence={decision.confidence:.0%} | {decision.reasoning[:80]}")
    else:
        final_action = "HOLD"

    # 执行信号
    signal = final_action  # 保持变量名兼容
    symbol = "BTC-USDT"
    if signal and signal != "HOLD":
        logger.info(f"📡 信号: {signal} | close=${close_price:,.0f}")

        if signal == "LONG" and (not executor.position or executor.position.size == 0):
            result = executor.buy(symbol, price=close_price)
            if result:
                notify_trade("LONG", close_price, f"开多仓 {executor._sim_leverage}x")

        elif signal == "SELL" and executor.position and executor.position.side == "long":
            lev = executor.position.leverage
            pnl_pct = (close_price - executor.position.entry_price) / executor.position.entry_price * 100 * lev
            result = executor.sell(symbol, price=close_price)
            if result:
                notify_trade("SELL", close_price, f"平多仓 | PnL={pnl_pct:+.2f}% ({lev}x)")
                engine = get_decision_engine()
                engine.update_trade_result(pnl_pct)

        elif signal == "SHORT" and (not executor.position or executor.position.size == 0):
            result = executor.short_sell(symbol, price=close_price)
            if result:
                notify_trade("SHORT", close_price, f"开空仓 {executor._sim_leverage}x")

        elif signal == "COVER" and executor.position and executor.position.side == "short":
            lev = executor.position.leverage
            pnl_pct = (executor.position.entry_price - close_price) / executor.position.entry_price * 100 * lev
            result = executor.short_cover(symbol, price=close_price)
            if result:
                notify_trade("COVER", close_price, f"平空仓 | PnL={pnl_pct:+.2f}% ({lev}x)")
                engine = get_decision_engine()
                engine.update_trade_result(pnl_pct)

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
    global market_state, ws_client, executor, strategy, alpha_factors, start_time, kline_count

    init_config()
    start_time = datetime.now()

    logger.info("=" * 60)
    logger.info("🚀 CryptoQuant 高性能实盘引擎")
    logger.info(f"   策略: OptimizedV6 | 合约 | 动态杠杆5-15x")
    logger.info(f"   数据: WebSocket 实时推送 (trade + kline_15m)")
    logger.info(f"   模式: K线闭合触发策略 | 秒级价格更新")
    logger.info(f"   因子: Alpha 因子集 v1.0 (44个因子)")
    logger.info(f"   初始资金: $1,000")
    logger.info("=" * 60)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 初始化组件
    executor = FuturesExecutor()
    executor.set_leverage(10)
    strategy = OptimizedStrategy()
    alpha_factors = AlphaFactors()

    # 加载 LightGBM 模型 (可选, 如果不存在则降级为纯 MATrend)
    lgb_adapter = LGBAdapter(horizon=24)
    if lgb_adapter.is_loaded():
        strategy.lgb_adapter = lgb_adapter
        logger.info(f"🧠 LightGBM 双确认已启用 | AUC={lgb_adapter.predictor.metrics.get('auc', '?'):.3f}")
    else:
        logger.info("⚠️ LightGBM 模型未找到, 仅依赖 MATrend 信号")

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
    df = fetch_historical_klines()
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

    while running:
        now = time.time()

        # 1. 更新实时价格到 executor
        live_price = market_state.get_price()
        if live_price > 0:
            executor.update_price("BTC-USDT", live_price)
            # 无持仓时也定期保存 (供 Dashboard)
            if not executor.position:
                if not hasattr(executor, '_dashboard_save'):
                    executor._dashboard_save = 0
                if now - executor._dashboard_save > 5:
                    executor._save_state()
                    executor._dashboard_save = now

        # 1.5 每秒保存价格快照 (供 Dashboard)
        if now - last_snapshot > 1.0:
            market_state.save_snapshot(str(PRICE_SNAPSHOT))
            last_snapshot = now

        # 每分钟心跳 (证明进程存活)
        if now - last_minute_log > 60:
            k = market_state.get_kline()
            kline_info = f"K:{k.close:.0f}|closed={k.is_closed}" if k else "K:waiting"
            logger.info(f"💚 存活 | ${live_price:,.0f} | {kline_info} | 等待闭合...")
            last_minute_log = now

        # 2. 等待 K 线闭合
        closed = market_state.wait_kline_closed(timeout=1.0)
        if closed:
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
                logger.info(f"🔧 WS 在线构建: df 从第1根K线开始累积")
                continue  # 先攒够 100 根再跑策略

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

                # 重算基础指标 + Alpha 因子（增量）
                if len(df) >= 26:
                    df["ma_7"] = df["close"].rolling(7).mean()
                    df["ma_25"] = df["close"].rolling(25).mean()
                    df["ma_99"] = df["close"].rolling(99).mean()
                    delta = df["close"].diff()
                    gain = delta.where(delta > 0, 0).rolling(14).mean()
                    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                    df["rsi"] = 100 - (100 / (1 + gain / loss.replace(0, 1)))
                    ema12 = df["close"].ewm(span=12, adjust=False).mean()
                    ema26 = df["close"].ewm(span=26, adjust=False).mean()
                    df["macd"] = ema12 - ema26
                    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
                    df["macd_hist"] = df["macd"] - df["macd_signal"]
                    df["volatility"] = df["close"].pct_change().rolling(20).std()

                    # 成交量确认 (修复: 之前遗漏未重算)
                    df["volume_ma"] = df["volume"].rolling(20).mean()
                    df["volume_surge"] = df["volume"] > df["volume_ma"] * 1.5

                    # ADX
                    tr = pd.concat([df['high'] - df['low'], (df['high'] - df['close'].shift(1)).abs(), (df['low'] - df['close'].shift(1)).abs()], axis=1).max(axis=1)
                    atr_14 = tr.rolling(14).mean()
                    plus_dm = ((df['high'].diff() > df['low'].diff() * -1) & (df['high'].diff() > 0)).astype(float) * df['high'].diff()
                    minus_dm = ((df['low'].diff() * -1 > df['high'].diff()) & (df['low'].diff() * -1 > 0)).astype(float) * (-df['low'].diff())
                    plus_di = 100 * plus_dm.rolling(14).mean() / atr_14
                    minus_di = 100 * minus_dm.rolling(14).mean() / atr_14
                    dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, 1)) * 100
                    df["adx"] = dx.rolling(14).mean()

                    # Alpha 因子增量更新 (重算最后 ~140 行)
                    try:
                        df = alpha_factors.compute_incremental(df, row_dict)
                    except Exception as e:
                        logger.warning(f"Alpha因子更新失败: {e}")

                # DF 太长就裁剪
                if len(df) > 300:
                    df = df.iloc[-250:]

            # 策略
            if len(df) >= 100:
                run_strategy_on_closed_bar(df)

        # 3. 心跳
        if now - last_heartbeat > 3600:
            account = executor.get_account()
            logger.info(f"💓 心跳 | 运行: {get_uptime()} | 权益: ${account.total_equity:,.0f} | 交易: {account.total_trades}次 | K线: {kline_count}")
            last_heartbeat = now

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