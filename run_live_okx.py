#!/usr/bin/env python3
"""OKX实盘交易主循环

功能:
1. 每60秒拉取K线数据
2. 运行策略生成信号
3. 执行实盘下单
4. 每5分钟生成看板HTML
5. 每30分钟通过Telegram推送看板截图
6. 实时风控检查

架构:
    K线数据 → 策略引擎 → 信号 → OKX执行器 → 实盘成交 → 看板
                                                      ↓
                                                 Telegram推送
"""

import sys
import os
import time
import json
import signal
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List

# 确保项目根目录在路径中
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import numpy as np

from core.config import get_config, init_config
from execution.okx_executor import OKXExecutor, OKXAccount
from execution.signals import default_signal_handler
from monitor.dashboard_enhanced import save_dashboard, generate_dashboard_html
from strategies.spot.optimized_v6 import OptimizedStrategy
from data.pipeline import DataPipeline
from notifier import notify_trade

# 日志
log_file = Path(__file__).parent / "data" / "okx_live_trading.log"
log_file.parent.mkdir(parents=True, exist_ok=True)

fh = logging.FileHandler(log_file, encoding="utf-8")
fh.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

root_logger = logging.getLogger()
root_logger.handlers.clear()
root_logger.addHandler(fh)
root_logger.setLevel(logging.INFO)

logger = logging.getLogger("CryptoQuant.OKXLive")

# 全局状态
executor: Optional[OKXExecutor] = None
strategy: Optional[OptimizedStrategy] = None
pipeline: Optional[DataPipeline] = None
running = True
start_time = None
recent_signals: List[str] = []
last_dashboard_time = 0
last_telegram_time = 0
DASHBOARD_INTERVAL = 300   # 5分钟刷新看板
TELEGRAM_INTERVAL = 1800   # 30分钟推送一次


def get_uptime() -> str:
    """获取运行时长"""
    if not start_time:
        return "00:00:00"
    delta = datetime.now() - start_time
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{delta.days}d {hours:02d}:{minutes:02d}:{seconds:02d}"


def fetch_klines_with_history(symbol: str, interval: str = "15m", limit: int = 200) -> pd.DataFrame:
    """拉取K线 + 预计算指标"""
    klines = executor.get_klines(symbol, interval, limit)
    if not klines:
        return pd.DataFrame()
    
    df = pd.DataFrame(klines)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
    
    # 预计算指标
    if len(df) >= 26:
        closes = df["close"].values
        
        df["ma_7"] = df["close"].rolling(7).mean()
        df["ma_25"] = df["close"].rolling(25).mean()
        df["ma_99"] = df["close"].rolling(99).mean()
        
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, 1)
        df["rsi"] = 100 - (100 / (1 + rs))
        
        ema12 = df["close"].ewm(span=12, adjust=False).mean()
        ema26 = df["close"].ewm(span=26, adjust=False).mean()
        df["macd"] = ema12 - ema26
        df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
        df["macd_hist"] = df["macd"] - df["macd_signal"]
        
        df["volatility"] = df["close"].pct_change().rolling(20).std()
        df["volume_ma"] = df["volume"].rolling(20).mean()
    
    return df


def build_bar_data(df: pd.DataFrame, index: int) -> Dict:
    """构建策略所需的 bar_data"""
    row = df.iloc[index]
    
    bar = {
        "timestamp": int(row["timestamp"]),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": float(row["volume"]),
        "history": df.iloc[:index + 1].copy(),
        "index": index,
        "position": None,
    }
    
    for col in ["ma_7", "ma_25", "ma_99", "rsi", "macd", "macd_signal", 
                 "macd_hist", "volatility", "volume_ma"]:
        if col in df.columns:
            val = row.get(col)
            if pd.notna(val):
                bar[col] = float(val)
    
    return bar


