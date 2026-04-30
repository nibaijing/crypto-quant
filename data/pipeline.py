"""数据管道 - 历史K线拉取 + 实时行情管理

支持从 OKX / Binance 批量拉取历史K线，保存为 Parquet 格式，加载到 DataFrame。
网络不可达时自动 fallback 到 Binance。
"""

import asyncio
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict

import pandas as pd

from core.config import get_config
from core.exchange_adapter import OKXAdapter, Kline

logger = logging.getLogger(__name__)


class DataPipeline:
    """数据管道 - 管理历史数据获取和存储"""
    
    def __init__(self, adapter = None):
        self.config = get_config()
        
        if adapter:
            self.adapter = adapter
            self._adapter_type = "custom"
        else:
            # 先试 OKX，不行 fallback 到 Binance
            from core.exchange_adapter import OKXAdapter
            from core.binance_adapter import BinanceAdapter
            
            try:
                okx = OKXAdapter(self.config)
                # 快速测试连接
                ticker = okx.get_ticker("BTC-USDT")
                if ticker:
                    self.adapter = okx
                    self._adapter_type = "okx"
                    logger.info("使用 OKX 适配器")
                else:
                    raise Exception("OKX 无响应")
            except Exception as e:
                logger.warning(f"OKX 不可用 ({e}), 切换到 Binance")
                self.adapter = BinanceAdapter()
                self._adapter_type = "binance"
        
        self._cache: Dict[str, pd.DataFrame] = {}
    
    def _kline_file_path(self, symbol: str, timeframe: str) -> Path:
        """K线数据文件路径"""
        safe_symbol = symbol.replace("-", "_").replace("/", "_")
        return self.config.kline_path / f"{safe_symbol}_{timeframe}.parquet"
    
    def fetch_and_save(self, symbol: str, timeframe: str = "1H",
                       days: int = 90) -> pd.DataFrame:
        """拉取历史K线并保存为 Parquet
        
        Args:
            symbol: 交易对，如 BTC-USDT-SWAP
            timeframe: K线周期 1m/5m/15m/30m/1H/4H/1D
            days: 拉取最近多少天的数据
        
        Returns:
            DataFrame 格式的K线数据
        """
        end = datetime.now()
        start = end - timedelta(days=days)
        
        logger.info(f"拉取历史K线: {symbol} {timeframe} | {start.date()} ~ {end.date()}")
        
        klines = self.adapter.get_all_klines(
            symbol=symbol,
            bar=timeframe,
            start=start,
            end=end,
        )
        
        if not klines:
            logger.warning(f"未获取到数据: {symbol}")
            return pd.DataFrame()
        
        # 转换为 DataFrame
        df = self._klines_to_df(klines)
        
        # 保存
        file_path = self._kline_file_path(symbol, timeframe)
        df.to_parquet(file_path, index=False)
        
        logger.info(f"数据已保存: {file_path} | {len(df)} 条记录")
        
        # 更新缓存
        self._cache[f"{symbol}_{timeframe}"] = df
        
        return df
    
    def load_klines(self, symbol: str, timeframe: str = "1H") -> Optional[pd.DataFrame]:
        """加载已保存的K线数据
        
        优先 Parquet，fallback CSV。
        """
        cache_key = f"{symbol}_{timeframe}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        file_path = self._kline_file_path(symbol, timeframe)
        csv_path = file_path.with_suffix(".csv")
        
        if file_path.exists():
            df = pd.read_parquet(file_path)
        elif csv_path.exists():
            df = pd.read_csv(csv_path)
            logger.info(f"从 CSV 加载: {csv_path}")
        else:
            logger.warning(f"K线数据文件不存在: {file_path}")
            return None
        
        if "timestamp" in df.columns:
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
        
        self._cache[cache_key] = df
        return df
    
    def refresh_klines(self, symbol: str, timeframe: str = "1H") -> pd.DataFrame:
        """更新K线数据（增量拉取，补充最新数据）
        
        如果已有历史数据，只拉取缺失的最近部分。
        """
        existing = self.load_klines(symbol, timeframe)
        
        if existing is not None and not existing.empty:
            # 增量更新：从最后一条数据之后开始拉
            last_ts = existing["timestamp"].max()
            last_time = datetime.fromtimestamp(last_ts / 1000)
            
            # 如果最后一条数据在1小时内，不需要更新
            if (datetime.now() - last_time).total_seconds() < 3600:
                logger.info(f"数据已是最新: {symbol}")
                return existing
            
            # 拉取增量
            new_klines = self.adapter.get_all_klines(
                symbol=symbol, bar=timeframe,
                start=last_time + timedelta(seconds=1),
                end=datetime.now(),
            )
        else:
            # 全量拉取
            new_klines = self.adapter.get_all_klines(
                symbol=symbol, bar=timeframe,
                start=datetime.now() - timedelta(days=90),
                end=datetime.now(),
            )
        
        if not new_klines:
            return existing if existing is not None else pd.DataFrame()
        
        new_df = self._klines_to_df(new_klines)
        
        if existing is not None and not existing.empty:
            df = pd.concat([existing, new_df], ignore_index=True)
            df.drop_duplicates(subset=["timestamp"], inplace=True)
            df.sort_values("timestamp", inplace=True)
            df.reset_index(drop=True, inplace=True)
        else:
            df = new_df
        
        # 保存
        file_path = self._kline_file_path(symbol, timeframe)
        df.to_parquet(file_path, index=False)
        
        cache_key = f"{symbol}_{timeframe}"
        self._cache[cache_key] = df
        
        logger.info(f"数据已更新: {symbol} | {len(df)} 条")
        return df
    
    def _klines_to_df(self, klines: List[Kline]) -> pd.DataFrame:
        """将 Kline 列表转为 DataFrame，附带技术指标列"""
        records = [k.to_dict() for k in klines]
        df = pd.DataFrame(records)
        
        # 添加 datetime 列
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
        
        # 预计算常用技术指标列 (方便回测使用)
        if len(df) >= 26:
            df["ma_7"] = df["close"].rolling(window=7).mean()
            df["ma_25"] = df["close"].rolling(window=25).mean()
            df["ma_99"] = df["close"].rolling(window=99).mean()
            
            # RSI
            delta = df["close"].diff()
            gain = delta.where(delta > 0, 0).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            df["rsi"] = 100 - (100 / (1 + rs))
            
            # EMA (for MACD)
            ema_12 = df["close"].ewm(span=12, adjust=False).mean()
            ema_26 = df["close"].ewm(span=26, adjust=False).mean()
            df["macd"] = ema_12 - ema_26
            df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
            df["macd_hist"] = df["macd"] - df["macd_signal"]
            
            # 波动率 (20周期)
            df["volatility"] = df["close"].pct_change().rolling(window=20).std()
            
            # 成交量均线
            df["volume_ma"] = df["volume"].rolling(window=20).mean()
        
        return df
    
    def get_available_data(self) -> Dict[str, List[str]]:
        """列出已保存的数据文件"""
        available = {}
        for f in self.config.kline_path.glob("*.parquet"):
            name = f.stem
            parts = name.rsplit("_", 1)
            if len(parts) == 2:
                symbol = parts[0].replace("_", "-")
                tf = parts[1]
                if symbol not in available:
                    available[symbol] = []
                available[symbol].append(tf)
            else:
                if "unknown" not in available:
                    available["unknown"] = []
                available["unknown"].append(name)
        
        return available


# ===== 便捷函数 =====

def fetch_historical_data(symbols: List[str] = None, 
                          timeframe: str = "1H",
                          days: int = 90) -> Dict[str, pd.DataFrame]:
    """快速拉取多个交易对的历史数据
    
    Args:
        symbols: 交易对列表，默认 ['BTC-USDT-SWAP', 'BTC-USDT']
        timeframe: K线周期
        days: 拉取天数
    
    Returns:
        {symbol: DataFrame}
    """
    if symbols is None:
        symbols = [
            "BTC-USDT-SWAP",    # BTC 永续合约
            "BTC-USDT",          # BTC 现货
            "ETH-USDT-SWAP",     # ETH 永续
            "ETH-USDT",          # ETH 现货
        ]
    
    pipeline = DataPipeline()
    results = {}
    
    for symbol in symbols:
        try:
            logger.info(f"--- 拉取 {symbol} ---")
            df = pipeline.fetch_and_save(symbol, timeframe, days)
            results[symbol] = df
        except Exception as e:
            logger.error(f"拉取 {symbol} 失败: {e}")
            results[symbol] = pd.DataFrame()
    
    return results