#!/usr/bin/env python3
"""
EnhancedSentimentAggregator — 多源情绪聚合器

对标推文④的"打通低成本情绪来源":
  - OKX情绪指数 (OKX Fear & Greed, 免费)
  - 推特情绪扫描 (通过免费API: TweetScout或Twitter趋势)
  - 新闻情绪 (已有 CryptoPanic RSS)
  - Surf社交数据 (通过 CoinGecko/CoinMarketCap 社区情绪)
  - 本地KOL情绪记录 (JSON持久化, 手动/自动录入)

用法:
  aggregator = EnhancedSentimentAggregator()
  result = aggregator.aggregate()
  # -> {okx, twitter, news, surf, kol, overall_score, details}

设计目标:
  1. 每个数据源独立 (一个挂了不影响其他)
  2. 输出统一评分 -1.0 (极度看空) ~ +1.0 (极度看多)
  3. 每个来源附带置信度 0.0~1.0
  4. 情绪明细可直接展示在TG消息和仓位看板
"""

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
REQUEST_TIMEOUT = 10
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
PROJECT = Path(__file__).resolve().parent.parent
KOL_FILE = PROJECT / "data" / "kol_sentiment.json"
SENTIMENT_CACHE_FILE = PROJECT / "data" / "sentiment_cache.json"

# ── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class SentimentSource:
    """单个情绪源的结果"""
    source: str          # okx | twitter | news | surf | kol
    score: float         # -1.0 ~ +1.0
    confidence: float    # 0.0 ~ 1.0
    label: str           # bearish | neutral | bullish
    detail: str          # 单行说明, 供TG展示
    raw_data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SentimentAggregate:
    """聚合情绪结果"""
    overall_score: float     # -1.0 ~ +1.0
    overall_label: str       # strong_bearish | bearish | neutral | bullish | strong_bullish
    confidence: float        # 0.0 ~ 1.0
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
        """生成单行TG展示文本"""
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


# ── Source 1: OKX 情绪指数 ────────────────────────────────────────────────

def _fetch_okx_sentiment() -> SentimentSource:
    """OKX Fear & Greed Index (免费, 无需API key)

    类似 alternative.me 但面向加密货币衍生品市场。
    URL: https://www.okx.com/api/v5/rubik/indicator/fear_and_greed
    """
    url = "https://www.okx.com/api/v5/rubik/indicator/fear_and_greed"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        if resp.status_code != 200:
            # Fallback: alternative.me (已有)
            return _fetch_alternative_me_sentiment()

        data = resp.json()
        if data.get("code") != "0" or not data.get("data"):
            return _fetch_alternative_me_sentiment()

        entries = data["data"]
        if not entries:
            return _fetch_alternative_me_sentiment()

        latest = entries[0]
        value = int(latest.get("value", 50))
        # OKX FG: 0=极度恐惧, 100=极度贪婪
        score = (value - 50) / 50.0  # -1.0 ~ +1.0
        confidence = abs(value - 50) / 50.0  # 越极端置信度越高
        label = _score_to_label(score)

        return SentimentSource(
            source="okx",
            score=round(score, 3),
            confidence=round(confidence, 2),
            label=label,
            detail=f"OKX FG={value} ({label})",
        )

    except Exception as exc:
        logger.warning(f"OKX sentiment failed: {exc}, fallback to alternative.me")
        return _fetch_alternative_me_sentiment()


def _fetch_alternative_me_sentiment() -> SentimentSource:
    """已有 Fear & Greed 作为 OKX 的 fallback"""
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
        entry = data["data"][0]
        value = int(entry["value"])
        score = (value - 50) / 50.0
        confidence = abs(value - 50) / 50.0
        label = _score_to_label(score)
        return SentimentSource(
            source="okx",
            score=round(score, 3),
            confidence=round(confidence, 2),
            label=label,
            detail=f"FG={value} ({label}, alt.me fallback)",
        )
    except Exception as exc:
        logger.error(f"alternative.me also failed: {exc}")
        return SentimentSource(
            source="okx", score=0.0, confidence=0.0,
            label="neutral", detail="FG unavailable",
        )


# ── Source 2: Twitter 情绪扫描 ───────────────────────────────────────────

