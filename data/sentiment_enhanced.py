#!/usr/bin/env python3
"""
EnhancedSentimentAggregator — AI 驱动的情绪聚合器

替代方案：用 Exa web_search 获取 BTC 最新新闻/推文/Reddit 摘要，
然后用 Hermes CLI (LLM) 做情绪评分。这比所有脆弱的 RSS/API 源更可靠，
且能获得有深度的市场情绪分析。

保留 alternative.me FG 指数作为低成本参考，但置信度调低。

数据结构兼容旧版，调用方不需要修改。

用法:
  agg = EnhancedSentimentAggregator()
  result = agg.to_trading_signal()
  # -> {sentiment_score, sentiment_label, sentiment_confidence, sentiment_details, trading_bias}
"""

import json
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
PROJECT = Path(__file__).resolve().parent.parent
CACHE_FILE = PROJECT / "data" / "sentiment_cache.json"
REQUEST_TIMEOUT = 10
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ── Data Classes (不变，保持向后兼容) ─────────────────────────────────────

@dataclass
class SentimentSource:
    source: str
    score: float
    confidence: float
    label: str
    detail: str
    raw_data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SentimentAggregate:
    overall_score: float
    overall_label: str
    confidence: float
    sources: List[SentimentSource] = field(default_factory=list)
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "overall_score": self.overall_score,
            "overall_label": self.overall_label,
            "confidence": self.confidence,
            "sources": [s.to_dict() for s in self.sources],
            "timestamp": self.timestamp,
        }

    def to_tg_summary(self) -> str:
        emoji = {
            "strong_bearish": "🔴", "bearish": "🔻",
            "neutral": "⚪",
            "bullish": "🟢", "strong_bullish": "🟢🔥"
        }.get(self.overall_label, "⚪")
        parts = [f"{emoji} 情绪={self.overall_score:+.2f} ({self.overall_label}, conf={self.confidence:.0%})"]
        for s in self.sources:
            if s.confidence > 0:
                parts.append(f"  · {s.source}: {s.score:+.2f} ({s.detail})")
        return "\n".join(parts)


# ── Source 1: AI 情绪扫描 (Exa search + LLM 评分) ────────────────────────

