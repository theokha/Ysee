import json
import unittest.mock
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from yc_monitor.adapters import linkedin as linkedin_module
from yc_monitor.adapters.linkedin import (
    APIFY_API_BASE,
    DEFAULT_QUERIES,
    LinkedInAdapter,
    LinkedInCollectError,
    allocate_query_max_posts,
    harvest_search_request,
    normalize_post,
)
from yc_monitor.adapters.twitter import (
    SEARCH_URL,
    TwitterAdapter,
    build_search_queries,
    parse_tweet_timestamp,
)
from yc_monitor.adapters.yc_directory import (
    CATALOG_URL,
    LATEST_CHANGES_URL,
    YCDirectoryAdapter,
    extract_founder_handles,
)
from yc_monitor.adapters.yc_launches import LAUNCHES_URL, YCLaunchesAdapter, normalize_launch
from yc_monitor.adapters.yc_speedrun import (
    OFFICIAL_API_URL,
    YCSpeedrunAdapter,
    normalize_speedrun_company,
)
from yc_monitor.models import HealthStatus

FIXTURES = Path(__file__).parent / "fixtures"


def test_normalize_official_a16z_speedrun_company() -> None:
    item = normalize_speedrun_company({
        "id": "company-1",
        "slug": "game-co",
        "name": "Game Co",
        "cohort": "SR007",
        "preamble": "AI games",
        "website_url": "https://game.example",
        "founder_set": [{"first_name": "Jane", "last_name": "Doe"}],
    })
    assert item
    assert item.item_id == "company-1"
    assert item.company_name == "Game Co"
    assert item.canonical_url == "https://speedrun.a16z.com/companies/game-co"
    assert item.batch == "a16z SR007"
    assert item.founder_name == "Jane Doe"


def test_twitter_parses_live_created_at_and_iso() -> None:
    live = parse_tweet_timestamp("Mon Aug 31 21:54:24 +0000 2026")
    assert live == datetime(2026, 8, 31, 21, 54, 24, tzinfo=UTC)
    iso = parse_tweet_timestamp("2026-08-31T21:54:24Z")
    assert iso == datetime(2026, 8, 31, 21, 54, 24, tzinfo=UTC)
    naive_iso = parse_tweet_timestamp("2026-08-31T21:54:24")
    assert naive_iso == datetime(2026, 8, 31, 21, 54, 24, tzinfo=UTC)
    assert parse_tweet_timestamp("not-a-date") is None


def test_twitter_query_pack_is_focused_and_bounded() -> None:
    queries = build_search_queries("F26,W27,S27")
    joined = " ".join(queries)
    assert len(queries) == 6
    assert '"got into YC"' in queries[0]
    assert '"accepted into YC"' in queries[0]
    assert '"got into Y Combinator"' in queries[1]
    assert "launching" in queries[2]
    assert '"backed by Y Combinator"' in queries[3]
    assert '"YC Speedrun"' in queries[4]
    assert '"YC F26"' in queries[5]
    assert '"YC W27"' in queries[5]
    assert '"YC S27"' in queries[5]
    assert "NEAR" not in joined
    assert "since:" not in joined
    empty = build_search_queries("")
    assert len(empty) == 5
    assert all("YC F26" not in query for query in empty)


@pytest.mark.asyncio
@respx.mock
async def test_twitter_original_normalization() -> None:
    payload = json.loads((FIXTURES / "twitter_advanced_search.json").read_text())
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=payload))
    async with httpx.AsyncClient() as client:
        result = await TwitterAdapter("secret", 1, 7, "F26").collect(client)
    assert result.health.status == HealthStatus.OK
    assert result.items[0].founder_handle == "alice"
    assert result.items[0].published_at == datetime(2026, 8, 31, 21, 54, 24, tzinfo=UTC)
    calls = list(respx.calls)
    assert len(calls) == 6
    for call in calls:
        query = call.request.url.params["query"]
        assert "since_time:" in query
        assert call.request.url.params["queryType"] == "Latest"


@pytest.mark.asyncio
@respx.mock
async def test_yc_directory_probes_latest_changes() -> None:
    catalog = [{
        "slug": "almanac",
        "name": "Almanac",
        "website": "https://usealmanac.com",
        "one_liner": "AI agents",
        "batch": "Summer 2026",
        "url": "https://www.ycombinator.com/companies/almanac",
        "launched_at": 1754000000,
    }]
    latest = json.loads((FIXTURES / "yc_latest_changes.json").read_text())
    respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=catalog))
    respx.get(LATEST_CHANGES_URL).mock(return_value=httpx.Response(200, json=latest))
    async with httpx.AsyncClient() as client:
        result = await YCDirectoryAdapter().collect(client)
    assert result.health.status == HealthStatus.OK
    assert result.items[0].company_name == "Almanac"
    assert result.items[0].raw["_latest_changes_added"] is False
    assert "latest-changes probe ok" in result.health.detail
    assert "generated_at=2026-08-31T02:27:34.876Z" in result.health.detail
    assert "added=1" in result.health.detail


