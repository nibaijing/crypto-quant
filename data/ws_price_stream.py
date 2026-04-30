#!/usr/bin/env python3
"""
Binance WebSocket 实时行情流

订阅:
  - BTCUSDT@ticker  (24hr 统计)
  - BTCUSDT@trade   (逐笔成交，用于实时价格)
  - BTCUSDT@kline_15m (15分钟K线)

架构:
  WebSocket → 回调 → SharedState (线程安全)
  run_live 主线程读取 SharedState，K线闭合时触发策略
"""

import json
import time
import logging
import threading
from typing import Optional, Callable, Dict
from dataclasses import dataclass, field
from datetime import datetime

import websocket

logger = logging.getLogger(__name__)


@dataclass
class KlineBar:
    """单根K线"""
    open_time: int = 0
    close_time: int = 0
    open: float = 0
    high: float = 0
    low: float = 0
    close: float = 0
    volume: float = 0
    is_closed: bool = False  # 是否已闭合


@dataclass
class SharedMarketState:
    """线程安全的共享市场状态"""
    price: float = 0
    price_updated_at: float = 0

    # 24hr ticker
    price_change_pct: float = 0
    high_24h: float = 0
    low_24h: float = 0
    volume_24h: float = 0

    # 当前15m K线
    current_kline: Optional[KlineBar] = None

    # K线闭合事件
    kline_closed_event: threading.Event = field(default_factory=threading.Event)
    closed_kline: Optional[KlineBar] = None

    _lock: threading.Lock = field(default_factory=threading.Lock)

    def update_price(self, price: float):
        with self._lock:
            self.price = price
            self.price_updated_at = time.time()

    def update_ticker(self, data: Dict):
        with self._lock:
            self.price = float(data.get('c', self.price))
            self.price_change_pct = float(data.get('P', 0))
            self.high_24h = float(data.get('h', 0))
            self.low_24h = float(data.get('l', 0))
            self.volume_24h = float(data.get('v', 0))
            self.price_updated_at = time.time()

    def update_kline(self, k: Dict):
        """更新当前K线"""
        bar = KlineBar(
            open_time=k['t'],
            close_time=k['T'],
            open=float(k['o']),
            high=float(k['h']),
            low=float(k['l']),
            close=float(k['c']),
            volume=float(k['v']),
            is_closed=k['x'],
        )
        with self._lock:
            self.current_kline = bar
            if bar.is_closed:
                # 闭合K线 → 通知策略
                self.closed_kline = bar
                self.kline_closed_event.set()

    def get_price(self) -> float:
        with self._lock:
            return self.price

    def get_kline(self) -> Optional[KlineBar]:
        with self._lock:
            return self.current_kline

    def get_snapshot(self) -> dict:
        """返回当前快照 (供 Dashboard 写入)"""
        with self._lock:
            k = self.current_kline
            indicators = {}
            if hasattr(self, 'latest_indicators'):
                indicators = self.latest_indicators
            return {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "price": self.price,
                "change_pct": self.price_change_pct,
                "high_24h": self.high_24h,
                "low_24h": self.low_24h,
                "volume_24h": self.volume_24h,
                "kline": {
                    "open": k.open if k else 0,
                    "high": k.high if k else 0,
                    "low": k.low if k else 0,
                    "close": k.close if k else 0,
                    "is_closed": k.is_closed if k else False,
                } if k else None,
                "indicators": indicators,
            }

    def set_indicators(self, ind: dict):
        """策略运行后更新指标"""
        with self._lock:
            self.latest_indicators = ind

    def save_snapshot(self, path: str):
        """写入价格快照到文件"""
        snap = self.get_snapshot()
        try:
            with open(path, "w") as f:
                json.dump(snap, f)
        except:
            pass

    def wait_kline_closed(self, timeout: float = 900) -> Optional[KlineBar]:
        """阻塞等待下一根K线闭合"""
        if self.kline_closed_event.wait(timeout):
            with self._lock:
                k = self.closed_kline
                self.kline_closed_event.clear()
                return k
        return None


class BinanceWebSocket:
    """Binance WebSocket 客户端 — 多流合并"""

    STREAM_URL = "wss://stream.binance.com:9443/stream?streams=btcusdt@trade/btcusdt@kline_15m"

    def __init__(self, state: SharedMarketState):
        self.state = state
        self.ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False
        self._reconnect_count = 0
        self._max_reconnect = 50

    # ========== 公开 API ==========

    def start(self):
        """在后台线程启动 WebSocket"""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self.ws:
            self.ws.close()

    @property
    def connected(self) -> bool:
        return self._connected

    # ========== 内部 ==========

    def _run(self):
        while self._running and self._reconnect_count < self._max_reconnect:
            try:
                self.ws = websocket.WebSocketApp(
                    self.STREAM_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.error(f"WebSocket 异常: {e}")

            if self._running:
                self._reconnect_count += 1
                wait = min(2 ** self._reconnect_count, 60)
                logger.warning(f"WebSocket 断开, {wait}s 后重连 (第{self._reconnect_count}次)")
                time.sleep(wait)

        logger.info("WebSocket 线程退出")

    def _subscribe(self):
        """无需单独订阅 — URL 里已指定流"""
        pass

    def _on_open(self, ws):
        self._connected = True
        self._reconnect_count = 0
        logger.info("🔗 WebSocket 已连接 (ticker + kline_15m)")

    def _on_message(self, ws, message: str):
        try:
            data = json.loads(message)
            stream = data.get('stream', '')
            payload = data.get('data', data)

            if 'trade' in stream:
                if 'p' in payload:
                    self.state.update_price(float(payload['p']))

            elif 'kline' in stream:
                k = payload.get('k', payload)
                if 't' in k:
                    self.state.update_kline(k)

        except json.JSONDecodeError:
            pass
        except Exception as e:
            logger.debug(f"消息处理异常: {e}")

    def _on_error(self, ws, error):
        logger.error(f"WebSocket 错误: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        self._connected = False
        logger.warning(f"WebSocket 断开: {close_status_code} {close_msg}")


# ===== 便捷工厂 =====

def create_price_stream() -> tuple[SharedMarketState, BinanceWebSocket]:
    """创建价格流 → 返回 (state, ws_client)"""
    state = SharedMarketState()
    ws = BinanceWebSocket(state)
    return state, ws


if __name__ == '__main__':
    # 简单测试
    logging.basicConfig(level=logging.INFO)
    state, ws = create_price_stream()
    ws.start()

    try:
        for i in range(20):
            time.sleep(3)
            k = state.get_kline()
            p = state.get_price()
            status = "🟢" if ws.connected else "🔴"
            kline_str = f"K:{k.open:.0f}→{k.close:.0f}" if k else "K:waiting"
            if k and k.is_closed:
                kline_str += " ✅CLOSED"
            print(f"{status} ${p:,.0f} | {kline_str}")
    finally:
        ws.stop()