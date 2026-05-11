#!/usr/bin/env python3
"""
TradingMemory — 交易长期记忆系统

对标推文⑦"Hermes开始拥有长期记忆":
  - 哪个标的拖累最大 → 降权
  - 多单/空单谁更弱 → 降频
  - 最常见的失败类型
  - 自进化趋势

设计:
  1. 用 JSON 文件持久化 (轻量, 零依赖)
  2. 每次交易结束 (平仓) 更新
  3. 开仓前查询, 影响仓位和频率
  4. 带衰减: 旧交易的权重随时间降低

用法:
  memory = TradingMemory()
  memory.record_trade(symbol="BTC-USDT", side="long", pnl_pct=-5.2,
                      exit_reason="stop_loss", leverage=10)
  bias = memory.get_trading_bias("BTC-USDT")
  # -> {"weight_adjust": 0.8, "side_frequency": {"long": 0.4, "short": 0.6}, ...}
"""

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PROJECT = Path(__file__).resolve().parent.parent
MEMORY_FILE = PROJECT / "data" / "trading_memory.json"

# ── 记忆衰减配置 ─────────────────────────────────────────────────────────
# 新交易权重 = 1.0, 每天衰减 5%
MEMORY_DECAY_PER_DAY = 0.05
# 记忆有效时长 (天)
MEMORY_TTL_DAYS = 30
# 最多保留的交易记录
MAX_TRADES = 200


# ── 记忆条目 ─────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    """单笔交易记录"""
    symbol: str
    side: str              # long | short
    pnl_pct: float         # 盈亏百分比 (杠杆回报)
    exit_reason: str       # stop_loss | take_profit | max_hold | signal | ai_intervention
    leverage: int = 10
    timestamp: float = 0.0

    @property
    def age_days(self) -> float:
        if self.timestamp <= 0:
            return 0
        return (time.time() - self.timestamp) / 86400

    @property
    def weight(self) -> float:
        """衰减权重: 越新的交易权重越高"""
        days = self.age_days
        if days <= 0:
            return 1.0
        decay = 1.0 - min(days * MEMORY_DECAY_PER_DAY, 0.95)
        return max(decay, 0.05)

    @property
    def is_win(self) -> bool:
        return self.pnl_pct > 0

    @property
    def is_loss(self) -> bool:
        return self.pnl_pct <= 0


@dataclass
class MemorySummary:
    """记忆摘要 — 供开仓决策参考"""
    # 标的维度
    symbol: str
    total_trades: int = 0
    win_rate: float = 0.0
    avg_pnl: float = 0.0
    total_pnl: float = 0.0
    weight_adjust: float = 1.0  # 仓位调整系数 (0.0~1.0)

    # 方向维度
    long_trades: int = 0
    long_win_rate: float = 0.0
    long_avg_pnl: float = 0.0
    short_trades: int = 0
    short_win_rate: float = 0.0
    short_avg_pnl: float = 0.0

    # 失败模式
    top_failure_type: str = "none"  # stop_loss | reversal | trend_change | overstay
    failure_rate: float = 0.0

    # 推荐
    recommended_side: str = "neutral"  # long | short | neutral
    long_frequency: float = 0.5      # 做多频率 (0.0~1.0)
    short_frequency: float = 0.5     # 做空频率

    def to_dict(self) -> dict:
        return asdict(self)

    def to_ai_context(self) -> str:
        """生成供 AI 决策使用的记忆上下文文本"""
        lines = [f"Trading Memory for {self.symbol}:"]

        # 总览
        if self.total_trades > 0:
            lines.append(f"  Total: {self.total_trades} trades | "
                         f"Win rate: {self.win_rate:.0%} | "
                         f"Avg PnL: {self.avg_pnl:+.1f}%")
            lines.append(f"  Total PnL: {self.total_pnl:+.1f}% | "
                         f"Weight adjust: {self.weight_adjust:.0%}")

        # 方向分析
        if self.long_trades > 0:
            lines.append(f"  LONG: {self.long_trades} trades | "
                         f"WR={self.long_win_rate:.0%} | "
                         f"avg={self.long_avg_pnl:+.1f}%")
        if self.short_trades > 0:
            lines.append(f"  SHORT: {self.short_trades} trades | "
                         f"WR={self.short_win_rate:.0%} | "
                         f"avg={self.short_avg_pnl:+.1f}%")

        # 失败模式
        if self.failure_rate > 0.3:
            lines.append(f"  ⚠️ Top failure: {self.top_failure_type} "
                         f"(rate={self.failure_rate:.0%})")

        # 推荐
        if self.recommended_side != "neutral":
            pref_emoji = "🟢" if self.recommended_side == "long" else "🔴"
            lines.append(f"  {pref_emoji} Preference: {self.recommended_side.upper()} "
                         f"(LONG freq={self.long_frequency:.0%}, "
                         f"SHORT freq={self.short_frequency:.0%})")

        return "\n".join(lines)


