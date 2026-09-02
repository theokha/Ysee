from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from yc_monitor.models import AdapterHealth, CanonicalItem, CollectionResult, HealthStatus, Source

SEARCH_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
DEFAULT_BATCHES = ("F26", "W27", "S27")
_BATCH_CODE = re.compile(r"^[SWF]\d{2}$")
OFFICIAL_HANDLES = frozenset({"ycombinator", "ycombinator_"})


def parse_batch_codes(value: str | Sequence[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    parts = value.split(",") if isinstance(value, str) else list(value)
    codes: list[str] = []
    seen: set[str] = set()
    for raw in parts:
        code = re.sub(r"\s+", "", str(raw).strip().upper())
        if _BATCH_CODE.fullmatch(code) and code not in seen:
            seen.add(code)
            codes.append(code)
    return tuple(codes)


def build_search_queries(batches: Sequence[str] | str | None = DEFAULT_BATCHES) -> tuple[str, ...]:
    """Focused Latest-search groups using phrase/OR operators TwitterAPI.io documents."""
    codes = parse_batch_codes(batches)
    queries = (
        '("got into YC" OR "accepted into YC" OR "accepted to YC" OR "joining YC") lang:en',
        '("got into Y Combinator" OR "accepted into Y Combinator" OR "joining Y Combinator") lang:en',
        '("today we\'re launching" YC OR "we\'re launching" YC) lang:en',
        '("backed by Y Combinator" OR "backed by YC") lang:en',
        '("a16z Speedrun" OR "speedrun a16z" OR "YC Speedrun" OR "Speedrun batch" OR "got into Speedrun" OR "accepted into Speedrun") lang:en',
    )
    if not codes:
        return queries
    batch_clause = " OR ".join(f'"YC {code}"' for code in codes)
    return (*queries, f"({batch_clause}) lang:en")


class TwitterAdapter:
    source = Source.TWITTER

    def __init__(
        self,
        api_key: str | None,
        max_pages: int = 3,
        lookback_days: int = 7,
        current_batches: Sequence[str] | str | None = DEFAULT_BATCHES,
    ) -> None:
        self.api_key = api_key
        self.max_pages = max_pages
        self.lookback_days = lookback_days
        self.current_batches = parse_batch_codes(current_batches)
        self.queries = build_search_queries(self.current_batches)

    async def collect(self, client: httpx.AsyncClient) -> CollectionResult:
        if not self.api_key:
            return CollectionResult([], AdapterHealth(
                self.source, HealthStatus.SKIPPED, "TWITTERAPI_IO_API_KEY is not configured"
            ))
        items: dict[str, CanonicalItem] = {}
        since_time = int((datetime.now(UTC) - timedelta(days=self.lookback_days)).timestamp())
        for base_query in self.queries:
            query = f"{base_query} since_time:{since_time}"
            cursor = ""
            for _ in range(self.max_pages):
                response = await client.get(
                    SEARCH_URL,
                    headers={"X-API-Key": self.api_key},
                    params={"query": query, "queryType": "Latest", "cursor": cursor},
                    timeout=20,
                )
                response.raise_for_status()
                payload = response.json()
                tweets = payload.get("tweets") or payload.get("data") or []
                for tweet in tweets:
                    if not isinstance(tweet, dict):
                        continue
                    item = normalize_tweet(tweet)
                    if item and (item.founder_handle or "") not in OFFICIAL_HANDLES:
                        items[item.item_id] = item
                cursor = str(payload.get("next_cursor") or payload.get("nextCursor") or "")
                if not cursor or not tweets:
                    break
        values = list(items.values())
        return CollectionResult(values, AdapterHealth(
            self.source,
            HealthStatus.OK,
            (
                f"Twitter searches completed with {len(self.queries)} query groups, "
                f"{self.lookback_days}-day lookback"
            ),
            len(values),
        ))


def parse_tweet_timestamp(value: object) -> datetime | None:
    """Parse TwitterAPI.io createdAt values without inventing a time.

    Live advanced_search tweets use Twitter's classic form
    ``Mon Aug 31 21:54:24 +0000 2026``. ISO-8601 (including ``Z``) is preserved.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if timestamp > 1e12:
            timestamp /= 1000.0
        try:
            return datetime.fromtimestamp(timestamp, UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        parsed = None
    if parsed is not None:
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError, IndexError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def normalize_tweet(tweet: dict[str, Any]) -> CanonicalItem | None:
    tweet_id = str(tweet.get("id") or tweet.get("tweet_id") or "")
    text = str(tweet.get("text") or "")
    author = tweet.get("author") or tweet.get("user") or {}
    if not tweet_id or not text or not isinstance(author, dict):
        return None
    handle = str(author.get("userName") or author.get("username") or "").lstrip("@").lower()
    name = str(author.get("name") or "") or None
    published = parse_tweet_timestamp(tweet.get("createdAt") or tweet.get("created_at"))
    url = str(tweet.get("url") or tweet.get("twitterUrl") or f"https://x.com/{handle}/status/{tweet_id}")
    return CanonicalItem(
        source=Source.TWITTER, item_id=tweet_id, company_name=None,
        canonical_url=url, content_text=text, description=text,
        founder_name=name, founder_handle=handle or None,
        author_url=f"https://x.com/{handle}" if handle else None,
        published_at=published, raw=tweet,
    )
