#!/usr/bin/env python3
"""
PolymarketScanner — Polymarket 预测市场扫描与评分

对标推文的 Polymarket 三层处理:
  Layer 1: 多维度匹配 (ticker + 别名 + JSON + 标题/描述/slug)
  Layer 2: 概率转分数 → 加权聚合 → 截断 [-0.6, +0.6]
  Layer 3: 自学习别名 (匹配成功 → 自动写回 aliases.json)

集成方式:
  scanner = PolymarketScanner(symbol="BTC")
  result = scanner.aggregate_for_trading()
  # -> {pm_score, pm_label, pm_confidence, pm_details, matched_markets}

用法 (独立测试):
  python3 data/polymarket_scanner.py            # BTC 默认
  python3 data/polymarket_scanner.py ETH        # ETH
  python3 data/polymarket_scanner.py --refresh  # 强制刷新缓存
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
PROJECT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT / "data"
ALIASES_FILE = DATA_DIR / "polymarket_aliases.json"
CACHE_FILE = DATA_DIR / "pm_cache.json"

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
REQUEST_TIMEOUT = 12
USER_AGENT = "Hermes-Agent/1.0"

# ── 映射表: ticker → Polymarket 搜索关键词 ─────────────────────────────
# 内置别名 (第一层匹配)
BUILTIN_ALIASES = {
    "BTC":   ["bitcoin", "btc price", "bitcoin above"],
    "ETH":   ["ethereum", "eth price", "ethereum above"],
    "SOL":   ["solana", "sol price"],
    "BNB":   ["binance coin", "bnb"],
    "XRP":   ["xrp", "ripple"],
    "DOGE":  ["dogecoin", "doge"],
    "ADA":   ["cardano", "ada"],
    "AVAX":  ["avalanche", "avax"],
    "DOT":   ["polkadot", "dot"],
    "LINK":  ["chainlink", "link"],
    "MATIC": ["polygon", "matic"],
    "AAVE":  ["aave"],
    "UNI":   ["uniswap", "uni"],
}

# ── 推文的阈值配置 ─────────────────────────────────────────────────────
POLYMARKET_POSITIVE_THRESHOLD = 0.08   # score >= 0.08 = bullish
POLYMARKET_NEGATIVE_THRESHOLD = -0.08  # score <= -0.08 = bearish
POLYMARKET_SCORE_BONUS = 7            # 加分权重 (供外部参考)
POLYMARKET_MARKET_LIMIT = 250         # 最大扫描市场数

# ── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class MatchedMarket:
    """单个匹配成功的 Polymarket 市场"""
    question: str
    slug: str
    yes_price: float          # 0.0~1.0
    no_price: float
    direction_score: float    # -1.0 ~ +1.0 (正=看多, 负=看空)
    volume_24h: float
    liquidity: float
    weight: float             # 聚合权重 (根据成交量和流动性)
    match_source: str         # ticker | alias | title | slug | description

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PolymarketAggregate:
    """聚合后的 Polymarket 信号"""
    pm_score: float            # -0.6 ~ +0.6
    pm_label: str              # bullish | bearish | neutral
    pm_confidence: float       # 0.0~1.0
    pm_details: str            # 匹配市场摘要
    matched_markets: List[MatchedMarket] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pm_score": self.pm_score,
            "pm_label": self.pm_label,
            "pm_confidence": self.pm_confidence,
            "pm_details": self.pm_details,
            "matched_markets": [m.to_dict() for m in self.matched_markets],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ── HTTP Helper ─────────────────────────────────────────────────────────────

def _get(url: str) -> Optional[Any]:
    """GET 请求, 解析 JSON"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
            json.JSONDecodeError, OSError) as exc:
        logger.debug(f"Polymarket API error: {exc} ({url[:100]})")
        return None


def _parse_json_field(val):
    """解析双重编码 JSON 字段"""
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    return val


# ── Layer 1: 别名管理 ─────────────────────────────────────────────────────

def _load_aliases() -> Dict[str, List[str]]:
    """加载 polymarket_aliases.json (推文的自学习别名)"""
    aliases = {}
    if ALIASES_FILE.exists():
        try:
            aliases = json.loads(ALIASES_FILE.read_text())
        except Exception as exc:
            logger.warning(f"Failed to load aliases: {exc}")
    return aliases