@pytest.mark.asyncio
@respx.mock
async def test_yc_directory_marks_only_latest_added_slugs() -> None:
    catalog = [
        {"slug": "almanac", "name": "Almanac", "launched_at": 1646585799},
        {"slug": "redoubt-insurance", "name": "Redoubt Insurance", "launched_at": 1754000000},
    ]
    latest = json.loads((FIXTURES / "yc_latest_changes.json").read_text())
    respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=catalog))
    respx.get(LATEST_CHANGES_URL).mock(return_value=httpx.Response(200, json=latest))
    async with httpx.AsyncClient() as client:
        result = await YCDirectoryAdapter().collect(client)
    flags = {item.item_id: item.raw["_latest_changes_added"] for item in result.items}
    assert flags == {"almanac": False, "redoubt-insurance": True}


@pytest.mark.asyncio
@respx.mock
async def test_yc_directory_survives_failed_latest_probe() -> None:
    respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=[{
        "slug": "almanac", "name": "Almanac", "url": "https://www.ycombinator.com/companies/almanac",
    }]))
    respx.get(LATEST_CHANGES_URL).mock(return_value=httpx.Response(500, json={"error": "nope"}))
    async with httpx.AsyncClient() as client:
        result = await YCDirectoryAdapter().collect(client)
    assert result.health.status == HealthStatus.OK
    assert result.items[0].company_name == "Almanac"
    assert result.items[0].raw["_latest_changes_added"] is False
    assert "latest-changes probe failed" in result.health.detail


def test_normalize_launch_yc_hit() -> None:
    item = normalize_launch({
        "id": 113577,
        "title": "GetCrux: The AI Agents for Paid Social",
        "tagline": "Go from insights to live ads in minutes",
        "slug": "TXt-getcrux-the-ai-agents-for-paid-social",
        "created_at": "2026-09-02T19:48:04.501Z",
        "search_path": "https://www.ycombinator.com/launches/TXt-getcrux-the-ai-agents-for-paid-social",
        "company": {"name": "GetCrux", "slug": "getcrux", "url": "http://getcrux.ai/", "batch": "Winter 2024"},
    })
    assert item
    assert item.item_id == "113577"
    assert item.company_name == "GetCrux"
    assert item.batch == "Winter 2024"
    assert item.canonical_url.endswith("getcrux-the-ai-agents-for-paid-social")


@pytest.mark.asyncio
@respx.mock
async def test_yc_launches_adapter_fetches_hits() -> None:
    respx.get(LAUNCHES_URL).mock(return_value=httpx.Response(200, json={
        "hits": [{
            "id": 1,
            "title": "Acme",
            "tagline": "Widgets",
            "created_at": "2026-09-02T19:48:04.501Z",
            "search_path": "https://www.ycombinator.com/launches/acme",
            "company": {"name": "Acme", "slug": "acme", "batch": "Fall 2026"},
        }],
        "nbHits": 1,
    }))
    async with httpx.AsyncClient() as client:
        result = await YCLaunchesAdapter().collect(client)
    assert result.health.status == HealthStatus.OK
    assert result.items[0].company_name == "Acme"


def test_extract_founder_handles_ignores_generic_names() -> None:
    payload = json.loads((FIXTURES / "yc_company_with_founders.json").read_text())
    assert extract_founder_handles(payload) == ["janedoe"]
    assert extract_founder_handles({"name": "Almanac", "founders": [{"name": "Jane"}]}) == []
    assert extract_founder_handles({"twitter": "https://x.com/intent/tweet"}) == []


@pytest.mark.asyncio
@respx.mock
async def test_speedrun_fetches_paginated_official_a16z_api() -> None:
    route = respx.get(OFFICIAL_API_URL).mock(side_effect=[
        httpx.Response(200, json={
            "count": 2,
            "next": "next",
            "previous": None,
            "results": [{
                "id": "company-1", "slug": "alpha", "name": "Alpha",
                "cohort": "SR007", "preamble": "One", "founder_set": [],
            }],
        }),
        httpx.Response(200, json={
            "count": 2,
            "next": None,
            "previous": "previous",
            "results": [{
                "id": "company-2", "slug": "beta", "name": "Beta",
                "cohort": "SR006", "preamble": "Two", "founder_set": [],
            }],
        }),
    ])
    async with httpx.AsyncClient() as client:
        result = await YCSpeedrunAdapter().collect(client)
    assert route.call_count == 2
    assert result.health.status == HealthStatus.OK
    assert [item.company_name for item in result.items] == ["Alpha", "Beta"]
    assert "Official a16z Speedrun" in result.health.detail