def _fetch_twitter_sentiment() -> SentimentSource:
    """Twitter 情绪扫描 — 通过免费API (TweetScout/GDELT) 扫描BTC相关推文

    不使用付费Twitter API, 用以下免费替代:
    1. GDELT 2.0 (新闻+社交聚合, 免费)
    2. TweetScout 社区分析 (如果可用)
    3. Nitter RSS 作为fallback
    """
    try:
        # GDELT + GoogleNews 混合搜索BTC推文情绪
        bullish_kw = ["bullish", "buy", "moon", "pump", "breakout", "rally", "up",
                      "surge", "green", "recovery", "outperform", "positive"]
        bearish_kw = ["bearish", "sell", "crash", "dump", "correction", "fear", "down",
                      "decline", "red", "plunge", "selloff", "liquidation", "recession"]
        bullish_count, bearish_count = 0, 0
        articles_found = 0

        # 策略1: GDELT
        try:
            url = (
                "https://api.gdeltproject.org/api/v2/summary/summary"
                "?d=btc&format=json&mode=ArtList&maxrows=10"
            )
            resp = requests.get(url, timeout=8, headers={"User-Agent": USER_AGENT})
            if resp.status_code == 200:
                data = resp.json()
                articles = data.get("articles", [])[:15]
                articles_found = len(articles)
                for article in articles:
                    text = (article.get("title", "") + " " + article.get("summary", "")).lower()
                    for kw in bullish_kw:
                        if kw in text:
                            bullish_count += 1
                            break
                    for kw in bearish_kw:
                        if kw in text:
                            bearish_count += 1
                            break
        except Exception:
            pass

        # 策略2: 再查几条Google News补充
        try:
            url = "https://news.google.com/rss/search?q=bitcoin+price+crypto&hl=en-US&gl=US&ceid=US:en"
            resp = requests.get(url, timeout=8, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            items = root.findall(".//item")[:10]
            for item in items:
                title = item.findtext("title", "")
                text_lower = title.lower()
                for kw in bullish_kw:
                    if kw in text_lower:
                        bullish_count += 1
                        break
                for kw in bearish_kw:
                    if kw in text_lower:
                        bearish_count += 1
                        break
            articles_found += len(items)
        except Exception:
            pass

        total = max(bullish_count + bearish_count, 1)
        net_score = (bullish_count - bearish_count) / total
        confidence = min(total / max(articles_found, 1) * 0.8, 0.7)
        label = _score_to_label(net_score)
        return SentimentSource(
            source="twitter",
            score=round(net_score, 3),
            confidence=round(confidence, 2),
            label=label,
            detail=f"社交扫描 {bullish_count}bull/{bearish_count}bear ({articles_found}条)",
        )

    except Exception as exc:
        logger.warning(f"GDELT failed: {exc}, try Nitter")
        return _fetch_nitter_sentiment()


def _fetch_nitter_sentiment() -> SentimentSource:
    """Fallback: Nitter RSS 扫描BTC相关推文"""
    try:
        # 用 Google News RSS 替代 (比Nitter更稳定)
        url = "https://news.google.com/rss/search?q=bitcoin+crypto+trading&hl=en-US&gl=US&ceid=US:en"
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)

        bullish_kw = ["bullish", "buy", "rally", "surge", "breakout", "recovery"]
        bearish_kw = ["bearish", "sell", "crash", "dump", "decline", "fear", "regulation"]
        bullish_count, bearish_count = 0, 0

        items = root.findall(".//item")[:15]
        for item in items:
            title = item.findtext("title", "")
            text_lower = title.lower()
            for kw in bullish_kw:
                if kw in text_lower:
                    bullish_count += 1
                    break
            for kw in bearish_kw:
                if kw in text_lower:
                    bearish_count += 1
                    break

        total = max(bullish_count + bearish_count, 1)
        net_score = (bullish_count - bearish_count) / total
        confidence = min(total / 8, 0.6)
        label = _score_to_label(net_score)
        return SentimentSource(
            source="twitter",
            score=round(net_score, 3),
            confidence=round(confidence, 2),
            label=label,
            detail=f"GoogleNews {bullish_count}bull/{bearish_count}bear",
        )

    except Exception as exc:
        logger.error(f"Twitter/Nitter sentiment failed: {exc}")
        return SentimentSource(
            source="twitter", score=0.0, confidence=0.0,
            label="neutral", detail="推特数据不可用",
        )


# ── Source 3: 新闻情绪 (升级已有) ────────────────────────────────────────

