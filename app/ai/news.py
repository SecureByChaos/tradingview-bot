"""Agentic AI's news agent (step 5, 25 Oct 2026, final piece of the original
five-agent plan -- backtest/QA #108, logging #109, risk/sizing + entry/exit
#110). Fetches recent India-market-relevant headlines from free, keyless RSS
feeds and does two things with them, both deliberately modest:

1. Shows the real headlines to the entry/exit model as plain text context,
   never a fabricated sentiment/impact score -- this project's own standing
   rule (CLAUDE.md's many "never invent a number that isn't in the data"
   gates) applies here as much as anywhere else. The model weighs them
   itself, with the same soft-caution treatment every other contextual
   signal in this project gets before any real evidence justifies a hard
   directional gate (see app.ai.autonomous's own chop/recent-momentum
   history for the precedent).

2. A SEPARATE, deterministic, safety-only check: classify_severe_event()
   looks for a short, deliberately extreme keyword list (circuit breaker,
   war, bank run, trading halt, ...) in recent headlines. A match is a
   "kill switch" -- it blocks new AGENTIC_AI entries and force-closes every
   open AGENTIC_AI position, before the model is asked anything, same "the
   model never decides risk" principle the risk/sizing agent already
   established for P&L, applied here to a market-wide shock instead. This
   does NOT need the same backtest-before-shipping discipline a directional
   signal would -- it is purely protective, extremely rare to fire, and its
   failure mode (pausing paper trading on a false positive) costs nothing
   real. Still a reasoned starting point, not backtested: the keyword list
   could both miss a real event worded unusually, and fire on an unrelated
   headline that happens to contain a matched phrase (a fictional/sports/
   entertainment story using "earthquake" or "war" figuratively, say) --
   named here rather than glossed over.

NOT VERIFIED LIVE FROM THIS SANDBOX -- the agent proxy here blocks outbound
access to all three feed hosts (news.google.com, economictimes.indiatimes.
com, moneycontrol.com all return connect_rejected), the same standing
constraint every other network-dependent module in this project documents.
After deploying, confirm each feed is actually reachable and returning real
items:

    curl -s "https://news.google.com/rss/search?q=Nifty+OR+Sensex+OR+RBI&hl=en-IN&gl=IN&ceid=IN:en" | head -c 2000
    curl -s "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms" | head -c 2000
    curl -s "https://www.moneycontrol.com/rss/latestnews.xml" | head -c 2000

and watch the first few days of [AGENTIC_AI] logs for "[NEWS] ... fetch
failed" lines -- a feed that's moved or started blocking this user agent
degrades silently (fetch_market_headlines() is deliberately fail-soft: one
feed failing never blocks trading, or the other feeds), so this is the
concrete thing to check rather than assuming all three are still live.
"""

from __future__ import annotations

import logging
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

# Free, keyless RSS feeds -- no API key, no auth, no SmartAPI-style rate
# budget to manage. Each is fetched independently; one failing (network
# error, malformed XML, a moved URL) never blocks the others or the
# trading cycle that reads them -- see fetch_market_headlines()'s own
# fail-soft shape.
_NEWS_FEEDS: list[tuple[str, str]] = [
    (
        "Google News (India markets)",
        "https://news.google.com/rss/search?q="
        + quote('Nifty OR Sensex OR "Bank Nifty" OR RBI OR NSE OR BSE')
        + "&hl=en-IN&gl=IN&ceid=IN:en",
    ),
    ("Economic Times Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/latestnews.xml"),
]

_FETCH_TIMEOUT_SECONDS = 8
_MAX_HEADLINES_PER_FEED = 15
# Headlines older than this are dropped from what's shown to the model --
# a 90-minute-old "market fell" story is not live context by the time a
# fresh 5-minute cycle reads it, it's history the entry prompt would
# otherwise read as happening right now. A headline with no parseable
# pubDate is kept rather than dropped (see fetch_market_headlines), since
# excluding it would silently lose real items on a feed that formats dates
# unusually, which is a worse failure than occasionally showing something
# slightly stale.
_HEADLINE_LOOKBACK_MINUTES = 90


@dataclass(frozen=True)
class Headline:
    title: str
    source: str
    published: Optional[datetime]  # None when the feed's own pubDate couldn't be parsed


