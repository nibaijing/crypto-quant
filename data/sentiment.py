"""
External Signal Module — Sentiment & Macro Indicators
======================================================
Collects external market sentiment signals from free public APIs:

1. Fear & Greed Index (alternative.me)
2. CryptoPanic News Sentiment (RSS feed)
3. CoinGecko Trending

All data sources are independent functions. `aggregate_signals()` merges
them into a unified trading bias signal used by strategy & execution layers.
"""

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
REQUEST_TIMEOUT = 10  # seconds
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ── Keyword Sentiment Lexicon ────────────────────────────────────────────────
BULLISH_KEYWORDS: List[str] = [
    "bullish", "rally", "surge", "pump", "breakout",
    "etf approved", "halving", "institutional", "adoption",
    "partnership", "accumulation", "upgrade", "btc to",
    "new ath", "all-time high", "record high", "buy",
    "green", "outperform", "positive", "upbeat",
    "rate cut", "dovish", "soft landing",
]

BEARISH_KEYWORDS: List[str] = [
    "bearish", "crash", "dump", "correction", "sell-off",
    "regulation", "sec", "crackdown", "ban", "lawsuit",
    "hack", "exploit", "liquidation", "whale dump",
    "bankruptcy", "ftx", "contagion", "depeg",
    "recession", "inflation", "hawkish", "tariff",
    "war", "geopolitical", "uncertainty",
    "resistance", "rejected", "breakdown",
]


# ── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class FearGreedResult:
    value: int                  # 0-100
    classification: str         # Extreme Fear / Fear / Neutral / Greed / Extreme Greed
    signal: str                 # bearish | neutral | bullish
    timestamp: str              # ISO-8601

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class NewsSentimentResult:
    bullish_count: int
    bearish_count: int
    neutral_count: int
    headlines: List[str]
    overall: str                # bullish | bearish | neutral
    confidence: float           # 0.0 - 1.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SmartMoneyResult:
    signal: str                 # neutral (placeholder)
    data: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Source 1: Fear & Greed Index ─────────────────────────────────────────────

def _classify_fear_greed(value: int) -> str:
    """Map numeric FG value to signal direction."""
    if value <= 25:
        return "bearish"           # extreme fear → contrarian? No — fear = risk-off
    elif value <= 45:
        return "bearish"
    elif value <= 55:
        return "neutral"
    elif value <= 75:
        return "bullish"
    elif value <= 100:
        return "bullish"
    return "neutral"


def get_fear_greed() -> FearGreedResult:
    """Fetch Fear & Greed Index from alternative.me (free, no API key).

    Returns
    -------
    FearGreedResult
        value: 0-100 numeric index
        classification: human-readable label
        signal: trading bias derived from the index
    """
    url = "https://api.alternative.me/fng/?limit=1"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
        entry = data["data"][0]

        value = int(entry["value"])
        classification = entry.get("value_classification", "")
        timestamp = datetime.fromtimestamp(
            int(entry["timestamp"]), tz=timezone.utc
        ).isoformat()

        result = FearGreedResult(
            value=value,
            classification=classification,
            signal=_classify_fear_greed(value),
            timestamp=timestamp,
        )
        logger.info("Fear & Greed: %d (%s) → %s", value, classification, result.signal)
        return result

    except Exception as exc:
        logger.error("Failed to fetch Fear & Greed: %s", exc)
        return FearGreedResult(
            value=50,
            classification="Neutral",
            signal="neutral",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )


# ── Source 2: CryptoPanic News Sentiment ─────────────────────────────────────

def _sentiment_label(counts: Dict[str, int], min_margin: int = 1) -> Tuple[str, float]:
    """Determine overall sentiment from keyword counts.

    Returns (label, confidence) where confidence is proportional to the
    ratio of the dominant category to total classified items.
    """
    bullish = counts.get("bullish", 0)
    bearish = counts.get("bearish", 0)
    total = bullish + bearish

    if total == 0:
        return "neutral", 0.0

    if bearish > bullish + min_margin:
        confidence = bearish / total
        return "bearish", round(confidence, 2)
    if bullish > bearish + min_margin:
        confidence = bullish / total
        return "bullish", round(confidence, 2)

    return "neutral", 0.5