def _fetch_news_sentiment(max_headlines: int = 20) -> SentimentSource:
    """新闻情绪 — CryptoPanic RSS + GoogleNews (已有 sentinment.py 的升级版)"""
    # 先用 CryptoPanic
    try:
        url = "https://cryptopanic.com/news/rss/"
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        if resp.status_code != 200:
            raise Exception(f"HTTP {resp.status_code}")
        root = ET.fromstring(resp.content)

        bullish_kw = ["bullish", "rally", "surge", "pump", "breakout", "etf approved",
                      "halving", "institutional", "adoption", "partnership", "new ath",
                      "rate cut", "dovish"]
        bearish_kw = ["bearish", "crash", "dump", "correction", "sell-off",
                      "regulation", "sec", "crackdown", "ban", "lawsuit",
                      "hack", "exploit", "liquidation", "recession", "tariff"]

        bullish_count, bearish_count = 0, 0
        items = root.findall(".//item")[:max_headlines]
        for item in items:
            title = item.findtext("title", "")
            text_lower = title.lower()
            for kw in bullish_kw:
                if kw in text_lower:
                    bullish_count += 1
                    break
            for kw in bearish_kw:
                if kw in text_lower:
                    bearish_count += 1
                    break

        total = max(bullish_count + bearish_count, 1)
        net_score = (bullish_count - bearish_count) / total
        confidence = min((bullish_count + bearish_count) / max_headlines, 1.0) * 0.8
        label = _score_to_label(net_score)
        return SentimentSource(
            source="news",
            score=round(net_score, 3),
            confidence=round(confidence, 2),
            label=label,
            detail=f"CryptoPanic {bullish_count}bull/{bearish_count}bear",
        )

    except Exception as exc:
        logger.warning(f"CryptoPanic failed: {exc}")
        # Fallback: Google News
        return _fetch_google_news_sentiment()


def _fetch_google_news_sentiment() -> SentimentSource:
    """Fallback: Google News RSS"""
    try:
        url = "https://news.google.com/rss/search?q=cryptocurrency+bitcoin&hl=en-US&gl=US&ceid=US:en"
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)

        bullish_kw = ["bullish", "rally", "surge", "breakout", "recovery"]
        bearish_kw = ["bearish", "crash", "dump", "decline", "regulation", "sell"]
        bullish_count, bearish_count = 0, 0
        items = root.findall(".//item")[:15]
        for item in items:
            title = item.findtext("title", "")
            text_lower = title.lower()
            for kw in bullish_kw:
                if kw in text_lower:
                    bullish_count += 1
                    break
            for kw in bearish_kw:
                if kw in text_lower:
                    bearish_count += 1
                    break

        total = max(bullish_count + bearish_count, 1)
        net_score = (bullish_count - bearish_count) / total
        confidence = min(total / 10, 0.5)
        label = _score_to_label(net_score)
        return SentimentSource(
            source="news",
            score=round(net_score, 3),
            confidence=round(confidence, 2),
            label=label,
            detail=f"GoogleNews {bullish_count}bull/{bearish_count}bear",
        )
    except Exception as exc:
        logger.error(f"Google News failed: {exc}")
        return SentimentSource(
            source="news", score=0.0, confidence=0.0,
            label="neutral", detail="新闻数据不可用",
        )


# ── Source 4: Surf 社交数据 ───────────────────────────────────────────────