@pytest.mark.asyncio
async def test_speedrun_can_be_disabled_explicitly() -> None:
    async with httpx.AsyncClient() as client:
        result = await YCSpeedrunAdapter(None).collect(client)
    assert result.health.status == HealthStatus.SKIPPED
    assert "disabled by configuration" in result.health.detail
    assert result.items == []


def test_linkedin_actor_result_normalization() -> None:
    item = normalize_post({
        "type": "post",
        "id": "7330988768578920448",
        "linkedinUrl": "https://www.linkedin.com/posts/alice_123",
        "content": "We got into YC S26!",
        "author": {
            "name": "Alice",
            "linkedinUrl": "https://linkedin.com/in/alice",
            "publicIdentifier": "alice",
        },
        "postedAt": {"date": "2026-08-31T12:00:00+00:00"},
    })
    assert item
    assert item.item_id == "7330988768578920448"
    assert item.founder_name == "Alice"
    assert item.founder_handle == "alice"
    assert item.published_at is not None


@pytest.mark.asyncio
@respx.mock
async def test_linkedin_apify_actor_contract() -> None:
    adapter = LinkedInAdapter("token", total_posts=2)
    recent = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_route = respx.post(f"{APIFY_API_BASE}/acts/{adapter.actor_id}/runs").mock(
        return_value=httpx.Response(200, json={"data": {
            "status": "SUCCEEDED",
            "buildId": adapter.build_id,
            "defaultDatasetId": "dataset-1",
        }})
    )
    dataset_route = respx.get(f"{APIFY_API_BASE}/datasets/dataset-1/items").mock(
        return_value=httpx.Response(200, json=[{
            "type": "post",
            "id": "p1",
            "linkedinUrl": "https://linkedin.com/posts/p1",
            "content": "We got into YC S26",
            "author": {"name": "Alice"},
            "postedAt": {"date": recent},
        }])
    )
    async with httpx.AsyncClient() as client:
        result = await adapter.collect(client)
    assert run_route.call_count == 1
    assert "build" not in run_route.calls[0].request.url.params
    request_payload = json.loads(run_route.calls[0].request.content.decode())
    assert request_payload["postedLimit"] == "week"
    assert request_payload["scrapeComments"] is False
    assert request_payload["scrapeReactions"] is False
    assert request_payload["maxPosts"] * len(request_payload["searchQueries"]) <= adapter.total_posts
    assert dataset_route.call_count == 1
    assert [value.item_id for value in result.items] == ["p1"]
    assert result.health.status == HealthStatus.OK


@pytest.mark.asyncio
@respx.mock
async def test_linkedin_window_drops_stale_posts() -> None:
    """Client-side window: the actor returns a week, only window_hours survive."""

    def record(post_id: str, hours_ago: float) -> dict[str, Any]:
        stamp = datetime.now(UTC) - timedelta(hours=hours_ago)
        return {
            "type": "post",
            "id": post_id,
            "linkedinUrl": f"https://linkedin.com/posts/{post_id}",
            "content": f"We got into YC S26 ({post_id})",
            "author": {"name": "Alice"},
            "postedAt": {"date": stamp.strftime("%Y-%m-%dT%H:%M:%SZ")},
        }

    adapter = LinkedInAdapter("token", total_posts=10, window_hours=1)
    respx.post(f"{APIFY_API_BASE}/acts/{adapter.actor_id}/runs").mock(
        return_value=httpx.Response(200, json={"data": {
            "status": "SUCCEEDED",
            "buildId": adapter.build_id,
            "defaultDatasetId": "dataset-1",
        }})
    )
    respx.get(f"{APIFY_API_BASE}/datasets/dataset-1/items").mock(
        return_value=httpx.Response(
            200, json=[record("fresh", 0.5), record("stale", 3), record("edge", 0.9)]
        )
    )
    async with httpx.AsyncClient() as client:
        result = await adapter.collect(client)
    assert [value.item_id for value in result.items] == ["fresh", "edge"]
    assert all(value.published_at is not None for value in result.items)