def _save_alias(symbol: str, new_alias: str):
    """自学习: 将新别名写回 aliases.json (推文 Layer 3)"""
    aliases = _load_aliases()
    if symbol not in aliases:
        aliases[symbol] = []
    if new_alias not in aliases[symbol]:
        aliases[symbol].append(new_alias)
        try:
            ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
            ALIASES_FILE.write_text(json.dumps(aliases, ensure_ascii=False, indent=2))
            logger.info(f"📝 自学习别名: {symbol} → '{new_alias}'")
        except Exception as exc:
            logger.warning(f"Failed to save alias: {exc}")


def _get_search_terms(symbol: str) -> List[str]:
    """获取某标的的所有搜索词 (内置 + 自学习)"""
    terms = BUILTIN_ALIASES.get(symbol.upper(), [symbol.lower()])
    # 自学习别名
    aliases = _load_aliases()
    learned = aliases.get(symbol.upper(), [])
    if learned:
        terms = list(dict.fromkeys(terms + learned))  # 去重保留顺序
    # 总把符号本身作为保底
    if symbol.lower() not in terms:
        terms.append(symbol.lower())
    return terms


# ── Layer 1: 匹配逻辑 ─────────────────────────────────────────────────────

def _extract_project_from_slug(slug: str) -> Optional[str]:
    """从 slug 保守提取项目名 (推文 Layer 3 的自学习)"""
    # 模式: "bitcoin-above-100k-aug-2025" → "bitcoin"
    # 模式: "eth-blasts-past-4k" → "eth"
    # 模式: "will-solana-reach-300" → "solana"
    # 只匹配简单的前缀词
    known_projects = {
        "bitcoin": "BTC", "btc": "BTC",
        "ethereum": "ETH", "eth": "ETH",
        "solana": "SOL", "sol": "SOL",
        "binance": "BNB", "bnb": "BNB",
        "ripple": "XRP", "xrp": "XRP",
        "dogecoin": "DOGE", "doge": "DOGE",
        "cardano": "ADA", "ada": "ADA",
        "avalanche": "AVAX", "avax": "AVAX",
        "polkadot": "DOT", "dot": "DOT",
        "chainlink": "LINK", "link": "LINK",
        "polygon": "MATIC", "matic": "MATIC",
        "aave": "AAVE",
        "uniswap": "UNI", "uni": "UNI",
    }
    slug_lower = slug.lower().strip()
    # 取第一个分段
    first_part = slug_lower.split("-")[0] if "-" in slug_lower else slug_lower
    for alias_key, ticker in known_projects.items():
        if alias_key == first_part or alias_key in slug_lower:
            return ticker
    return None


def _is_bullish_question(question: str, slug: str) -> bool:
    """判断市场问题方向: 是看多还是看空问题

    推文逻辑: 围绕 0.5 映射到 [-1, 1], 再按问题方向决定正负。
    如果问题是 "Will BTC be above $100k?" → 价格越高(Yes高)越看多, 方向为正
    如果问题是 "Will BTC crash below $50k?" → 价格越低(No高)越看多, 方向为负
    """
    text = (question + " " + slug).lower()

    # 看空关键词 — 这些词表示"坏消息"类问题
    bearish_indicators = [
        "crash", "dump", "below", "drop", "decline", "fall", "breakdown",
        "recession", "bear", "correction", "lower", "minimum", "worst",
        "lose", "decrease", "reject", "halt", "ban", "restrict",
    ]
    # 看多关键词 — 表示"好消息"类问题
    bullish_indicators = [
        "above", "reach", "新高", "ath", "all-time high", "rally",
        "surge", "breakout", "recovery", "gain", "increase", "rise",
        "approve", "adopt", "win", "success", "新高",
    ]

    # 问题方向判断: 如果包含看空词, 说明这是个"坏消息发生"的问题
    # 如果包含看多词, 说明这是个"好消息发生"的问题
    has_bearish = any(kw in text for kw in bearish_indicators)
    has_bullish = any(kw in text for kw in bullish_indicators)

    if has_bearish and not has_bullish:
        # "Will BTC crash?" → Yes=坏消息发生=看空 → 反转
        return False
    elif has_bullish and not has_bearish:
        # "Will BTC reach $150k?" → Yes=好消息=看多 → 正向
        return True

    # 中性或混合 → 默认正向 (Yes=看多)
    return True