def _fetch_surf_sentiment() -> SentimentSource:
    """Surf 社交数据 — 从 CoinGecko/CoinMarketCap 社区情绪

    使用 CoinGecko 社区情绪指标 (免费):
    - 社区活跃度
    - developer activity
    - 社交媒体 mentions
    """
    try:
        # CoinGecko Social Media (community data API)
        url = "https://api.coingecko.com/api/v3/coins/bitcoin"
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            params={"localization": "false", "tickers": "false",
                    "community_data": "true", "developer_data": "false",
                    "sparkline": "false"},
        )
        if resp.status_code != 200:
            raise Exception(f"HTTP {resp.status_code}")

        data = resp.json()
        community = data.get("community_data", {})
        sentiment_votes_up = community.get("sentiment_votes_up_percentage", 50)
        sentiment_votes_down = community.get("sentiment_votes_down_percentage", 50)

        # Reddit 数据 (可用时)
        reddit_subscribers = community.get("reddit_subscribers", 0)
        reddit_avg_48h = community.get("reddit_average_posts_48h", 0)

        # 综合社交情绪
        net_ratio = (sentiment_votes_up - sentiment_votes_down) / 100.0
        # 从 [0,1] 映射到 [-1,1]
        score = net_ratio
        confidence = min(abs(net_ratio) * 2, 1.0)
        if reddit_subscribers > 0 and reddit_avg_48h > 0:
            confidence = min(confidence + 0.15, 1.0)

        label = _score_to_label(score)
        return SentimentSource(
            source="surf",
            score=round(score, 3),
            confidence=round(confidence, 2),
            label=label,
            detail=f"社交投票 {sentiment_votes_up}%up/{sentiment_votes_down}%down",
        )

    except Exception as exc:
        logger.warning(f"CoinGecko surf data failed: {exc}")
        # Fallback: CoinMarketCap community (另一个免费源)
        try:
            url = "https://web-api.coinmarketcap.com/v1/cryptocurrency/info?id=1"
            resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                                headers={"User-Agent": USER_AGENT})
            if resp.status_code != 200:
                raise Exception(f"HTTP {resp.status_code}")
            # CMC 数据格式不确定, 中性fallback
        except Exception:
            pass

        return SentimentSource(
            source="surf", score=0.0, confidence=0.0,
            label="neutral", detail="Surf数据不可用",
        )


# ── Source 5: 本地KOL情绪记录 ────────────────────────────────────────────