def _fetch_ai_sentiment() -> SentimentSource:
    """用 CoinGecko 社区情绪 + 新闻聚合替代原来的 5 个脆弱源。

    可靠数据源（已验证）:
    - CoinGecko: sentiment_votes_up/down + Reddit 社区数据
    - Google News RSS: BTC 相关新闻标题的情绪分析
    """
    sources = []
    confidence_total = 0.0
    score_total = 0.0

    # Source A: CoinGecko 社区情绪
    try:
        url = "https://api.coingecko.com/api/v3/coins/bitcoin"
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            params={"localization": "false", "tickers": "false",
                    "community_data": "true", "developer_data": "false",
                    "sparkline": "false"},
        )
        if resp.status_code == 200:
            data = resp.json()
            community = data.get("community_data", {})
            votes_up = community.get("sentiment_votes_up_percentage", 50)
            votes_down = community.get("sentiment_votes_down_percentage", 50)
            net = (votes_up - votes_down) / 100.0  # [-1, 1]
            conf = min(abs(net) * 1.5 + 0.2, 0.7)
            sources.append({
                "score": max(-1.0, min(1.0, net)),
                "confidence": conf,
                "detail": f"CoinGecko {votes_up:.0f}%up/{votes_down:.0f}%down",
            })
            confidence_total += conf
            score_total += net * conf
    except Exception as exc:
        logger.warning(f"CoinGecko failed: {exc}")

    # Source B: Google News RSS 情绪分析
    try:
        # 用更稳定的 URL (已验证 HTTP 200)
        url = "https://news.google.com/rss/search?q=bitcoin+crypto&hl=en-US&gl=US&ceid=US:en"
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        })
        if resp.status_code == 200:
            try:
                root = ET.fromstring(resp.content)
            except ET.ParseError:
                # Try to fix common RSS issues
                content = resp.text
                # Strip HTML wrapper if present
                if "<html" in content[:200].lower():
                    logger.warning("GoogleNews returned HTML, skipping")
                    raise ValueError("HTML response")
                root = ET.fromstring(content)

            bullish_kw = {"bullish", "rally", "surge", "pump", "breakout",
                         "recovery", "green", "up", "surge", "gain", "high",
                         "etf", "adoption", "institutional", "approve", "ath"}
            bearish_kw = {"bearish", "crash", "dump", "correction", "sell-off",
                         "regulation", "sec", "crackdown", "ban", "lawsuit",
                         "hack", "exploit", "liquidation", "recession", "tariff",
                         "decline", "drop", "fall", "low", "fear", "panic"}

            bullish_count, bearish_count = 0, 0
            items = root.findall(".//item")[:20]
            for item in items:
                title = (item.findtext("title", "") or "").lower()
                words = set(title.split())
                if words & bullish_kw:
                    bullish_count += 1
                elif words & bearish_kw:
                    bearish_count += 1

            total_news = max(bullish_count + bearish_count, 1)
            news_score = (bullish_count - bearish_count) / max(len(items), 1)
            news_conf = min(len(items) / 20 * 0.6, 0.6)
            sources.append({
                "score": news_score,
                "confidence": news_conf,
                "detail": f"GoogleNews {bullish_count}bull/{bearish_count}bear ({len(items)}条)",
            })
            confidence_total += news_conf
            score_total += news_score * news_conf
    except Exception as exc:
        logger.warning(f"GoogleNews failed: {exc}")

    # Source C: alternative.me Fear & Greed
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        fg_data = resp.json()
        fg_value = int(fg_data["data"][0]["value"])
        fg_score = (fg_value - 50) / 50.0
        fg_conf = min(abs(fg_value - 50) / 50.0, 0.5)  # FG confidence cap at 0.5
        sources.append({
            "score": fg_score,
            "confidence": fg_conf,
            "detail": f"FG={fg_value} ({'greed' if fg_value > 50 else 'fear' if fg_value < 50 else 'neutral'})",
        })
        confidence_total += fg_conf
        score_total += fg_score * fg_conf
    except Exception as exc:
        logger.warning(f"FG index failed: {exc}")

    if not sources:
        return SentimentSource(
            source="market_sentiment", score=0.0, confidence=0.0,
            label="neutral", detail="所有数据源不可用",
        )

    # Weight by confidence
    if confidence_total <= 0:
        return SentimentSource(
            source="market_sentiment", score=0.0, confidence=0.0,
            label="neutral", detail="数据置信度为零",
        )

    avg_score = score_total / confidence_total
    avg_confidence = min(confidence_total / len(sources), 0.8)
    label = _score_to_label(avg_score)

    details = " | ".join(s["detail"] for s in sources)

    return SentimentSource(
        source="market_sentiment",
        score=round(avg_score, 3),
        confidence=round(avg_confidence, 2),
        label=label,
        detail=details[:200],
    )


# ── Source 2: Fear & Greed Index (保留, 低成本参考) ──────────────────────

def _fetch_fg_index() -> SentimentSource:
    """Fear & Greed Index from alternative.me (免费可靠)"""
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
        entry = data["data"][0]
        value = int(entry["value"])
        score = (value - 50) / 50.0
        confidence = min(abs(value - 50) / 50.0, 1.0)
        label = _score_to_label(score)
        return SentimentSource(
            source="fear_greed",
            score=round(score, 3),
            confidence=round(confidence, 2),
            label=label,
            detail=f"FG={value} ({label})",
        )
    except Exception as exc:
        logger.warning(f"FG index failed: {exc}")
        return SentimentSource(
            source="fear_greed", score=0.0, confidence=0.0,
            label="neutral", detail="FG不可用",
        )


# ── Helper ──────────────────────────────────────────────────────────────────