def _match_market_to_symbol(symbol: str, search_terms: List[str],
                            market: dict) -> Optional[float]:
    """多维度匹配一个市场到标的 (推文 Layer 1)

    Returns
    -------
    float or None
        匹配强度分数 0.0~1.0, None=不匹配
    """
    question = (market.get("question", "") or "").lower()
    slug = (market.get("slug", "") or "").lower()
    description = (market.get("description", "") or "").lower()
    ticker_lower = symbol.lower()

    score = 0.0

    # ① ticker 精确匹配 (最高权重)
    if ticker_lower in slug.split("-") or ticker_lower == slug:
        score = 1.0
    elif ticker_lower in question:
        score = 0.9

    # ② 别名匹配 (内置 + 自学习)
    if score < 1.0:
        for term in search_terms:
            term_lower = term.lower()
            if term_lower in slug:
                score = max(score, 0.95)
            elif term_lower in question:
                score = max(score, 0.85)
            elif term_lower in description:
                score = max(score, 0.70)

    # ③ 最低匹配阈值
    return score if score >= 0.5 else None


def _compute_direction_score(yes_price: float, is_bullish_question: bool) -> float:
    """把概率转成方向分数 (推文 Layer 2)

    围绕 0.5 映射到 [-1, 1], 再按问题方向决定正负。
    - 看多问题: Yes高=正, Yes低=负
    - 看空问题: Yes高=负, Yes低=正

    极端概率(>0.95或<0.05)会返回0让权重处理, 避免过高确定性market主导。
    """
    # 极端概率: 过于确定的市场, 信息熵低, 不贡献方向判断
    if yes_price < 0.02 or yes_price > 0.98:
        return 0.0

    # 基础分数: 偏离 0.5 的程度 [-0.5, +0.5]
    base = yes_price - 0.5

    if is_bullish_question:
        return base * 2  # [-1.0, +1.0]
    else:
        return -base * 2  # [-1.0, +1.0]


def _compute_weight(volume_24h: float, liquidity: float) -> float:
    """按成交量和流动性计算权重 (推文 Layer 2)"""
    # 成交量权重: 0~500K = 0~0.5, 500K~5M = 0.5~1.0
    vol_weight = min(volume_24h / 5_000_000, 1.0)
    # 流动性权重: 类似逻辑
    liq_weight = min(liquidity / 2_000_000, 1.0)
    return (vol_weight * 0.6 + liq_weight * 0.4)


# ── 缓存 ────────────────────────────────────────────────────────────────────

def _load_cache(ttl: int = 600) -> Optional[dict]:
    """加载缓存 (默认10分钟有效)"""
    try:
        if CACHE_FILE.exists():
            data = json.loads(CACHE_FILE.read_text())
            if data.get("timestamp", 0) > time.time() - ttl:
                return data
    except Exception:
        pass
    return None


def _save_cache(result: dict):
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        logger.debug(f"PM cache save failed: {exc}")


# ── Main Scanner ────────────────────────────────────────────────────────────