# ── 记忆状态 (持久化的方向级归因) ─────────────────────────────────────────

@dataclass
class MemoryState:
    """持久化的记忆状态 — 推文⑦的核心结构"""
    top_drag_symbol: str = ""          # 最近哪个标的亏最多
    weak_side: str = "neutral"         # long | short | neutral
    common_failure: str = ""           # 最常见失败类型
    evo_trend: str = "stable"          # improving | declining | stable
    last_updated: float = 0.0
    recent_drawdown: float = 0.0       # 近期回撤幅度

    def to_dict(self) -> dict:
        result = asdict(self)
        if self.last_updated > 0:
            result["last_updated_iso"] = datetime.fromtimestamp(
                self.last_updated
            ).isoformat()
        return result


# ── Main Memory System ──────────────────────────────────────────────────────

class TradingMemory:
    """交易长期记忆系统 — 对标推文⑦"""

    def __init__(self, memory_path: str = None):
        self.memory_path = Path(memory_path) if memory_path else MEMORY_FILE
        self._trades: List[TradeRecord] = []
        self._state: MemoryState = MemoryState()
        self._load()

    # ── 读写 ─────────────────────────────────────────────────────────────

    def _load(self):
        """从文件加载记忆"""
        if not self.memory_path.exists():
            self._trades = []
            self._state = MemoryState()
            return

        try:
            data = json.loads(self.memory_path.read_text())
            self._trades = [TradeRecord(**t) for t in data.get("trades", [])]
            state_data = data.get("state", {})
            self._state = MemoryState(**state_data)
        except Exception as exc:
            logger.warning(f"Failed to load trading memory: {exc}")
            self._trades = []
            self._state = MemoryState()

    def _save(self):
        """持久化记忆"""
        # 清理过期交易
        cutoff = time.time() - MEMORY_TTL_DAYS * 86400
        self._trades = [t for t in self._trades if t.timestamp > cutoff]

        # 限制数量
        if len(self._trades) > MAX_TRADES:
            self._trades = self._trades[-MAX_TRADES:]

        try:
            self.memory_path.parent.mkdir(parents=True, exist_ok=True)
            self.memory_path.write_text(json.dumps({
                "trades": [asdict(t) for t in self._trades],
                "state": asdict(self._state),
            }, ensure_ascii=False, indent=2))
        except Exception as exc:
            logger.warning(f"Failed to save trading memory: {exc}")

    def _update_state(self):
        """更新全局记忆状态 (推文⑦的记忆归因)"""
        if not self._trades:
            self._state = MemoryState()
            return

        # 标的维度: 找出总亏损最多的标的
        symbol_pnl: Dict[str, float] = {}
        for t in self._trades:
            if t.timestamp > time.time() - 7 * 86400:  # 近7天
                symbol_pnl[t.symbol] = symbol_pnl.get(t.symbol, 0) + t.pnl_pct

        if symbol_pnl:
            worst_symbol = min(symbol_pnl, key=symbol_pnl.get)
            self._state.top_drag_symbol = worst_symbol
        else:
            self._state.top_drag_symbol = ""

        # 方向维度: 多空对比
        recent = [t for t in self._trades if t.timestamp > time.time() - 7 * 86400]
        long_trades = [t for t in recent if t.side == "long"]
        short_trades = [t for t in recent if t.side == "short"]

        long_avg = sum(t.pnl_pct for t in long_trades) / max(len(long_trades), 1)
        short_avg = sum(t.pnl_pct for t in short_trades) / max(len(short_trades), 1)

        if long_avg < short_avg - 2:
            self._state.weak_side = "long"
        elif short_avg < long_avg - 2:
            self._state.weak_side = "short"
        else:
            self._state.weak_side = "neutral"

        # 失败类型
        loss_trades = [t for t in recent if not t.is_win]
        if loss_trades:
            failures: Dict[str, int] = {}
            for t in loss_trades:
                failures[t.exit_reason] = failures.get(t.exit_reason, 0) + 1
            top_fail = max(failures, key=failures.get)
            self._state.common_failure = top_fail

        # 自进化趋势: 对比近7天和7-14天的胜率
        older = [t for t in self._trades
                 if time.time() - 14 * 86440 < t.timestamp < time.time() - 7 * 86400]
        recent_wr = sum(1 for t in recent if t.is_win) / max(len(recent), 1)
        older_wr = sum(1 for t in older if t.is_win) / max(len(older), 1)

        if recent_wr > older_wr + 0.1:
            self._state.evo_trend = "improving"
        elif recent_wr < older_wr - 0.1:
            self._state.evo_trend = "declining"
        else:
            self._state.evo_trend = "stable"

        # 近期回撤
        if recent:
            recent_pnl = sum(t.pnl_pct for t in recent)
            if recent_pnl < 0:
                self._state.recent_drawdown = abs(recent_pnl)
            else:
                self._state.recent_drawdown = 0.0

        self._state.last_updated = time.time()

    # ── 公开接口 ─────────────────────────────────────────────────────────

    def record_trade(self, symbol: str, side: str, pnl_pct: float,
                     exit_reason: str = "signal", leverage: int = 10):
        """记录一笔完成的交易 (平仓时调用)

        对标推文⑦: 交易结束后更新记忆, 记住标的、方向、失败类型
        """
        trade = TradeRecord(
            symbol=symbol,
            side=side,
            pnl_pct=round(pnl_pct, 2),
            exit_reason=exit_reason,
            leverage=leverage,
            timestamp=time.time(),
        )
        self._trades.append(trade)
        self._update_state()
        self._save()

        logger.info(f"📝 记忆: {symbol} {side} PnL={pnl_pct:+.1f}% "
                    f"reason={exit_reason} | "
                    f"weak_side={self._state.weak_side} "
                    f"drag={self._state.top_drag_symbol}")

    def get_memory_state(self) -> MemoryState:
        """获取全局记忆状态 (供AI/prompt展示)"""
        return self._state

    def get_memory_summary(self, symbol: str = "BTC-USDT") -> MemorySummary:
        """获取某个标的的记忆摘要 (供开仓决策)

        Parameters
        ----------
        symbol : str
            标的

        Returns
        -------
        MemorySummary with:
            weight_adjust: 仓位调整 (0.0~1.0), 亏多了降仓
            long/short_frequency: 方向频率推荐
            recommended_side: 推荐方向
        """
        # 该标的所有交易 (不限时间, 但老的权重低)
        trades = [t for t in self._trades if t.symbol == symbol]
        if not trades:
            return MemorySummary(symbol=symbol)

        total = len(trades)
        wins = sum(1 for t in trades if t.is_win)
        losses = total - wins
        weighted_pnl = sum(t.pnl_pct * t.weight for t in trades)
        total_weighted = sum(t.weight for t in trades)
        avg_pnl = weighted_pnl / max(total_weighted, 1)

        # 胜率 (加权)
        weighted_wins = sum(t.weight for t in trades if t.is_win)
        win_rate = weighted_wins / max(total_weighted, 1)

        # 方向分析
        long_t = [t for t in trades if t.side == "long"]
        short_t = [t for t in trades if t.side == "short"]

        def _side_stats(side_trades) -> Tuple[int, float, float]:
            if not side_trades:
                return 0, 0.0, 0.0
            sw = sum(t.weight for t in side_trades)
            s_wins = sum(t.weight for t in side_trades if t.is_win)
            s_wr = s_wins / max(sw, 1)
            s_avg = sum(t.pnl_pct * t.weight for t in side_trades) / max(sw, 1)
            return len(side_trades), s_wr, s_avg

        long_count, long_wr, long_avg = _side_stats(long_t)
        short_count, short_wr, short_avg = _side_stats(short_t)

        # 仓位调整: 基于总体盈亏
        if total_weighted > 0:
            # avg_pnl 是加权平均盈亏
            if avg_pnl < -10:
                weight_adjust = 0.5  # 大幅亏损, 仓位减半
            elif avg_pnl < -5:
                weight_adjust = 0.7
            elif avg_pnl < -2:
                weight_adjust = 0.85
            elif avg_pnl < 0:
                weight_adjust = 0.95
            elif avg_pnl > 10:
                weight_adjust = 1.2  # 盈利不错, 适当加仓
            elif avg_pnl > 5:
                weight_adjust = 1.1
            else:
                weight_adjust = 1.0
        else:
            weight_adjust = 1.0

        # 降权: 如果自己是拖累标的
        if self._state.top_drag_symbol == symbol:
            weight_adjust *= 0.75

        weight_adjust = max(0.3, min(2.0, weight_adjust))

        # 推荐方向和频率
        if long_count > 0 and short_count > 0:
            if long_wr > short_wr + 0.15:
                recommended_side = "long"
            elif short_wr > long_wr + 0.15:
                recommended_side = "short"
            else:
                recommended_side = "neutral"

            # 弱侧降频
            if self._state.weak_side == "long" and long_count > 0:
                long_freq = 0.3
                short_freq = 0.7
            elif self._state.weak_side == "short" and short_count > 0:
                long_freq = 0.7
                short_freq = 0.3
            else:
                total_dir = long_count + short_count
                long_freq = long_count / max(total_dir, 1)
                short_freq = short_count / max(total_dir, 1)
        elif long_count > 0:
            recommended_side = "long"
            long_freq = 0.8
            short_freq = 0.2
        elif short_count > 0:
            recommended_side = "short"
            long_freq = 0.2
            short_freq = 0.8
        else:
            recommended_side = "neutral"
            long_freq = 0.5
            short_freq = 0.5

        # 失败模式分析
        loss_exits: Dict[str, float] = {}
        for t in trades:
            if not t.is_win:
                loss_exits[t.exit_reason] = loss_exits.get(t.exit_reason, 0) + t.weight
        top_failure = "none"
        failure_rate = 0.0
        if loss_exits and total_weighted > 0:
            top_failure = max(loss_exits, key=loss_exits.get)
            failure_rate = sum(loss_exits.values()) / max(total_weighted, 1)

        return MemorySummary(
            symbol=symbol,
            total_trades=total,
            win_rate=round(win_rate, 3),
            avg_pnl=round(avg_pnl, 2),
            total_pnl=round(sum(t.pnl_pct for t in trades), 2),
            weight_adjust=round(weight_adjust, 2),
            long_trades=long_count,
            long_win_rate=round(long_wr, 3),
            long_avg_pnl=round(long_avg, 2),
            short_trades=short_count,
            short_win_rate=round(short_wr, 3),
            short_avg_pnl=round(short_avg, 2),
            top_failure_type=top_failure,
            failure_rate=round(failure_rate, 3),
            recommended_side=recommended_side,
            long_frequency=round(long_freq, 2),
            short_frequency=round(short_freq, 2),
        )

    def get_state_for_ai(self) -> str:
        """生成AI决策用的记忆上下文 (推文⑦的记忆→开仓反馈)"""
        state = self._state
        lines = ["## Trading Memory (Long-term)"]
        if state.top_drag_symbol:
            lines.append(f"  Drag symbol: {state.top_drag_symbol} "
                         f"(reduce position if trading this)")
        if state.weak_side != "neutral":
            lines.append(f"  Weak side: {state.weak_side.upper()} "
                         f"(reduce frequency for {state.weak_side} entries)")
        if state.common_failure:
            lines.append(f"  Common failure: {state.common_failure}")
        trend_emoji = {"improving": "🟢", "declining": "🔴", "stable": "⚪"}
        lines.append(f"  {trend_emoji.get(state.evo_trend, '⚪')} "
                     f"Evo trend: {state.evo_trend}")
        if state.recent_drawdown > 5:
            lines.append(f"  ⚠️ Recent drawdown: {state.recent_drawdown:.0f}% — "
                         f"reduce risk")
        return "\n".join(lines)

    def clear(self):
        """清空所有记忆 (用于重置)"""
        self._trades = []
        self._state = MemoryState()
        self._save()
        logger.info("🧹 Trading memory cleared")

    def get_statistics(self) -> dict:
        """获取统计"""
        total = len(self._trades)
        wins = sum(1 for t in self._trades if t.is_win)
        return {
            "total_trades": total,
            "win_rate": f"{wins/max(total,1):.0%}",
            "recent_count": sum(1 for t in self._trades
                                if t.timestamp > time.time() - 7*86400),
            "state": self._state.to_dict(),
        }


