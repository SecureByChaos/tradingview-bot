from __future__ import annotations

from datetime import datetime, timedelta, timezone

import app.ai.news as news_module
from app.ai.news import (
    Headline,
    _fetch_one_feed,
    _parse_rss_items,
    _parse_rss_pubdate,
    classify_severe_event,
    fetch_market_headlines,
    format_headlines_for_prompt,
)


def _rss(*items: tuple[str, str]) -> bytes:
    """Builds a minimal, valid RSS 2.0 document from (title, pubDate) pairs."""
    body = "".join(
        f"<item><title>{title}</title><pubDate>{pubdate}</pubDate></item>" for title, pubdate in items
    )
    return f"<?xml version='1.0'?><rss><channel>{body}</channel></rss>".encode()


class _FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


# ---------------------------------------------------------------------------
# _parse_rss_pubdate
# ---------------------------------------------------------------------------


def test_parse_rss_pubdate_valid_gmt():
    result = _parse_rss_pubdate("Sun, 25 Oct 2026 06:00:00 GMT")
    assert result == datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)


def test_parse_rss_pubdate_valid_with_numeric_offset():
    result = _parse_rss_pubdate("Sun, 25 Oct 2026 11:30:00 +0530")
    assert result == datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)


def test_parse_rss_pubdate_invalid_returns_none():
    assert _parse_rss_pubdate("not a date") is None
    assert _parse_rss_pubdate("") is None


# ---------------------------------------------------------------------------
# _parse_rss_items
# ---------------------------------------------------------------------------


def test_parse_rss_items_valid_document():
    xml = _rss(("Nifty hits record high", "Sun, 25 Oct 2026 06:00:00 GMT"))
    items = _parse_rss_items(xml, "Test Feed")
    assert len(items) == 1
    assert items[0].title == "Nifty hits record high"
    assert items[0].source == "Test Feed"
    assert items[0].published == datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)


def test_parse_rss_items_skips_item_with_no_title():
    xml = b"<?xml version='1.0'?><rss><channel><item><pubDate>Sun, 25 Oct 2026 06:00:00 GMT</pubDate></item></channel></rss>"
    items = _parse_rss_items(xml, "Test Feed")
    assert items == []


def test_parse_rss_items_malformed_xml_returns_empty():
    items = _parse_rss_items(b"<not valid xml", "Test Feed")
    assert items == []


def test_parse_rss_items_caps_at_max_headlines_per_feed():
    pairs = [(f"Headline {i}", "Sun, 25 Oct 2026 06:00:00 GMT") for i in range(50)]
    xml = _rss(*pairs)
    items = _parse_rss_items(xml, "Test Feed")
    assert len(items) == news_module._MAX_HEADLINES_PER_FEED


def test_parse_rss_items_missing_pubdate_keeps_headline_with_none():
    xml = b"<?xml version='1.0'?><rss><channel><item><title>No date here</title></item></channel></rss>"
    items = _parse_rss_items(xml, "Test Feed")
    assert len(items) == 1
    assert items[0].published is None


# ---------------------------------------------------------------------------
# _fetch_one_feed
# ---------------------------------------------------------------------------


def test_fetch_one_feed_parses_a_real_response(monkeypatch):
    xml = _rss(("Sensex rallies", "Sun, 25 Oct 2026 06:00:00 GMT"))
    monkeypatch.setattr(news_module.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(xml))
    items = _fetch_one_feed("Test Feed", "https://example.com/rss")
    assert len(items) == 1
    assert items[0].title == "Sensex rallies"


def test_fetch_one_feed_fails_soft_on_network_error(monkeypatch):
    def _raise(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(news_module.urllib.request, "urlopen", _raise)
    items = _fetch_one_feed("Test Feed", "https://example.com/rss")
    assert items == []


# ---------------------------------------------------------------------------
# fetch_market_headlines
# ---------------------------------------------------------------------------


def test_fetch_market_headlines_combines_multiple_feeds(monkeypatch):
    now = datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc)

    def _fake_fetch(source_label, url, timeout=8):
        if source_label == "Feed A":
            return [Headline("A headline", "Feed A", now - timedelta(minutes=5))]
        return [Headline("B headline", "Feed B", now - timedelta(minutes=10))]

    monkeypatch.setattr(news_module, "_fetch_one_feed", _fake_fetch)
    result = fetch_market_headlines(now_utc=now, feeds=[("Feed A", "u1"), ("Feed B", "u2")])
    assert {h.title for h in result} == {"A headline", "B headline"}


