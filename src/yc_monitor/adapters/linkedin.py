from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from yc_monitor.models import AdapterHealth, CanonicalItem, CollectionResult, HealthStatus, Source

ACTOR_ID = "buIWk2uOUzTmcLsuB"
PINNED_BUILD_ID = "ASBzmjLXGQlvadkLr"
KNOWN_BUILD_IDS = frozenset({
    "ASBzmjLXGQlvadkLr",  # 0.0.110
    "tBATtQstpZt632roT",  # 0.0.104
})
APIFY_API_BASE = "https://api.apify.com/v2"
# Posts older than this are dropped after fetch. The 8h cycle + 36h window
# means every post is visible for at least 4 cycles before it expires, so a
# single failed cycle cannot lose a signal permanently.
LINKEDIN_WINDOW_HOURS_DEFAULT = 36
DEFAULT_QUERIES = (
    '"got into YC" OR "accepted into YC" OR "joining YC"',
    '"backed by Y Combinator" OR "YC S26"',
    '"got into Speedrun" OR "accepted into Speedrun" OR "a16z Speedrun" OR "YC Speedrun"',
)


def allocate_query_max_posts(total_posts: int, query_count: int) -> list[int]:
    """Split a cycle-wide LinkedIn budget across HarvestAPI search queries.

    HarvestAPI `maxPosts` is per query. Passing the cycle total as `maxPosts`
    would scrape about `query_count` times the configured budget.
    """
    if query_count <= 0:
        return []
    bounded = max(total_posts, 0)
    base, remainder = divmod(bounded, query_count)
    return [base + (1 if index < remainder else 0) for index in range(query_count)]


def harvest_search_request(
    total_posts: int,
    queries: Sequence[str] = DEFAULT_QUERIES,
) -> dict[str, Any]:
    if not queries:
        queries = DEFAULT_QUERIES
    allocations = allocate_query_max_posts(max(total_posts, 0), len(queries))
    selected = [query for query, count in zip(queries, allocations, strict=True) if count > 0]
    if not selected:
        selected = list(queries[:1]) or list(DEFAULT_QUERIES[:1])
        per_query = max(total_posts, 1)
    else:
        # One actor `maxPosts` applies to every query, so use the smallest slot.
        per_query = min(count for count in allocations if count > 0)
    return {
        "searchQueries": selected,
        "maxPosts": per_query,
        # Fetch a full week from the actor, then filter client-side to
        # LINKEDIN_WINDOW_HOURS so posts between cycles are never missed.
        "postedLimit": "week",
        "sortBy": "date",
        "profileScraperMode": "short",
        "scrapeComments": False,
        "scrapeReactions": False,
    }


class LinkedInCollectError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class LinkedInAdapter:
    """Search LinkedIn posts through HarvestAPI's pinned Apify actor build."""

    source = Source.LINKEDIN

    def __init__(
        self,
        api_token: str | None,
        total_posts: int = 50,
        actor_id: str = ACTOR_ID,
        build_id: str = PINNED_BUILD_ID,
        window_hours: int = 36,
    ) -> None:
        self.api_token = api_token
        self.total_posts = min(max(total_posts, 1), 100)
        self.actor_id = actor_id
        self.build_id = build_id
        self.window_hours = window_hours

    async def collect(self, client: httpx.AsyncClient) -> CollectionResult:
        if not self.api_token:
            return CollectionResult(
                [],
                AdapterHealth(
                    self.source,
                    HealthStatus.SKIPPED,
                    "APIFY_API_TOKEN is not configured",
                ),
            )

        run = await self._start_run(client)
        status = str(run.get("status") or "")
        if status != "SUCCEEDED":
            raise LinkedInCollectError(
                "apify_actor_failed",
                f"Apify actor run ended with status {status or 'unknown'}",
            )
        build_id = str(run.get("buildId") or "")
        dataset_id = str(run.get("defaultDatasetId") or "")
        if not dataset_id:
            raise LinkedInCollectError("apify_dataset_missing", "Apify actor run did not return a dataset")

        records = await self._dataset_items(client, dataset_id)
        if records and not any(normalize_post(record) for record in records):
            raise LinkedInCollectError(
                "apify_schema_drift",
                f"Actor build {build_id or 'unknown'} returned no recognizable post records",
            )
        items: dict[str, CanonicalItem] = {}
        cutoff = datetime.now(UTC) - timedelta(hours=self.window_hours)
        for record in records:
            item = normalize_post(record)
            if item is None:
                continue
            if item.published_at is not None and item.published_at < cutoff:
                continue
            items[item.item_id] = item
        values = list(items.values())[: self.total_posts]
        return CollectionResult(
            values,
            AdapterHealth(
                self.source,
                HealthStatus.OK,
                (
                    f"LinkedIn posts searched with HarvestAPI actor {self.actor_id}; "
                    f"cycle budget {self.total_posts}"
                ),
                len(values),
            ),
        )

    async def _start_run(self, client: httpx.AsyncClient) -> dict[str, Any]:
        try:
            response = await client.post(
                f"{APIFY_API_BASE}/acts/{self.actor_id}/runs",
                headers={"Authorization": f"Bearer {self.api_token}"},
                params={"waitForFinish": 120},
                json=harvest_search_request(self.total_posts, DEFAULT_QUERIES),
                timeout=135,
            )
        except httpx.TimeoutException as exc:
            raise LinkedInCollectError("apify_timeout", "Apify actor run timed out") from exc
        if response.status_code == 402:
            raise LinkedInCollectError("apify_spend_limit", "Apify spend limit reached")
        if response.status_code == 403:
            raise LinkedInCollectError(
                "apify_http_error",
                "Apify run HTTP 403 (token, actor access, or unsupported build parameter)",
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LinkedInCollectError(
                "apify_http_error",
                f"Apify run HTTP {exc.response.status_code}",
            ) from exc
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise LinkedInCollectError("apify_http_error", "Apify run response was missing data")
        return data

    async def _dataset_items(
        self, client: httpx.AsyncClient, dataset_id: str
    ) -> list[dict[str, Any]]:
        response = await client.get(
            f"{APIFY_API_BASE}/datasets/{dataset_id}/items",
            headers={"Authorization": f"Bearer {self.api_token}"},
            params={"clean": "true"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise TypeError("Apify dataset response was not a list")
        return [record for record in payload if isinstance(record, dict)]


def normalize_post(record: dict[str, Any]) -> CanonicalItem | None:
    if record.get("type") not in {None, "post"}:
        return None
    post_id = str(record.get("id") or record.get("entityId") or record.get("shareUrn") or "")
    url = str(record.get("linkedinUrl") or record.get("shareLinkedinUrl") or "")
    text = str(record.get("content") or "")
    raw_author = record.get("author")
    author: dict[str, Any] = raw_author if isinstance(raw_author, dict) else {}
    raw_posted = record.get("postedAt")
    posted: dict[str, Any] = raw_posted if isinstance(raw_posted, dict) else {}
    if not post_id or not url or not text:
        return None

    return CanonicalItem(
        source=Source.LINKEDIN,
        item_id=post_id,
        company_name=None,
        canonical_url=url,
        content_text=text,
        description=text,
        founder_name=_first_string(author, "name"),
        founder_handle=_first_string(author, "publicIdentifier", "universalName"),
        author_url=_first_string(author, "linkedinUrl"),
        published_at=_date(
            _first_string(posted, "date", "timestamp", "iso", "dateTime")
        ),
        raw=record,
    )


def _first_string(value: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