class PolymarketScanner:
    """Polymarket 预测市场扫描器 — 推文三层处理"""

    def __init__(self, symbol: str = "BTC", search_limit: int = 50):
        self.symbol = symbol.upper()
        self.search_terms = _get_search_terms(self.symbol)
        self.search_limit = search_limit

    def scan(self, force_refresh: bool = False) -> PolymarketAggregate:
        """执行完整扫描流程: 搜索→匹配→评分→聚合

        Parameters
        ----------
        force_refresh : bool
            是否强制刷新缓存

        Returns
        -------
        PolymarketAggregate
        """
        # 检查缓存
        if not force_refresh:
            cached = _load_cache()
            if cached and cached.get("symbol") == self.symbol:
                markets = [MatchedMarket(**m) for m in cached.get("matched_markets", [])]
                return PolymarketAggregate(
                    pm_score=cached["pm_score"],
                    pm_label=cached["pm_label"],
                    pm_confidence=cached["pm_confidence"],
                    pm_details=cached.get("pm_details", ""),
                    matched_markets=markets,
                )

        # ── Layer 1: 多维度匹配 ──
        matched_markets: List[MatchedMarket] = []
        seen_slugs = set()

        # 策略1: 用 ticker 直接搜索
        for term in self.search_terms[:3]:  # 前3个搜索词
            try:
                q = urllib.parse.quote(term)
                data = _get(f"{GAMMA}/public-search?q={q}&limit={self.search_limit}")
                if not data:
                    continue
                events = data.get("events", [])
                for evt in events:
                    # 只处理 active 事件
                    if evt.get("closed", False):
                        continue
                    markets = evt.get("markets", [])
                    for m in markets:
                        slug = m.get("slug", "")
                        if slug in seen_slugs:
                            continue
                        match_score = _match_market_to_symbol(
                            self.symbol, self.search_terms, m
                        )
                        if not match_score:
                            continue

                        seen_slugs.add(slug)

                        # 解析 market 关键数据
                        prices = _parse_json_field(m.get("outcomePrices", "[]"))
                        if not isinstance(prices, list) or len(prices) < 2:
                            continue
                        try:
                            yes_price = float(prices[0])
                            no_price = float(prices[1])
                        except (ValueError, TypeError):
                            continue

                        question = m.get("question", "")
                        volume = float(m.get("volume", 0) or 0)
                        liquidity = float(m.get("liquidity", 0) or 0)

                        # 判断问题方向
                        is_bullish_q = _is_bullish_question(question, slug)

                        # Layer 2: 方向分数
                        dir_score = _compute_direction_score(yes_price, is_bullish_q)

                        # 权重
                        weight = _compute_weight(volume, liquidity)

                        # 确定匹配来源
                        ticker_lower = self.symbol.lower()
                        slug_lower = slug.lower()
                        question_lower = question.lower()
                        if ticker_lower in slug_lower.split("-"):
                            m_source = "ticker"
                        elif any(t in slug_lower or t in question_lower for t in self.search_terms):
                            m_source = "alias"
                        else:
                            m_source = "title"

                        market_data = MatchedMarket(
                            question=question[:120],
                            slug=slug,
                            yes_price=round(yes_price, 4),
                            no_price=round(no_price, 4),
                            direction_score=round(dir_score, 3),
                            volume_24h=volume,
                            liquidity=liquidity,
                            weight=round(weight, 3),
                            match_source=m_source,
                        )
                        matched_markets.append(market_data)

                        # Layer 3: 自学习 (匹配成功→提取项目名→写回aliases)
                        if m_source in ("title", "slug"):
                            project = _extract_project_from_slug(slug)
                            if project and project == self.symbol:
                                # 提取slug中有区分度的部分作为别名
                                slug_parts = slug_lower.split("-")
                                for part in slug_parts:
                                    if part not in (ticker_lower, "will", "above", "below",
                                                    "reach", "price", "month", "hit",
                                                    "to", "or", "be", "a", "an", "the"):
                                        if len(part) > 3 and part not in self.search_terms:
                                            _save_alias(self.symbol, part)
                                            break
            except Exception as exc:
                logger.debug(f"PM search error for '{term}': {exc}")
                continue

        # 策略2: 如果匹配太少, 多搜一个更宽的关键词
        if len(matched_markets) < 3:
            broader = f"{self.symbol} crypto"
            try:
                q = urllib.parse.quote(broader)
                data = _get(f"{GAMMA}/public-search?q={q}&limit={self.search_limit}")
                if data:
                    events = data.get("events", [])
                    for evt in events[:20]:
                        if evt.get("closed", False):
                            continue
                        for m in evt.get("markets", []):
                            slug = m.get("slug", "")
                            if slug in seen_slugs:
                                continue
                            match_score = _match_market_to_symbol(
                                self.symbol, self.search_terms, m
                            )
                            if not match_score:
                                continue
                            seen_slugs.add(slug)
                            prices = _parse_json_field(m.get("outcomePrices", "[]"))
                            if not isinstance(prices, list) or len(prices) < 2:
                                continue
                            try:
                                yes_price = float(prices[0])
                                no_price = float(prices[1])
                            except (ValueError, TypeError):
                                continue
                            dir_score = _compute_direction_score(
                                yes_price,
                                _is_bullish_question(m.get("question", ""), slug),
                            )
                            volume = float(m.get("volume", 0) or 0)
                            liquidity = float(m.get("liquidity", 0) or 0)
                            weight = _compute_weight(volume, liquidity)
                            matched_markets.append(MatchedMarket(
                                question=(m.get("question", "") or "")[:120],
                                slug=slug,
                                yes_price=round(yes_price, 4),
                                no_price=round(no_price, 4),
                                direction_score=round(dir_score, 3),
                                volume_24h=volume,
                                liquidity=liquidity,
                                weight=round(weight, 3),
                                match_source="broader_search",
                            ))
            except Exception:
                pass

        # ── Layer 2: 加权聚合 ──
        if not matched_markets:
            result = PolymarketAggregate(
                pm_score=0.0, pm_label="neutral",
                pm_confidence=0.0,
                pm_details="未匹配到相关预测市场",
            )
            _save_cache(self._serialize_result(result))
            return result

        # 计算加权平均方向分数
        total_weight = sum(m.weight for m in matched_markets)
        if total_weight > 0:
            weighted_score = sum(m.direction_score * m.weight for m in matched_markets) / total_weight
        else:
            weighted_score = 0.0

        # 截断到 [-0.6, +0.6]
        pm_score = max(-0.6, min(0.6, weighted_score))

        # 标签
        if pm_score >= POLYMARKET_POSITIVE_THRESHOLD:
            pm_label = "bullish"
        elif pm_score <= POLYMARKET_NEGATIVE_THRESHOLD:
            pm_label = "bearish"
        else:
            pm_label = "neutral"

        # 置信度: 取决于匹配数量 + 权重分布
        market_count = len(matched_markets)
        weight_quality = min(total_weight / max(market_count, 1), 1.0)
        count_confidence = min(market_count / 10, 1.0)  # 10个市场=满置信
        confidence = round(min(weight_quality * 0.5 + count_confidence * 0.5, 1.0), 2)

        # 生成详情文本
        details_lines = []
        # 按权重排序, 展示前5个
        sorted_markets = sorted(matched_markets, key=lambda m: m.weight, reverse=True)
        for m in sorted_markets[:5]:
            emoji = "🟢" if m.direction_score > 0.1 else ("🔴" if m.direction_score < -0.1 else "⚪")
            vol_str = f"${m.volume_24h:,.0f}" if m.volume_24h > 0 else "N/A"
            details_lines.append(
                f"{emoji} {m.question[:60]} | {m.yes_price:.0%}Yes | "
                f"vol={vol_str} | w={m.weight:.2f}"
            )
        if len(sorted_markets) > 5:
            details_lines.append(f"  ... +{len(sorted_markets)-5} more markets")
        pm_details = "\n".join(details_lines)

        result = PolymarketAggregate(
            pm_score=round(pm_score, 4),
            pm_label=pm_label,
            pm_confidence=confidence,
            pm_details=pm_details,
            matched_markets=matched_markets,
        )

        # 写缓存
        _save_cache(self._serialize_result(result))

        logger.info(
            f"PM scan for {self.symbol}: score={pm_score:+.4f} ({pm_label}, "
            f"conf={confidence:.0%}) markets={len(matched_markets)}"
        )
        return result

    def _serialize_result(self, result: PolymarketAggregate) -> dict:
        """序列化用于缓存"""
        return {
            "symbol": self.symbol,
            "pm_score": result.pm_score,
            "pm_label": result.pm_label,
            "pm_confidence": result.pm_confidence,
            "pm_details": result.pm_details,
            "matched_markets": [m.to_dict() for m in result.matched_markets],
            "timestamp": time.time(),
        }

    def aggregate_for_trading(self, force_refresh: bool = False) -> dict:
        """输出供交易系统使用的聚合信号

        Returns
        -------
        dict with keys: pm_score, pm_label, pm_confidence, pm_details
        """
        result = self.scan(force_refresh=force_refresh)
        return {
            "pm_score": result.pm_score,
            "pm_label": result.pm_label,
            "pm_confidence": result.pm_confidence,
            "pm_details": result.pm_details,
        }


# ── 独立测试 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s: %(message)s")

    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTC"
    force = "--refresh" in sys.argv

    scanner = PolymarketScanner(symbol=symbol)
    result = scanner.scan(force_refresh=force)

    print("=" * 60)
    print(f"Polymarket Scan: {symbol}")
    print(f"Score: {result.pm_score:+.4f} ({result.pm_label})")
    print(f"Confidence: {result.pm_confidence:.0%}")
    print(f"Matched: {len(result.matched_markets)} markets")
    print()
    if result.matched_markets:
        print("Top matches:")
        for m in sorted(result.matched_markets, key=lambda x: x.weight, reverse=True)[:8]:
            emoji = "🟢" if m.direction_score > 0.1 else ("🔴" if m.direction_score < -0.1 else "⚪")
            print(f"  {emoji} [{m.match_source}] {m.question[:70]}")
            print(f"       Yes={m.yes_price:.0%} No={m.no_price:.0%} "
                  f"dir={m.direction_score:+.2f} vol=${m.volume_24h:,.0f} w={m.weight:.2f}")
    print()
    print("Details:")
    print(result.pm_details)
    print("=" * 60)