def _classify_headline(headline: str) -> Optional[str]:
    """Classify a single headline as bullish / bearish / neutral via keyword match.

    Returns None for headlines that don't match any keyword (will be counted
    as neutral in the final summary).
    """
    text_lower = headline.lower()

    for kw in BULLISH_KEYWORDS:
        if kw in text_lower:
            return "bullish"
    for kw in BEARISH_KEYWORDS:
        if kw in text_lower:
            return "bearish"

    return None  # neutral / unclassified


def get_news_sentiment(max_headlines: int = 30) -> NewsSentimentResult:
    """Fetch CryptoPanic RSS feed and derive sentiment from keyword counts.

    Uses the free public RSS feed at https://cryptopanic.com/news/rss/.

    Parameters
    ----------
    max_headlines : int
        Cap on headlines to process (RSS feeds can be large).

    Returns
    -------
    NewsSentimentResult
        Counts of bullish / bearish / neutral headlines, sampled headlines,
        and the overall sentiment label with confidence.
    """
    url = "https://cryptopanic.com/news/rss/"
    counts: Dict[str, int] = {"bullish": 0, "bearish": 0, "neutral": 0}
    headlines_sampled: List[str] = []

    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)

        items = root.findall(".//item")[:max_headlines]
        for item in items:
            title_elem = item.find("title")
            if title_elem is not None and title_elem.text:
                headline = title_elem.text.strip()
                label = _classify_headline(headline)
                if label is None:
                    counts["neutral"] += 1
                else:
                    counts[label] += 1
                headlines_sampled.append(headline)

        overall, confidence = _sentiment_label(counts)
        logger.info(
            "News sentiment: bullish=%d bearish=%d neutral=%d → %s (%.2f)",
            counts["bullish"], counts["bearish"], counts["neutral"],
            overall, confidence,
        )
        return NewsSentimentResult(
            bullish_count=counts["bullish"],
            bearish_count=counts["bearish"],
            neutral_count=counts["neutral"],
            headlines=headlines_sampled,
            overall=overall,
            confidence=confidence,
        )

    except Exception as exc:
        logger.error("Failed to fetch CryptoPanic RSS: %s", exc)
        return NewsSentimentResult(
            bullish_count=0,
            bearish_count=0,
            neutral_count=0,
            headlines=[],
            overall="neutral",
            confidence=0.0,
        )


# ── Source 3: CoinGecko Trending ─────────────────────────────────────────────

def get_coingecko_trending(*, btc_symbol: str = "BTC") -> Dict[str, Any]:
    """Fetch CoinGecko trending coins and check whether BTC is among them.

    Parameters
    ----------
    btc_symbol : str
        Symbol to check for (default "BTC").

    Returns
    -------
    dict with keys:
        trending_coins  : top coin symbols (list of str)
        btc_trending    : whether BTC is in trending (bool)
        signal          : derived signal (str)
    """
    url = "https://api.coingecko.com/api/v3/search/trending"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        data = resp.json()

        coins_data = data.get("coins", [])
        trending_symbols = [
            c.get("item", {}).get("symbol", "?") for c in coins_data
        ]
        btc_trending = btc_symbol in trending_symbols

        # Derive signal: BTC trending implies retail attention (mildly bullish),
        # but extreme alt season (no BTC) can also be bullish. Keep it simple.
        if btc_trending:
            signal = "bullish"
        else:
            signal = "neutral"

        logger.info(
            "CoinGecko trending: %s ... btc_trending=%s → %s",
            trending_symbols[:8], btc_trending, signal,
        )
        return {
            "trending_coins": trending_symbols,
            "btc_trending": btc_trending,
            "signal": signal,
        }

    except Exception as exc:
        logger.error("Failed to fetch CoinGecko trending: %s", exc)
        return {
            "trending_coins": [],
            "btc_trending": None,
            "signal": "neutral",
        }


# ── Source 4: Smart Money Flow (Placeholder) ─────────────────────────────────

def get_smart_money_flow() -> SmartMoneyResult:
    """Placeholder for smart-money / whale-flow data.

    Real implementations could pull from:
      - Whale Alert API
      - Glassnode / CoinMetrics (paid)
      - On-chain exchange inflow/outflow (e.g. CryptoQuant — paid)

    Returns
    -------
    SmartMoneyResult with signal="neutral" and placeholder data.
    """
    return SmartMoneyResult(signal="neutral", data="placeholder")


# ── Aggregation ──────────────────────────────────────────────────────────────