def _parse_rss_pubdate(value: str) -> Optional[datetime]:
    """RFC 822 ("Sat, 25 Oct 2026 06:00:00 GMT" or with a numeric offset) --
    the format every RSS 2.0 pubDate uses. Returns None rather than raising
    on anything else; the caller treats None as "no known age", not as
    "drop this headline"."""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_rss_items(xml_bytes: bytes, source_label: str) -> list[Headline]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        logger.warning("[NEWS] %s: could not parse RSS XML (%s)", source_label, exc)
        return []
    headlines: list[Headline] = []
    for item in root.iter("item"):
        title_el = item.find("title")
        if title_el is None or not (title_el.text or "").strip():
            continue
        pubdate_el = item.find("pubDate")
        published = _parse_rss_pubdate(pubdate_el.text) if pubdate_el is not None and pubdate_el.text else None
        headlines.append(Headline(title=title_el.text.strip(), source=source_label, published=published))
        if len(headlines) >= _MAX_HEADLINES_PER_FEED:
            break
    return headlines


def _fetch_one_feed(source_label: str, url: str, timeout: int = _FETCH_TIMEOUT_SECONDS) -> list[Headline]:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (StrikeVault news agent)"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except Exception as exc:
        logger.warning("[NEWS] %s: fetch failed (%s)", source_label, exc)
        return []
    return _parse_rss_items(body, source_label)


def fetch_market_headlines(
    now_utc: Optional[datetime] = None,
    feeds: Optional[list[tuple[str, str]]] = None,
) -> list[Headline]:
    """Pulls recent headlines from every configured feed independently,
    filters to the last _HEADLINE_LOOKBACK_MINUTES minutes, and returns
    them newest-first. Never raises -- a total failure of every feed
    returns an empty list, same fail-soft-never-block-trading philosophy
    this project already applies to the live index-price feed.

    Deliberately not cached beyond one call's lifetime -- these feeds
    update roughly as often as this strategy's own 5-minute cycle anyway,
    and re-fetching three cheap, keyless HTTP GETs each cycle costs
    nothing worth caching against, unlike SmartAPI's real quote throttle
    budget."""
    now_utc = now_utc or datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(minutes=_HEADLINE_LOOKBACK_MINUTES)
    active_feeds = feeds if feeds is not None else _NEWS_FEEDS
    all_headlines: list[Headline] = []
    for source_label, url in active_feeds:
        all_headlines.extend(_fetch_one_feed(source_label, url))
    recent = [h for h in all_headlines if h.published is None or h.published >= cutoff]
    recent.sort(key=lambda h: h.published or now_utc, reverse=True)
    return recent


def format_headlines_for_prompt(headlines: list[Headline], limit: int = 8) -> str:
    """Plain-text bullet list for the entry/exit prompt -- real headlines,
    never a fabricated sentiment score. Empty string (not a placeholder
    sentence) when there's nothing to show, so the caller can omit the
    section entirely rather than render an empty one."""
    if not headlines:
        return ""
    return "\n".join(f'- "{h.title}" ({h.source})' for h in headlines[:limit])


# ---------------------------------------------------------------------------
# Severe-event keyword gate -- deterministic, safety-only. See module
# docstring for why this doesn't need the same validation discipline a
# directional signal would.
# ---------------------------------------------------------------------------

# Reasoned starting point, not backtested -- same status every other new
# threshold in this project carries before real history looks at it. Kept
# short and deliberately extreme (genuine market-wide shocks, not ordinary
# bearish news) so this almost never fires; an ordinary bad headline is
# exactly what the entry/exit model's own judgment is already for.
_SEVERE_KEYWORDS: list[str] = [
    "circuit breaker", "trading halt", "market crash", "flash crash",
    "war declared", "military strike", "invasion", "nuclear attack",
    "terror attack", "terrorist attack", "assassination",
    "bank run", "banking collapse", "sovereign default", "rbi emergency",
    "state of emergency", "coup", "pandemic declared",
]


def classify_severe_event(headlines: list[Headline]) -> tuple[bool, list[str]]:
    """True plus the matching headline titles if ANY recent headline
    contains a severe-event keyword (case-insensitive substring match).
    (False, []) otherwise. Deliberately coarse -- a real black-swan event
    would be covered by multiple outlets in blunt language ("market
    crash", "trading halt"), so a substring match is enough to catch it;
    refining this further only matters once a real false positive is
    actually observed, not speculatively now."""
    matched = [h.title for h in headlines if any(keyword in h.title.lower() for keyword in _SEVERE_KEYWORDS)]
    return (len(matched) > 0, matched)
