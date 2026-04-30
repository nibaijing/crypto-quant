"""Binance 交易所适配器 - REST API

用于数据拉取和回测验证。
OKX 网络不可用时作为备选，API 逻辑与 OKX 适配器完全等价。
"""

import logging
from typing import Optional, List
from datetime import datetime

import requests
import pandas as pd

from core.exchange_adapter import Kline, Ticker
from core.config import get_config

logger = logging.getLogger(__name__)


class BinanceAdapter:
    """Binance 适配器 - 轻量级，仅用于数据拉取
    
    与 OKXAdapter 接口一致，方便切换。
    """
    
    BASE_URL = "https://api.binance.com"
    
    def __init__(self):
        self.config = get_config()
        logger.info("Binance适配器初始化")
    
    def get_klines(self, symbol: str, interval: str = "1h", 
                   limit: int = 500, start_time: int = None, 
                   end_time: int = None) -> List[Kline]:
        """获取K线"""
        # 转换交易对格式: BTC-USDT-SWAP → BTCUSDT
        symbol_binance = self._convert_symbol(symbol)
        interval_binance = self._convert_interval(interval)
        
        params = {
            "symbol": symbol_binance,
            "interval": interval_binance,
            "limit": min(limit, 1000),
        }
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        
        try:
            resp = requests.get(
                f"{self.BASE_URL}/api/v3/klines",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            
            if not data:
                return []
            
            klines = []
            for item in data:
                klines.append(Kline(
                    timestamp=item[0],
                    open=float(item[1]),
                    high=float(item[2]),
                    low=float(item[3]),
                    close=float(item[4]),
                    volume=float(item[5]),
                ))
            
            klines.sort(key=lambda x: x.timestamp)
            return klines
            
        except Exception as e:
            logger.error(f"Binance K线获取失败: {e}")
            return []
    
    def get_all_klines(self, symbol: str, bar: str = "1H",
                       start: datetime = None, end: datetime = None) -> List[Kline]:
        """分页获取全量K线"""
        all_klines = []
        start_ts = int(start.timestamp() * 1000) if start else None
        end_ts = int(end.timestamp() * 1000) if end else None
        
        current = start_ts
        while True:
            batch = self.get_klines(
                symbol=symbol, interval=bar, limit=1000,
                start_time=current, end_time=end_ts,
            )
            
            if not batch:
                break
            
            all_klines.extend(batch)
            current = batch[-1].timestamp + 1
            
            if len(batch) < 1000:
                break
            
            import time
            time.sleep(0.3)
        
        # 去重排序
        seen = set()
        unique = []
        for k in sorted(all_klines, key=lambda x: x.timestamp):
            if k.timestamp not in seen:
                seen.add(k.timestamp)
                unique.append(k)
        
        if start_ts:
            unique = [k for k in unique if k.timestamp >= start_ts]
        if end_ts:
            unique = [k for k in unique if k.timestamp <= end_ts]
        
        logger.info(f"Binance K线完成: {symbol} {bar} | {len(unique)} 条")
        return unique
    
    def get_ticker(self, symbol: str) -> Optional[Ticker]:
        """获取行情"""
        symbol_binance = self._convert_symbol(symbol)
        
        try:
            resp = requests.get(
                f"{self.BASE_URL}/api/v3/ticker/24hr",
                params={"symbol": symbol_binance},
                timeout=10,
            )
            data = resp.json()
            
            return Ticker(
                symbol=symbol_binance,
                last=float(data["lastPrice"]),
                bid=float(data["bidPrice"]),
                ask=float(data["askPrice"]),
                high_24h=float(data["highPrice"]),
                low_24h=float(data["lowPrice"]),
                vol_24h=float(data["volume"]),
                timestamp=data["closeTime"],
            )
        except Exception as e:
            logger.error(f"行情获取失败: {e}")
            return None
    
    @staticmethod
    def _convert_symbol(symbol: str) -> str:
        """OKX 格式 → Binance 格式"""
        # BTC-USDT-SWAP → BTCUSDT, BTC-USDT → BTCUSDT
        parts = symbol.replace("-SWAP", "").replace("-", "")
        return parts
    
    @staticmethod
    def _convert_interval(interval: str) -> str:
        """周期格式转换"""
        mapping = {
            "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1H": "1h", "4H": "4h", "1D": "1d",
        }
        return mapping.get(interval, "1h")