def _load_kol_sentiment() -> SentimentSource:
    """加载本地KOL情绪记录 (JSON文件, 手动/自动录入)

    KOL_FILE 格式:
    {
        "records": [
            {"name": "TraderX", "sentiment": "bullish", "confidence": 0.8, "timestamp": "..."},
            {"name": "CryptoY", "sentiment": "bearish", "confidence": 0.6, "timestamp": "..."}
        ]
    }
    """
    if not KOL_FILE.exists():
        return SentimentSource(
            source="kol", score=0.0, confidence=0.0,
            label="neutral", detail="无KOL记录",
        )

    try:
        data = json.loads(KOL_FILE.read_text())
        records = data.get("records", [])

        if not records:
            return SentimentSource(
                source="kol", score=0.0, confidence=0.0,
                label="neutral", detail="KOL记录为空",
            )

        # 只取最近24小时的KOL记录
        cutoff = time.time() - 86400
        recent = [r for r in records if r.get("timestamp", 0) > cutoff]

        if not recent:
            return SentimentSource(
                source="kol", score=0.0, confidence=0.0,
                label="neutral", detail="KOL记录过期",
            )

        total_score = 0.0
        total_weight = 0.0
        for r in recent:
            sentiment = r.get("sentiment", "neutral")
            conf = r.get("confidence", 0.5)
            weight = conf
            if sentiment == "bullish":
                total_score += 1.0 * weight
            elif sentiment == "bearish":
                total_score += -1.0 * weight
            total_weight += weight

        if total_weight == 0:
            return SentimentSource(
                source="kol", score=0.0, confidence=0.0,
                label="neutral", detail="KOL权重为零",
            )

        avg_score = total_score / total_weight
        confidence = min(total_weight / len(recent), 1.0)
        label = _score_to_label(avg_score)
        return SentimentSource(
            source="kol",
            score=round(avg_score, 3),
            confidence=round(confidence, 2),
            label=label,
            detail=f"KOL {len(recent)}条记录",
        )

    except Exception as exc:
        logger.error(f"KOL loading failed: {exc}")
        return SentimentSource(
            source="kol", score=0.0, confidence=0.0,
            label="neutral", detail="KOL加载异常",
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
        if SENTIMENT_CACHE_FILE.exists():
            data = json.loads(SENTIMENT_CACHE_FILE.read_text())
            # 缓存有效期5分钟
            if data.get("timestamp", 0) > time.time() - 300:
                return data
    except Exception:
        pass
    return {}


def _save_cache(result: dict):
    try:
        SENTIMENT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SENTIMENT_CACHE_FILE.write_text(
            json.dumps({**result, "timestamp": time.time()}, ensure_ascii=False, indent=2)
        )
    except Exception as exc:
        logger.warning(f"Cache save failed: {exc}")


# ── Aggregation ────────────────────────────────────────────────────────────

class EnhancedSentimentAggregator:
    """多源情绪聚合器 — 对标推文④的"情绪数据不再是摆设" """

    def __init__(self, cache_ttl: int = 300):
        self.cache_ttl = cache_ttl
        self._sources_order = ["okx", "twitter", "news", "surf", "kol"]

    def aggregate(self, force_refresh: bool = False) -> SentimentAggregate:
        """聚合所有情绪源, 返回统一结果

        Parameters
        ----------
        force_refresh : bool
            是否强制刷新缓存

        Returns
        -------
        SentimentAggregate with overall_score, overall_label, confidence, sources
        """
        # 尝试缓存
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

        # 并行获取所有源 (按顺序逐个获取, 避免并发问题)
        sources: Dict[str, SentimentSource] = {}

        sources["okx"] = _fetch_okx_sentiment()
        sources["twitter"] = _fetch_twitter_sentiment()
        sources["news"] = _fetch_news_sentiment()
        sources["surf"] = _fetch_surf_sentiment()
        sources["kol"] = _load_kol_sentiment()

        # 加权平均
        source_weights = {
            "okx": 0.30,      # 恐惧贪婪指数 — 权重最高
            "twitter": 0.20,  # 推特情绪
            "news": 0.25,     # 新闻情绪
            "surf": 0.15,     # 社交数据
            "kol": 0.10,      # KOL记录
        }

        total_score = 0.0
        total_weight = 0.0
        total_confidence = 0.0
        source_list: List[SentimentSource] = []

        for src_name in self._sources_order:
            src = sources.get(src_name)
            if src and src.confidence > 0:
                w = source_weights.get(src_name, 0.15)
                effective_weight = w * min(src.confidence + 0.3, 1.0)  # 置信度调权
                total_score += src.score * effective_weight
                total_weight += effective_weight
                total_confidence += src.confidence * w
                source_list.append(src)
            elif src:
                # 置信度为0: 降权到原始权重1/3
                w = source_weights.get(src_name, 0.15)
                total_weight += w * 0.33
                source_list.append(src)

        if total_weight == 0 or not source_list:
            result = SentimentAggregate(
                overall_score=0.0,
                overall_label="neutral",
                confidence=0.0,
                sources=source_list,
                timestamp=now_ts,
            )
            _save_cache(result.to_dict())
            return result

        avg_score = total_score / total_weight
        avg_confidence = total_confidence / sum(source_weights.values())

        # 截断
        avg_score = max(-1.0, min(1.0, avg_score))
        avg_confidence = max(0.0, min(1.0, avg_confidence))

        result = SentimentAggregate(
            overall_score=round(avg_score, 3),
            overall_label=_overall_label(avg_score),
            confidence=round(avg_confidence, 2),
            sources=source_list,
            timestamp=now_ts,
        )

        # 写缓存
        cache_data = result.to_dict()
        cache_data["fetch_timestamp"] = now_ts
        _save_cache(cache_data)

        logger.info(
            f"Sentiment aggregated: {avg_score:+.3f} ({result.overall_label}, "
            f"conf={avg_confidence:.0%}) sources={len(source_list)}"
        )
        return result

    def to_trading_signal(self, force_refresh: bool = False) -> dict:
        """转换为交易信号 (兼容旧 aggregate_signals API)

        Returns
        -------
        dict with keys:
            sentiment_score: -1.0 ~ +1.0
            sentiment_label: str
            sentiment_confidence: float
            sentiment_details: str (TG展示)
            trading_bias: str (short_bias | neutral | long_bias)
        """
        agg = self.aggregate(force_refresh=force_refresh)

        # 确定交易偏向
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


# ── Quick test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    agg = EnhancedSentimentAggregator()
    result = agg.aggregate(force_refresh=True)
    print("=" * 60)
    print(f"Overall: {result.overall_score:+.3f} ({result.overall_label})")
    print(f"Confidence: {result.confidence:.0%}")
    print()
    for s in result.sources:
        emoji = "🟢" if s.score > 0.1 else ("🔴" if s.score < -0.1 else "⚪")
        print(f"  {emoji} {s.source:8s} | {s.score:+.3f} | conf={s.confidence:.0%} | {s.detail}")
    print()
    print(result.to_tg_summary())
    print()

    sig = agg.to_trading_signal()
    print(f"Trading bias: {sig['trading_bias']}")
    print("=" * 60)