def test_linkedin_default_window_matches_config() -> None:
    assert LinkedInAdapter("token").window_hours == 36
    assert linkedin_module.LINKEDIN_WINDOW_HOURS_DEFAULT == 36


def _actor_record(entry: dict[str, Any]) -> dict[str, Any]:
    """Map a saved HarvestAPI field sample onto the actor record shape."""
    return {
        "type": "post",
        "id": entry["id"],
        "linkedinUrl": entry["url"],
        "content": entry["content"],
        "author": {"name": entry["author_name"], "publicIdentifier": entry["author_handle"]},
        "postedAt": {"date": entry["posted_at"]},
    }


class _FrozenDatetime(datetime):
    """datetime stand-in so the fixture window test cannot age out."""

    frozen = datetime(2026, 1, 1, tzinfo=UTC)

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return cls.frozen if tz is not None else cls.frozen.replace(tzinfo=None)


@pytest.mark.asyncio
@respx.mock
async def test_linkedin_fixture_window_filtering() -> None:
    entries: list[dict[str, Any]] = json.loads(
        (FIXTURES / "linkedin_harvest_sample.json").read_text()
    )
    records = [_actor_record(entry) for entry in entries]
    timestamps = [datetime.fromisoformat(entry["posted_at"]) for entry in entries]
    _FrozenDatetime.frozen = max(timestamps) + timedelta(minutes=5)

    async def collect(window_hours: int) -> list[Any]:
        adapter = LinkedInAdapter("token", total_posts=100, window_hours=window_hours)
        respx.reset()
        respx.post(f"{APIFY_API_BASE}/acts/{adapter.actor_id}/runs").mock(
            return_value=httpx.Response(200, json={"data": {
                "status": "SUCCEEDED",
                "buildId": adapter.build_id,
                "defaultDatasetId": "dataset-1",
            }})
        )
        respx.get(f"{APIFY_API_BASE}/datasets/dataset-1/items").mock(
            return_value=httpx.Response(200, json=records)
        )
        with unittest.mock.patch.object(linkedin_module, "datetime", _FrozenDatetime):
            async with httpx.AsyncClient() as client:
                result = await adapter.collect(client)
        assert result.health.status == HealthStatus.OK
        return result.items

    everything = await collect(100000)
    assert len(everything) > 30
    assert all(item.published_at is not None for item in everything)

    expected = {
        entry["id"]
        for entry, stamp in zip(entries, timestamps, strict=True)
        if stamp >= _FrozenDatetime.frozen - timedelta(hours=1)
    }
    assert expected
    recent = await collect(1)
    assert {item.item_id for item in recent} == expected
    assert len(recent) < len(everything)


def test_linkedin_cycle_budget_is_allocated_not_multiplied() -> None:
    allocations = allocate_query_max_posts(50, len(DEFAULT_QUERIES))
    assert allocations == [17, 17, 16]
    assert sum(allocations) == 50
    payload = harvest_search_request(50)
    assert payload["searchQueries"] == list(DEFAULT_QUERIES)
    assert payload["maxPosts"] == 16
    assert payload["maxPosts"] * len(payload["searchQueries"]) == 48
    assert payload["maxPosts"] != 50
    small = harvest_search_request(2)
    assert small["maxPosts"] * len(small["searchQueries"]) == 2
    assert len(small["searchQueries"]) == 2


@pytest.mark.asyncio
@respx.mock
async def test_linkedin_rejects_schema_drift() -> None:
    adapter = LinkedInAdapter("token", total_posts=2)
    respx.post(f"{APIFY_API_BASE}/acts/{adapter.actor_id}/runs").mock(
        return_value=httpx.Response(200, json={"data": {
            "status": "SUCCEEDED",
            "buildId": "unexpected-build",
            "defaultDatasetId": "dataset-1",
        }})
    )
    respx.get(f"{APIFY_API_BASE}/datasets/dataset-1/items").mock(
        return_value=httpx.Response(200, json=[{"unexpected": "shape"}])
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(LinkedInCollectError, match="apify_schema_drift"):
            await adapter.collect(client)


@pytest.mark.asyncio
@respx.mock
async def test_linkedin_failed_actor_status_is_coded() -> None:
    adapter = LinkedInAdapter("token", total_posts=2)
    respx.post(f"{APIFY_API_BASE}/acts/{adapter.actor_id}/runs").mock(
        return_value=httpx.Response(200, json={"data": {
            "status": "FAILED",
            "buildId": adapter.build_id,
            "defaultDatasetId": "dataset-1",
        }})
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(LinkedInCollectError, match="apify_actor_failed"):
            await adapter.collect(client)