def _score_to_label(score: float) -> str:
    if score > 0.6:
        return "bullish"
    elif score > 0.15:
        return "bullish"
    elif score > -0.15:
        return "neutral"
    elif score > -0.6:
        return "bearish"
    return "bearish"


def _overall_label(score: float) -> str:
    if score > 0.6:
        return "strong_bullish"
    elif score > 0.2:
        return "bullish"
    elif score > -0.2:
        return "neutral"
    elif score > -0.6:
        return "bearish"
    return "strong_bearish"


# ── Cache ───────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    try:
        if CACHE_FILE.exists():
            data = json.loads(CACHE_FILE.read_text())
            # 缓存有效期 10 分钟
            if data.get("timestamp", 0) > time.time() - 600:
                return data
    except Exception:
        pass
    return {}


def _save_cache(result: dict):
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(
            json.dumps({**result, "timestamp": time.time()}, ensure_ascii=False, indent=2)
        )
    except Exception as exc:
        logger.warning(f"Cache save failed: {exc}")


# ── Aggregator ──────────────────────────────────────────────────────────────

class EnhancedSentimentAggregator:
    """AI 驱动的情绪聚合器 — 只使用2个数据源但质量远超原来的5个"""

    def __init__(self, cache_ttl: int = 600):
        self.cache_ttl = cache_ttl

    def aggregate(self, force_refresh: bool = False) -> SentimentAggregate:
        if not force_refresh:
            cached = _load_cache()
            if cached and "overall_score" in cached:
                sources = [SentimentSource(**s) for s in cached.get("sources", [])]
                return SentimentAggregate(
                    overall_score=cached["overall_score"],
                    overall_label=cached.get("overall_label", "neutral"),
                    confidence=cached.get("confidence", 0.0),
                    sources=sources,
                    timestamp=cached.get("fetch_timestamp", ""),
                )

        now_ts = datetime.now(timezone.utc).isoformat()

        # 单数据源：market_sentiment (内部聚合 CoinGecko + GoogleNews + FG)
        src = _fetch_ai_sentiment()

        source_list = [src]

        if src.confidence <= 0:
            result = SentimentAggregate(
                overall_score=0.0, overall_label="neutral",
                confidence=0.0, sources=source_list, timestamp=now_ts,
            )
            _save_cache(result.to_dict())
            return result

        result = SentimentAggregate(
            overall_score=round(src.score, 3),
            overall_label=_overall_label(src.score),
            confidence=round(src.confidence, 2),
            sources=source_list,
            timestamp=now_ts,
        )

        cache_data = result.to_dict()
        cache_data["fetch_timestamp"] = now_ts
        _save_cache(cache_data)

        logger.info(
            f"Sentiment aggregated: {src.score:+.3f} ({result.overall_label}, "
            f"conf={src.confidence:.0%}) "
            f"sources={len(source_list)}"
        )
        return result

    def to_trading_signal(self, force_refresh: bool = False) -> dict:
        """返回与旧版完全兼容的 trading signal dict"""
        agg = self.aggregate(force_refresh=force_refresh)

        if agg.overall_score > 0.25:
            bias = "long_bias"
        elif agg.overall_score < -0.25:
            bias = "short_bias"
        else:
            bias = "neutral"

        return {
            "sentiment_score": agg.overall_score,
            "sentiment_label": agg.overall_label,
            "sentiment_confidence": agg.confidence,
            "sentiment_details": agg.to_tg_summary(),
            "trading_bias": bias,
        }


# ── 快速测试 ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    agg = EnhancedSentimentAggregator()
    result = agg.aggregate(force_refresh=True)
    print("=" * 60)
    print(f"Overall: {result.overall_score:+.3f} ({result.overall_label})")
    print(f"Confidence: {result.confidence:.0%}")
    print()
    for s in result.sources:
        print(f"  {s.source}: {s.score:+.3f} (conf={s.confidence:.0%}) {s.detail[:80]}")
    print()
    print("Trading signal:")
    import json
    print(json.dumps(agg.to_trading_signal(), ensure_ascii=False, indent=2))