def test_fetch_market_headlines_one_feed_failing_does_not_lose_the_others(monkeypatch):
    # _fetch_one_feed itself already catches exceptions (tested above) and
    # returns [] on failure -- this test confirms fetch_market_headlines'
    # own per-feed loop correctly keeps going and still returns the other
    # feed's real results when one feed comes back empty.
    now = datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc)

    def _fake_fetch(source_label, url, timeout=8):
        if source_label == "Broken Feed":
            return []
        return [Headline("Still works", "Good Feed", now)]

    monkeypatch.setattr(news_module, "_fetch_one_feed", _fake_fetch)
    result = fetch_market_headlines(now_utc=now, feeds=[("Broken Feed", "u1"), ("Good Feed", "u2")])
    assert [h.title for h in result] == ["Still works"]


def test_fetch_market_headlines_filters_out_stale_items(monkeypatch):
    now = datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        news_module, "_fetch_one_feed",
        lambda *a, **k: [
            Headline("Fresh", "Feed", now - timedelta(minutes=10)),
            Headline("Stale", "Feed", now - timedelta(minutes=200)),
        ],
    )
    result = fetch_market_headlines(now_utc=now, feeds=[("Feed", "u")])
    assert [h.title for h in result] == ["Fresh"]


def test_fetch_market_headlines_keeps_headlines_with_no_known_date(monkeypatch):
    now = datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        news_module, "_fetch_one_feed",
        lambda *a, **k: [Headline("No date", "Feed", None)],
    )
    result = fetch_market_headlines(now_utc=now, feeds=[("Feed", "u")])
    assert len(result) == 1


def test_fetch_market_headlines_sorts_newest_first(monkeypatch):
    now = datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        news_module, "_fetch_one_feed",
        lambda *a, **k: [
            Headline("Older", "Feed", now - timedelta(minutes=30)),
            Headline("Newer", "Feed", now - timedelta(minutes=5)),
        ],
    )
    result = fetch_market_headlines(now_utc=now, feeds=[("Feed", "u")])
    assert [h.title for h in result] == ["Newer", "Older"]


# ---------------------------------------------------------------------------
# format_headlines_for_prompt
# ---------------------------------------------------------------------------


def test_format_headlines_for_prompt_empty_list():
    assert format_headlines_for_prompt([]) == ""


def test_format_headlines_for_prompt_formats_with_source():
    headlines = [Headline("Nifty rallies", "ET Markets", None)]
    text = format_headlines_for_prompt(headlines)
    assert text == '- "Nifty rallies" (ET Markets)'


def test_format_headlines_for_prompt_respects_limit():
    headlines = [Headline(f"H{i}", "Feed", None) for i in range(10)]
    text = format_headlines_for_prompt(headlines, limit=3)
    assert len(text.split("\n")) == 3


# ---------------------------------------------------------------------------
# classify_severe_event
# ---------------------------------------------------------------------------


def test_classify_severe_event_no_match():
    headlines = [Headline("Nifty closes flat", "Feed", None)]
    is_severe, matched = classify_severe_event(headlines)
    assert is_severe is False
    assert matched == []


def test_classify_severe_event_matches_keyword_case_insensitive():
    headlines = [Headline("NSE triggers CIRCUIT BREAKER amid selloff", "Feed", None)]
    is_severe, matched = classify_severe_event(headlines)
    assert is_severe is True
    assert matched == ["NSE triggers CIRCUIT BREAKER amid selloff"]


def test_classify_severe_event_multiple_matches():
    headlines = [
        Headline("Trading halt declared on NSE", "Feed", None),
        Headline("Nifty closes flat", "Feed", None),
        Headline("War declared, markets panic", "Feed", None),
    ]
    is_severe, matched = classify_severe_event(headlines)
    assert is_severe is True
    assert len(matched) == 2
