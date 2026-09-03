"""`/yc review`: replay queued review rows through the current classifier."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import patch

import pytest
from fastapi.responses import Response
from fastapi.testclient import TestClient

from yc_monitor.config import Settings
from yc_monitor.db import Database
from yc_monitor.models import (
    Alert,
    AlertKind,
    CanonicalItem,
    Classification,
    Source,
)
from yc_monitor.pipeline import MonitorPipeline, _item_from_review_row
from yc_monitor.pond_server import create_app
from yc_monitor.slack_app import handle_slash_command

ADMIN = "UADMIN"


def review_item(
    item_id: str = "tweet-1",
    company: str | None = None,
    text: str = "We got into YC S26",
) -> CanonicalItem:
    return CanonicalItem(
        Source.TWITTER,
        item_id,
        company,
        f"https://x.com/alice/status/{item_id}",
        content_text=text,
        description=text,
        founder_name="Alice",
        founder_handle="alice",
        raw={
            "id": item_id,
            "text": text,
            "author": {"name": "Alice", "userName": "alice"},
        },
    )


def seed_review(db: Database, item: CanonicalItem, reason: str = "gpt_review:needs_a_human") -> str:
    key = f"{item.source.value}:{item.item_id}"
    db.reserve_item(key, item, "review", reason)
    return key


class FakeClassifier:
    """Records items and replays one canned Classification per call."""

    def __init__(self, results: list[Classification]) -> None:
        self.results = list(results)
        self.items: list[CanonicalItem] = []
        self.cycles = 0

    def begin_cycle(self) -> None:
        self.cycles += 1

    async def classify(
        self,
        item: CanonicalItem,
        official_names: set[str],
        official_hosts: set[str],
        official_handles: set[str],
    ) -> Classification:
        self.items.append(item)
        if self.results:
            return self.results.pop(0)
        return Classification(None, "gpt_review:untouched", 0.0, persist=False)


def make_pipeline(tmp_path, classifier: FakeClassifier) -> MonitorPipeline:
    settings = Settings(
        database_path=str(tmp_path / "state.db"),
        openai_api_key=None,
        openai_max_calls_per_cycle=25,
        # Never let a developer's real .env Slack token deliver during tests.
        slack_bot_token=None,
        slack_channel_id=None,
        slack_ops_channel_id=None,
    )
    pipeline = MonitorPipeline(settings)
    pipeline.social_classifier = classifier  # type: ignore[assignment]
    return pipeline


def alert_for(item: CanonicalItem, company: str = "Harbor") -> Alert:
    return Alert(
        AlertKind.EARLY_FOUNDER,
        item,
        f"early:{company.lower()}",
        0.95,
    )


# --- db helpers --------------------------------------------------------------


def test_list_review_rows_returns_only_review_rows(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    seeded = seed_review(db, review_item("tweet-1", "Harbor"))
    seed_review(db, review_item("tweet-2"))
    db.reserve_item("twitter:tweet-3", review_item("tweet-3"), "rejected", "noise")
    db.reserve_item("twitter:tweet-4", review_item("tweet-4"), "alerted", "gpt_confirmed")

    rows = db.list_review_rows(25)
    assert [row["dedup_key"] for row in rows] == [seeded, "twitter:tweet-2"]
    # payload stays raw JSON; parsing is the caller's job
    assert json.loads(str(rows[0]["payload"]))["author"]["userName"] == "alice"
    assert rows[0]["reason"] == "gpt_review:needs_a_human"


def test_list_review_rows_is_bounded_and_ordered_oldest_first(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    for index in range(5):
        seed_review(db, review_item(f"tweet-{index}"))

    rows = db.list_review_rows(3)
    assert [row["item_id"] for row in rows] == ["tweet-0", "tweet-1", "tweet-2"]


def test_resolve_review_flips_disposition_and_keeps_old_reason(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    key = seed_review(db, review_item("tweet-1"), reason="gpt_review:invalid_batch")

    assert db.resolve_review(key, "rejected", None) is True
    with db.connect() as connection:
        row = connection.execute(
            "SELECT disposition, reason FROM seen_items WHERE dedup_key=?", (key,)
        ).fetchone()
    assert str(row["disposition"]) == "rejected"
    assert str(row["reason"]) == "gpt_review:invalid_batch"


def test_resolve_review_overwrites_reason_when_given(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    key = seed_review(db, review_item("tweet-1"), reason="gpt_review:invalid_batch")

    assert db.resolve_review(key, "evidence", "gpt_confirmed:fresh look") is True
    with db.connect() as connection:
        row = connection.execute(
            "SELECT disposition, reason FROM seen_items WHERE dedup_key=?", (key,)
        ).fetchone()
    assert str(row["disposition"]) == "evidence"
    assert str(row["reason"]) == "gpt_confirmed:fresh look"


def test_resolve_review_returns_false_for_non_review_row(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    item = review_item("tweet-1")
    db.reserve_item("twitter:tweet-1", item, "rejected", "noise")

    assert db.resolve_review("twitter:tweet-1", "evidence", None) is False
    assert db.resolve_review("twitter:missing", "evidence", None) is False


# --- row -> CanonicalItem rebuild -------------------------------------------


def test_review_row_rebuilds_author_and_text() -> None:
    row = {
        "dedup_key": "twitter:tweet-1",
        "source": "twitter",
        "item_id": "tweet-1",
        "company_name": "Harbor",
        "canonical_url": "https://x.com/alice/status/tweet-1",
        "reason": "gpt_review:invalid_batch",
        "first_seen_at": "2026-09-01T00:00:00+00:00",
        "payload": json.dumps(
            {
                "id": "tweet-1",
                "text": "We got into YC S26",
                "author": {"name": "Alice", "userName": "@Alice"},
            }
        ),
    }

    item = _item_from_review_row(row)
    assert item is not None
    assert item.source == Source.TWITTER
    assert item.item_id == "tweet-1"
    assert item.company_name == "Harbor"
    assert item.content_text == "We got into YC S26"
    assert item.founder_name == "Alice"
    assert item.founder_handle == "alice"  # @ stripped and lowercased
    assert item.raw["text"] == "We got into YC S26"


def test_review_row_rebuilds_linkedin_author() -> None:
    row = {
        "dedup_key": "linkedin:post-1",
        "source": "linkedin",
        "item_id": "post-1",
        "company_name": None,
        "canonical_url": "https://www.linkedin.com/feed/update/urn:li:activity:post-1",
        "reason": "gpt_review:invalid_batch",
        "first_seen_at": "2026-09-01T00:00:00+00:00",
        "payload": json.dumps(
            {
                "id": "post-1",
                "content": "We just got into YC S26",
                "author": {"name": "Alice", "publicIdentifier": "alice-li"},
            }
        ),
    }

    item = _item_from_review_row(row)
    assert item is not None
    assert item.source == Source.LINKEDIN
    assert item.content_text == "We just got into YC S26"
    assert item.founder_handle == "alice-li"


def test_review_row_with_bad_source_or_payload_is_skipped() -> None:
    bad_source = {
        "dedup_key": "newsletter:1",
        "source": "newsletter",
        "item_id": "1",
        "company_name": None,
        "canonical_url": "https://example.com/1",
        "payload": "{}",
    }
    assert _item_from_review_row(bad_source) is None

    garbage_payload = {
        "dedup_key": "twitter:2",
        "source": "twitter",
        "item_id": "2",
        "company_name": None,
        "canonical_url": "https://x.com/a/status/2",
        "payload": "not json",
    }
    item = _item_from_review_row(garbage_payload)
    assert item is not None
    assert item.content_text == ""
    assert item.founder_handle is None


# --- pipeline rejudge -------------------------------------------------------


@pytest.mark.asyncio
async def test_rejudge_promotes_alert_and_posts_company_alert(tmp_path) -> None:
    item = review_item("tweet-1", "Harbor", "We got into YC S26 building Harbor")
    db = Database(str(tmp_path / "state.db"))
    key = seed_review(db, item)
    classifier = FakeClassifier(
        [Classification(alert_for(item), "gpt_confirmed:acceptance", 0.95)]
    )
    pipeline = make_pipeline(tmp_path, classifier)
    pipeline.db = db

    result = await pipeline.rejudge_review_queue(25)

    assert result["reviewed"] == 1
    assert result["promoted"] == [key]
    assert result["promoted_alerts"] == ["early:harbor"]
    assert result["cleared"] == 0
    assert result["deferred"] == 0
    with db.connect() as connection:
        disposition = connection.execute(
            "SELECT disposition FROM seen_items WHERE dedup_key=?", (key,)
        ).fetchone()
    assert str(disposition["disposition"]) == "evidence"
    # The company alert row and its outbox payload are queued (never delivered:
    # the test pipeline is built without Slack credentials).
    assert db.has_seen_item("early:harbor")
    assert db.outbox_status("early:harbor") == "pending"


@pytest.mark.asyncio
async def test_rejudge_delivers_promoted_alert_through_outbox(tmp_path) -> None:
    item = review_item("tweet-1", "Harbor", "We got into YC S26 building Harbor")
    db = Database(str(tmp_path / "state.db"))
    seed_review(db, item)
    classifier = FakeClassifier(
        [Classification(alert_for(item), "gpt_confirmed:acceptance", 0.95)]
    )
    pipeline = make_pipeline(tmp_path, classifier)
    pipeline.db = db
    delivered: list[str] = []

    async def fake_send(
        alert, demo: bool = False, thread_ts: str | None = None
    ) -> dict[str, str]:
        delivered.append(alert.dedup_key)
        return {"channel": "C123", "ts": "1.0"}

    pipeline.notifier.send = fake_send  # type: ignore[method-assign]
    result = await pipeline.rejudge_review_queue(25)
    assert delivered == ["early:harbor"]
    assert result["delivered"] == 1


@pytest.mark.asyncio
async def test_rejudge_clears_firm_reject(tmp_path) -> None:
    item = review_item("tweet-1", "Harbor", "Almanac YC S26 launches an AI agent")
    db = Database(str(tmp_path / "state.db"))
    key = seed_review(db, item)
    classifier = FakeClassifier(
        [Classification(None, "gpt_rejected:third_party_news", 0.9, persist=True)]
    )
    pipeline = make_pipeline(tmp_path, classifier)
    pipeline.db = db

    result = await pipeline.rejudge_review_queue(25)
    assert result["reviewed"] == 1
    assert result["cleared"] == 1
    assert result["promoted"] == []
    assert result["deferred"] == 0
    with db.connect() as connection:
        row = connection.execute(
            "SELECT disposition FROM seen_items WHERE dedup_key=?", (key,)
        ).fetchone()
    assert str(row["disposition"]) == "rejected"
    assert db.status()["outbox"] == {}


@pytest.mark.asyncio
async def test_rejudge_defers_and_leaves_row_queued(tmp_path) -> None:
    item = review_item("tweet-1", "Harbor", "We got into YC S26")
    db = Database(str(tmp_path / "state.db"))
    key = seed_review(db, item, reason="gpt_review:unresolved_company_handle")
    classifier = FakeClassifier(
        [Classification(None, "gpt_cycle_budget_exhausted", 0.0, persist=False)]
    )
    pipeline = make_pipeline(tmp_path, classifier)
    pipeline.db = db

    result = await pipeline.rejudge_review_queue(25)
    assert result["reviewed"] == 1
    assert result["deferred"] == 1
    assert result["cleared"] == 0
    assert result["promoted"] == []
    with db.connect() as connection:
        row = connection.execute(
            "SELECT disposition, reason FROM seen_items WHERE dedup_key=?", (key,)
        ).fetchone()
    assert str(row["disposition"]) == "review"
    assert str(row["reason"]) == "gpt_review:unresolved_company_handle"


@pytest.mark.asyncio
async def test_rejudge_skips_already_archived_early_noise(tmp_path) -> None:
    item = review_item("tweet-1", "Box", "we just got into the YC F26 batch to build box")
    db = Database(str(tmp_path / "state.db"))
    seed_review(db, item)
    # The company's early key was already archived as known noise by the
    # startup migration, so reserve_alert must refuse a fresh alert row.
    db.reserve_item("early:box", item, "archived", "verified false positive")
    classifier = FakeClassifier(
        [Classification(alert_for(item, "box"), "gpt_confirmed:acceptance", 0.95)]
    )
    pipeline = make_pipeline(tmp_path, classifier)
    pipeline.db = db

    result = await pipeline.rejudge_review_queue(25)
    assert result["promoted"] == []
    assert result["promoted_alerts"] == []
    assert db.outbox_status("early:box") is None
    with db.connect() as connection:
        row = connection.execute(
            "SELECT disposition FROM seen_items WHERE dedup_key='twitter:tweet-1'"
        ).fetchone()
    assert str(row["disposition"]) == "evidence"


@pytest.mark.asyncio
async def test_rejudge_survives_one_bad_item_and_respects_cap(tmp_path) -> None:
    good = review_item("tweet-1", "Harbor", "We got into YC S26")
    other = review_item("tweet-2", "Gamma", "We got into YC S26")
    db = Database(str(tmp_path / "state.db"))
    seed_review(db, good)
    seed_review(db, other)

    class ExplodingThenFine(FakeClassifier):
        async def classify(
            self,
            item: CanonicalItem,
            official_names: set[str],
            official_hosts: set[str],
            official_handles: set[str],
        ) -> Classification:
            if item.item_id == "tweet-1":
                raise RuntimeError("bad payload")
            return await super().classify(item, official_names, official_hosts, official_handles)

    classifier = ExplodingThenFine(
        [Classification(None, "gpt_rejected:noise", 0.9, persist=True)]
    )
    pipeline = make_pipeline(tmp_path, classifier)
    pipeline.db = db
    pipeline.settings.openai_max_calls_per_cycle = 25

    result = await pipeline.rejudge_review_queue(25)
    assert result["reviewed"] == 1
    assert result["cleared"] == 1
    assert result["deferred"] == 1
    assert classifier.cycles == 1


@pytest.mark.asyncio
async def test_rejudge_stops_classifying_once_cycle_cap_is_hit(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    for index in range(3):
        seed_review(db, review_item(f"tweet-{index}"))
    # A firm reject for the two rows the cap allows; the third is never classified.
    classifier = FakeClassifier(
        [Classification(None, "gpt_rejected:noise", 0.9, persist=True) for _ in range(2)]
    )
    pipeline = make_pipeline(tmp_path, classifier)
    pipeline.db = db
    pipeline.settings.openai_max_calls_per_cycle = 2

    result = await pipeline.rejudge_review_queue(25)
    assert result["reviewed"] == 2
    assert result["cleared"] == 2
    assert result["deferred"] == 1
    assert len(classifier.items) == 2
    with db.connect() as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM seen_items WHERE disposition='review'"
        ).fetchone()[0]
    assert int(remaining) == 1


@pytest.mark.asyncio
async def test_rejudge_uses_official_identities_from_db(tmp_path) -> None:
    item = review_item("tweet-1", "Almanac", "We got into YC S26 building Almanac")
    db = Database(str(tmp_path / "state.db"))
    seed_review(db, item)
    db.upsert_yc_company("almanac", "almanac", "Almanac", "usealmanac.com", [], {})
    seen: list[tuple[set[str], set[str], set[str]]] = []

    class RecordingClassifier(FakeClassifier):
        async def classify(
            self,
            item: CanonicalItem,
            official_names: set[str],
            official_hosts: set[str],
            official_handles: set[str],
        ) -> Classification:
            seen.append((official_names, official_hosts, official_handles))
            return await super().classify(item, official_names, official_hosts, official_handles)

    pipeline = make_pipeline(tmp_path, RecordingClassifier([]))
    pipeline.db = db
    await pipeline.rejudge_review_queue(25)
    names, hosts, handles = seen[0]
    assert "almanac" in names
    assert "usealmanac.com" in hosts
    assert handles == set()


@pytest.mark.asyncio
async def test_rejudge_with_empty_queue_is_a_noop(tmp_path) -> None:
    pipeline = make_pipeline(tmp_path, FakeClassifier([]))
    result = await pipeline.rejudge_review_queue(25)
    assert result == {
        "reviewed": 0,
        "promoted": [],
        "cleared": 0,
        "deferred": 0,
        "promoted_alerts": [],
        "promoted_names": [],
        "delivered": 0,
    }


# --- slash command ----------------------------------------------------------


def test_review_slash_command_is_admin_gated() -> None:
    response = handle_slash_command("/yc", "review", {}, user_id="USTRANGER", admin_users={ADMIN})
    assert response["text"] == "Only admins can trigger scans (they spend API budget)."


def test_review_slash_command_allowed_for_admin_and_when_unconfigured() -> None:
    admin = handle_slash_command("/yc", "review", {}, user_id=ADMIN, admin_users={ADMIN})
    assert admin["text"] == "review_requested"
    assert admin["response_type"] == "ephemeral"
    open_ = handle_slash_command("/yc", "review", {}, user_id="UANYONE", admin_users=None)
    assert open_["text"] == "review_requested"


def test_help_mentions_review_command() -> None:
    response = handle_slash_command("/yc", "help", {}, admin_users={ADMIN})
    assert "`/yc review`" in str(response["text"])


# --- route wiring -----------------------------------------------------------


def _signed_post(client: TestClient, secret: str, body: bytes) -> Response:
    timestamp = str(int(time.time()))
    basestring = f"v0:{timestamp}:{body.decode()}".encode()
    signature = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return client.post(
        "/slack/commands",
        content=body,
        headers={
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )


def test_route_rejudges_review_queue_and_summarizes(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "pond.db"),
        slack_signing_secret="route-secret",
        scheduler_run_immediately=False,
        slack_bot_token=None,
        slack_channel_id=None,
        slack_ops_channel_id=None,
        openai_api_key=None,
    )
    db = Database(settings.database_path)
    seed_review(db, review_item("tweet-1", "Harbor"))

    with patch.object(MonitorPipeline, "rejudge_review_queue") as rejudge:
        rejudge.return_value = {
            "reviewed": 1,
            "promoted": ["twitter:tweet-1"],
            "cleared": 0,
            "deferred": 0,
            "promoted_alerts": ["early:harbor"],
            "promoted_names": ["Harbor"],
            "delivered": 1,
        }
        client = TestClient(create_app(settings))
        response = _signed_post(client, "route-secret", b"command=/yc&text=review&user_id=U1")

    assert response.status_code == 200
    body = response.json()
    assert body["response_type"] == "ephemeral"
    assert "Re-reviewed 1 queued post(s)" in body["text"]
    assert "1 promoted (and sent)" in body["text"]
    assert "New alerts: Harbor" in body["text"]
    rejudge.assert_awaited_once_with(25)