def process_tick():
    """处理一次行情tick: 拉数据 → 动态杠杆 → 策略 → 信号 → 执行"""
    global recent_signals
    
    symbol = "BTC-USDT-SWAP"
    close = 0
    
    # 先更新价格
    ticker = executor.get_ticker(symbol)
    if ticker:
        close = ticker["price"]
        executor.update_price(symbol, close)
    
    # 拉K线
    df = fetch_klines_with_history(symbol, "15m", 200)
    if df.empty or len(df) < 26:
        logger.debug("K线数据不足, 跳过信号生成")
        return
        
    # === 动态杠杆 (使用策略的方法) ===
    if len(df) >= 20:
        vol = df["close"].pct_change().rolling(20).std().iloc[-1]
        if pd.notna(vol):
            target_lev = strategy.get_dynamic_leverage(vol)
            executor.set_leverage(target_lev)
            logger.debug(f"动态杠杆: {target_lev}x (vol={vol:.4f})")
    
    # 最新一根K线给策略
    latest_idx = len(df) - 1
    bar = build_bar_data(df, latest_idx)
    
    # 更新 bar 里的 position
    if executor.position:
        pos = executor.position
        bar["position"] = type('obj', (object,), {
            'size': pos.size,
            'side': pos.side,
            'avg_price': pos.entry_price,
            'leverage': pos.leverage,
        })()
    
    # 运行策略
    try:
        signal = strategy.on_bar(bar, executor)
        executor.update_bars_held()
    except Exception as e:
        logger.error(f"策略执行错误: {e}", exc_info=True)
        signal = None
    
    # 记录信号
    if signal and signal != "None" and signal != None:
        recent_signals.append(
            f"[{datetime.now().strftime('%H:%M')}] {signal} @ ${close:,.2f}"
        )
        if len(recent_signals) > 20:
            recent_signals = recent_signals[-20:]
    
    # 执行信号
    if signal and signal != "HOLD" and signal is not None:
        logger.info(f"📡 信号: {signal} | close=${close:,.2f}")

        if signal == "LONG" and (not executor.position or executor.position.size == 0):
            result = executor.buy(symbol, price=close)
            if result:
                notify_trade("LONG", close, f"开多仓 {executor._leverage}x")
        elif signal == "SELL" and executor.position and executor.position.side == "long":
            lev = executor.position.leverage
            pnl_pct = (close - executor.position.entry_price) / executor.position.entry_price * 100 * lev
            result = executor.sell(symbol, price=close)
            if result:
                notify_trade("SELL", close, f"平多仓 | PnL={pnl_pct:+.2f}% ({lev}x)")
        elif signal == "SHORT" and (not executor.position or executor.position.size == 0):
            result = executor.short_sell(symbol, price=close)
            if result:
                notify_trade("SHORT", close, f"开空仓 {executor._leverage}x")
        elif signal == "COVER" and executor.position and executor.position.side == "short":
            lev = executor.position.leverage
            pnl_pct = (executor.position.entry_price - close) / executor.position.entry_price * 100 * lev
            result = executor.short_cover(symbol, price=close)
            if result:
                notify_trade("COVER", close, f"平空仓 | PnL={pnl_pct:+.2f}% ({lev}x)")
    
    # 记录状态
    status_parts = [
        f"${close:,.0f}",
        f"Eq=${executor.equity:,.0f}",
    ]
    if executor.position:
        status_parts.append(f"Pos={executor.position.unrealized_pnl_pct:+.2f}%")
    
    if signal:
        status_parts.append(f"SIG={signal}")
    
    logger.info(f"📊 {' | '.join(status_parts)}")


def generate_and_push_dashboard(force: bool = False):
    """生成看板"""
    global last_dashboard_time
    now = time.time()
    if force or (now - last_dashboard_time) >= DASHBOARD_INTERVAL:
        last_dashboard_time = now
        
        try:
            # 获取账户信息
            account = executor.get_account()
            
            # 计算保证金使用率和强平距离
            margin_used = 0
            liq_distance = 1.0
            
            if account.positions:
                pos = account.positions[0]
                if account.total_equity > 0:
                    margin_used = pos.margin / account.total_equity
                
                if pos.liq_price > 0 and pos.mark_price > 0:
                    if pos.side == "long":
                        liq_distance = (pos.mark_price - pos.liq_price) / pos.mark_price
                    else:
                        liq_distance = (pos.liq_price - pos.mark_price) / pos.mark_price
            
            # 生成HTML
            html = generate_dashboard_html(
                account=account,
                strategy_name="MATrend(7/25)",
                mode="LIVE" if not executor.config.exchange.testnet else "SIMULATION",
                symbol="BTC-USDT-SWAP",
                uptime=get_uptime(),
                extra_info={"recent_signals": recent_signals},
                api_connected=account.api_connected,
                risk_level=account.risk_level,
                margin_used=margin_used,
                liq_distance=liq_distance,
            )
            
            # 保存到文件
            dashboard_path = executor.config.project_root / "data" / "okx_dashboard.html"
            dashboard_path.write_text(html, encoding="utf-8")
            
            logger.info(f"看板已生成: {dashboard_path}")
            
        except Exception as e:
            logger.error(f"生成看板失败: {e}", exc_info=True)