def _resolve_conflict(fg_signal: str, news_overall: str) -> str:
    """Resolve conflict between Fear & Greed and news sentiment."""
    # Strong agreement scenarios
    if fg_signal == "bearish" and news_overall == "bearish":
        return "strong_bearish"
    if fg_signal == "bullish" and news_overall == "bullish":
        return "strong_bullish"

    # Moderate agreement
    if fg_signal == "bearish" and news_overall != "bullish":
        return "bearish"
    if fg_signal == "bullish" and news_overall != "bearish":
        return "bullish"
    if news_overall == "bearish" and fg_signal != "bullish":
        return "bearish"
    if news_overall == "bullish" and fg_signal != "bearish":
        return "bullish"

    # Conflict
    return "neutral"


def _overall_to_trading_bias(overall: str) -> str:
    """Map overall signal to a trading bias label."""
    bias_map = {
        "strong_bearish": "short_bias",
        "bearish": "short_bias",
        "neutral": "neutral",
        "bullish": "long_bias",
        "strong_bullish": "long_bias",
    }
    return bias_map.get(overall, "neutral")


def aggregate_signals(*, fetch_external: bool = True) -> Dict[str, Any]:
    """Collect all external signals and compute a unified trading bias.

    Parameters
    ----------
    fetch_external : bool
        If True, calls live APIs. If False, returns safe defaults (offline mode).

    Returns
    -------
    dict
        {
            "fear_greed":          FearGreedResult dict,
            "news":                NewsSentimentResult dict,
            "coingecko_trending":  dict,
            "smart_money":         SmartMoneyResult dict,
            "overall_signal":      "strong_bearish" | "bearish" | "neutral"
                                   | "bullish" | "strong_bullish",
            "confidence":          0.0 - 1.0,
            "trading_bias":        "short_bias" | "neutral" | "long_bias",
        }
    """
    if not fetch_external:
        return {
            "fear_greed": {"value": 50, "classification": "Neutral", "signal": "neutral", "timestamp": datetime.now(timezone.utc).isoformat()},
            "news": {"bullish_count": 0, "bearish_count": 0, "neutral_count": 0, "headlines": [], "overall": "neutral", "confidence": 0.0},
            "coingecko_trending": {"trending_coins": [], "btc_trending": None, "signal": "neutral"},
            "smart_money": {"signal": "neutral", "data": "placeholder"},
            "overall_signal": "neutral",
            "confidence": 0.0,
            "trading_bias": "neutral",
        }

    fg = get_fear_greed()
    news = get_news_sentiment()
    cg = get_coingecko_trending()
    sm = get_smart_money_flow()

    # Primary signal sources: Fear & Greed + News
    fg_signal = fg.signal        # bearish | neutral | bullish
    news_overall = news.overall  # bearish | neutral | bullish

    overall_signal = _resolve_conflict(fg_signal, news_overall)

    # Blend confidence from both sources
    fg_confidence = abs(fg.value - 50) / 50.0          # distance from neutral
    news_confidence = news.confidence
    confidence = round((fg_confidence + news_confidence) / 2.0, 2)

    trading_bias = _overall_to_trading_bias(overall_signal)

    result = {
        "fear_greed": fg.to_dict(),
        "news": news.to_dict(),
        "coingecko_trending": cg,
        "smart_money": sm.to_dict(),
        "overall_signal": overall_signal,
        "confidence": confidence,
        "trading_bias": trading_bias,
    }

    logger.info(
        "Aggregate sentiment: %s (conf=%.2f) → bias=%s",
        overall_signal, confidence, trading_bias,
    )
    return result


# ── Quick test hook ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print("=" * 60)
    print("Fear & Greed:")
    fg = get_fear_greed()
    print(f"  value={fg.value}  classification={fg.classification}  signal={fg.signal}")

    print("\nNews Sentiment:")
    ns = get_news_sentiment(max_headlines=10)
    print(f"  bullish={ns.bullish_count} bearish={ns.bearish_count} "
          f"neutral={ns.neutral_count}  overall={ns.overall} "
          f"confidence={ns.confidence}")
    for h in ns.headlines[:5]:
        print(f"    • {h}")

    print("\nCoinGecko Trending:")
    cg = get_coingecko_trending()
    print(f"  top={cg['trending_coins'][:6]}  btc_trending={cg['btc_trending']}  "
          f"signal={cg['signal']}")

    print("\nAggregate:")
    agg = aggregate_signals()
    print(f"  overall_signal={agg['overall_signal']}  "
          f"confidence={agg['confidence']}  trading_bias={agg['trading_bias']}")
    print("=" * 60)