# ── 独立测试 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s: %(message)s")

    memory = TradingMemory()

    if "--clear" in sys.argv:
        memory.clear()
        print("Cleared. Statistics:", memory.get_statistics())
        sys.exit(0)

    if "--demo" in sys.argv:
        # 模拟一些交易
        import random
        test_trades = [
            ("BTC-USDT", "long", -5.2, "stop_loss"),
            ("BTC-USDT", "long", 3.1, "take_profit"),
            ("BTC-USDT", "short", -2.8, "stop_loss"),
            ("BTC-USDT", "short", 8.5, "take_profit"),
            ("BTC-USDT", "long", -12.0, "stop_loss"),
            ("ETH-USDT", "long", -8.3, "stop_loss"),
            ("BTC-USDT", "short", 4.2, "signal"),
            ("BTC-USDT", "long", -6.1, "stop_loss"),
            ("BTC-USDT", "short", 2.0, "take_profit"),
            ("BTC-USDT", "long", 5.5, "take_profit"),
        ]
        for sym, side, pnl, reason in test_trades:
            memory.record_trade(sym, side, pnl, reason)

        print("Memory state:")
        state = memory.get_memory_state()
        print(f"  Top drag: {state.top_drag_symbol}")
        print(f"  Weak side: {state.weak_side}")
        print(f"  Common failure: {state.common_failure}")
        print(f"  Evo trend: {state.evo_trend}")
        print()

        print("Memory summary for BTC-USDT:")
        summary = memory.get_memory_summary("BTC-USDT")
        for k, v in summary.to_dict().items():
            print(f"  {k}: {v}")
        print()

        print("AI context:")
        print(memory.get_state_for_ai())
        sys.exit(0)

    # 显示现有记忆
    stats = memory.get_statistics()
    print(f"Total trades: {stats['total_trades']}")
    print(f"Win rate: {stats['win_rate']}")
    print(f"Recent count: {stats['recent_count']}")
    print(f"State: {stats.get('state', {})}")

    if memory._trades:
        print(f"\nBTC-USDT summary:")
        summary = memory.get_memory_summary("BTC-USDT")
        print(summary.to_ai_context())