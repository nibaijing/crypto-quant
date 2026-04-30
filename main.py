#!/usr/bin/env python3
"""加密货币合约量化交易系统 - 主入口

使用方法:
    # 拉取历史数据
    python main.py fetch --symbols BTC-USDT-SWAP,BTC-USDT --days 90
    
    # 运行回测 (现货策略)
    python main.py backtest --strategy spot --data BTC_USDT_1H.parquet
    
    # 运行回测 (合约策略)
    python main.py backtest --strategy futures --data BTC_USDT_SWAP_1H.parquet
"""

import argparse
import logging
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import init_config, get_config
from data.pipeline import DataPipeline, fetch_historical_data
from backtest.engine import BacktestEngine
from backtest.reporter import BacktestReporter
from execution.signals import default_signal_handler
from strategies.spot.ma_rsi_macd import MATrendStrategy
from strategies.futures.trend_following import FuturesTrendStrategy

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("CryptoQuant")


def cmd_fetch(args):
    """拉取历史K线数据"""
    symbols = args.symbols.split(",") if args.symbols else None
    results = fetch_historical_data(
        symbols=symbols,
        timeframe=args.timeframe,
        days=args.days,
    )
    
    for symbol, df in results.items():
        if not df.empty:
            print(f"  {symbol}: {len(df)} 条记录 | "
                  f"{df['datetime'].min()} ~ {df['datetime'].max()}")
        else:
            print(f"  {symbol}: 未获取到数据")


def cmd_backtest(args):
    """运行回测"""
    config = get_config()
    pipeline = DataPipeline()
    
    # 加载数据
    if args.data:
        # 从指定文件加载
        file_path = Path(args.data)
        if file_path.exists():
            df = pipeline.load_klines(
                file_path.stem.rsplit("_", 1)[0].replace("_", "-"),
                file_path.stem.rsplit("_", 1)[1],
            )
        else:
            df = pipeline.load_klines(
                args.data.replace(".parquet", "").replace("_", "-"),
                args.timeframe or "1H",
            )
    else:
        # 默认加载 BTC-USDT 1H
        symbol = args.futures and "BTC-USDT-SWAP" or "BTC-USDT"
        df = pipeline.load_klines(symbol, args.timeframe or "1H")
        
        # 如果本地没有, 尝试拉取
        if df is None:
            logger.info(f"本地无数据, 正在拉取 {symbol}...")
            df = pipeline.fetch_and_save(symbol, args.timeframe or "1H", args.days or 90)
    
    if df is None or df.empty:
        logger.error("没有可用数据, 请先运行 fetch 命令拉取历史数据")
        return
    
    # 选择策略
    if args.strategy == "futures":
        strategy = FuturesTrendStrategy()
        config.symbol = "BTC-USDT-SWAP"
        is_futures = True
    else:
        strategy = MATrendStrategy()
        config.symbol = "BTC-USDT"
        is_futures = False
    
    # 创建回测引擎
    engine = BacktestEngine(
        data=df,
        initial_capital=args.capital or 10000,
    )
    
    # 注册策略和信号处理器
    engine.register_strategy(strategy.on_bar)
    engine.on_signal(default_signal_handler)
    
    # 运行回测
    results = engine.run()
    
    # 生成报告
    reporter = BacktestReporter(results, strategy_name=strategy.name)
    md_path = reporter.save_markdown()
    
    # 绘图
    equity_path = config.results_path / "equity_curve.png"
    engine.plot_equity_curve(save_path=str(equity_path))
    
    # 成交点位图
    trades_path = config.results_path / "trade_points.png"
    engine.plot_trades(save_path=str(trades_path))
    
    print(f"\n📁 报告: {md_path}")
    print(f"📈 权益曲线: {equity_path}")
    print(f"📊 成交点位: {trades_path}")


def cmd_list(args):
    """列出已保存的数据"""
    pipeline = DataPipeline()
    available = pipeline.get_available_data()
    
    if not available:
        print("暂无已保存的数据。运行 'python main.py fetch' 来拉取历史数据。")
        return
    
    print("\n已保存的K线数据:")
    for symbol, timeframes in available.items():
        print(f"\n  {symbol}:")
        for tf in sorted(timeframes):
            file_path = pipeline._kline_file_path(symbol.replace("-", "_"), tf)
            df = pipeline.load_klines(symbol.replace("-", "_"), tf)
            if df is not None:
                size_mb = file_path.stat().st_size / 1024 / 1024 if file_path.exists() else 0
                print(f"    {tf:4s} | {len(df):5d} 条 | {size_mb:.1f}MB | "
                      f"{df['datetime'].min().strftime('%Y-%m-%d')} ~ "
                      f"{df['datetime'].max().strftime('%Y-%m-%d')}")


def main():
    parser = argparse.ArgumentParser(
        description="加密货币合约量化交易系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    subparsers = parser.add_subparsers(dest="command", help="子命令")
    
    # fetch
    parser_fetch = subparsers.add_parser("fetch", help="拉取历史K线数据")
    parser_fetch.add_argument("--symbols", default="BTC-USDT-SWAP,BTC-USDT",
                              help="交易对列表, 逗号分隔 (默认: BTC合约+现货)")
    parser_fetch.add_argument("--timeframe", default="1H",
                              help="K线周期 (1m/5m/15m/1H/4H/1D)")
    parser_fetch.add_argument("--days", type=int, default=90,
                              help="拉取天数 (默认: 90)")
    
    # backtest
    parser_bt = subparsers.add_parser("backtest", help="运行回测")
    parser_bt.add_argument("--strategy", default="spot",
                           choices=["spot", "futures"],
                           help="策略类型 (spot=现货MA趋势, futures=合约趋势)")
    parser_bt.add_argument("--data", help="数据文件路径")
    parser_bt.add_argument("--timeframe", default="1H", help="K线周期")
    parser_bt.add_argument("--capital", type=float, help="初始资金")
    parser_bt.add_argument("--days", type=int, help="数据天数 (本地无数据时自动拉取)")
    
    # list
    parser_list = subparsers.add_parser("list", help="列出已保存的数据")
    
    args = parser.parse_args()
    
    if args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "backtest":
        cmd_backtest(args)
    elif args.command == "list":
        cmd_list(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()