def signal_handler(sig, frame):
    global running
    logger.info("🛑 收到停止信号, 正在关闭...")
    running = False


def main():
    global executor, strategy, pipeline, start_time
    
    config = init_config()
    start_time = datetime.now()
    
    # 检查API配置
    if not config.exchange.api_key or not config.exchange.api_secret:
        logger.error("❌ 缺少OKX API配置，请在config/settings.yaml中设置api_key和api_secret")
        logger.error("   或者设置环境变量: CQ_EXCHANGE__API_KEY, CQ_EXCHANGE__API_SECRET")
        sys.exit(1)
    
    logger.info("=" * 60)
    logger.info("🚀 CryptoQuant OKX实盘启动")
    logger.info(f"   策略: OptimizedV6 自动优化 | 合约 | 动态杠杆5-15x")
    logger.info(f"   特性: MACD确认 + ADX过滤 + ATR止损 + 资金管理")
    logger.info(f"   交易对: BTC-USDT-SWAP")
    logger.info(f"   模式: {'实盘' if not config.exchange.testnet else '模拟盘'}")
    logger.info(f"   K线周期: 15m")
    logger.info(f"   杠杆: 动态 (波动率高→5x, 中→10x, 低→15x)")
    logger.info(f"   检查间隔: 60秒")
    logger.info("=" * 60)
    
    # 信号处理
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 初始化组件
    try:
        executor = OKXExecutor(config)
        executor.set_leverage(10)  # 默认10x
        strategy = OptimizedStrategy()
        pipeline = DataPipeline()
        
        logger.info("✅ 组件初始化成功")
    except Exception as e:
        logger.error(f"❌ 组件初始化失败: {e}", exc_info=True)
        sys.exit(1)
    
    # 预热: 拉初始数据
    logger.info("预热: 拉取初始K线数据...")
    df = fetch_klines_with_history("BTC-USDT-SWAP", "15m", 200)
    if not df.empty:
        logger.info(f"初始数据: {len(df)} 条K线 | "
                     f"{df['datetime'].min()} ~ {df['datetime'].max()}")
    else:
        logger.warning("⚠️  初始数据为空，请检查网络连接")
    
    # 生成首次看板
    generate_and_push_dashboard(force=True)
    
    # 主循环
    tick_count = 0
    while running:
        try:
            tick_count += 1
            
            # 处理行情
            process_tick()
            
            # 生成看板 (5分钟)
            generate_and_push_dashboard()
            
            # 每分钟打一次心跳
            if tick_count % 60 == 0:
                account = executor.get_account()
                logger.info(
                    f"💓 心跳 | 运行: {get_uptime()} | "
                    f"权益: ${account.total_equity:,.2f} | "
                    f"交易: {account.total_trades}次 | "
                    f"风险: {account.risk_level}"
                )
            
            # 等待下一轮
            time.sleep(60)
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"主循环错误: {e}", exc_info=True)
            time.sleep(10)
    
    # 清理
    logger.info("=" * 60)
    logger.info(f"⏹️ OKX实盘已停止 | 运行: {get_uptime()} | 总tick: {tick_count}")
    
    # 最终看板
    generate_and_push_dashboard(force=True)
    
    account = executor.get_account()
    total_return = (account.total_equity - 10000) / 10000 * 100 if account.total_equity > 0 else 0
    logger.info(f"💰 最终权益: ${account.total_equity:,.2f} ({total_return:+.2f}%)")
    logger.info(f"📋 总交易: {account.total_trades} | 胜率: {account.win_rate:.1f}%")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
