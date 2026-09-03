"""Audit lineage and correctness fixes from the false-negative/false-positive review."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from yc_monitor.config import Settings
from yc_monitor.db import Database
from yc_monitor.gpt_classify import GPTSocialClassifier
from yc_monitor.models import (
    AdapterHealth,
    Alert,
    AlertKind,
    CanonicalItem,
    Classification,
    CollectionResult,
    HealthStatus,
    Source,
)
from yc_monitor.pipeline import MonitorPipeline, ingest_yc_directory


def social_item(item_id: str = "tweet-1", text: str = "We got into YC S26") -> CanonicalItem:
    return CanonicalItem(
        Source.TWITTER,
        item_id,
        None,
        f"https://x.com/alice/status/{item_id}",
        content_text=text,
        description=text,
        founder_name="Alice",
        founder_handle="alice",
        published_at=datetime(2026, 9, 2, tzinfo=UTC),
        raw={"id": item_id, "text": text, "author": {"name": "Alice", "userName": "alice"}},
    )


class FakeClassifier:
    def __init__(self, results: list[Classification]) -> None:
        self.results = list(results)

    def begin_cycle(self) -> None: ...

    async def classify(self, item, names, hosts, handles) -> Classification:
        if self.results:
            return self.results.pop(0)
        return Classification(None, "gpt_review:untouched", 0.0, persist=False)


def make_pipeline(tmp_path, classifier) -> MonitorPipeline:
    settings = Settings(
        database_path=str(tmp_path / "state.db"),
        openai_api_key=None,
        slack_bot_token=None,
        slack_channel_id=None,
        slack_ops_channel_id=None,
    )
    pipeline = MonitorPipeline(settings)
    pipeline.social_classifier = classifier  # type: ignore[assignment]
    return pipeline


def run_official_result(*items: CanonicalItem) -> CollectionResult:
    return CollectionResult(
        list(items),
        AdapterHealth(Source.YC_DIRECTORY, HealthStatus.OK, "test", len(items)),
    )


def yc_item(slug: str, name: str, *, launched_at: datetime | None = None) -> CanonicalItem:
    return CanonicalItem(
        Source.YC_DIRECTORY,
        slug,
        name,
        f"https://yc.test/{slug}",
        company_url=f"https://{slug}.test",
        published_at=launched_at or datetime.now(UTC),
        raw={"slug": slug, "name": name},
    )


# --- audit lineage -----------------------------------------------------------


@pytest.mark.asyncio
async def test_run_records_audit_decision_for_deferred_classification(tmp_path) -> None:
    """A capped/deferred post that never reaches seen_items must stay auditable."""
    classifier = FakeClassifier(
        [
            Classification(None, "gpt_cycle_budget_exhausted", 0.0, persist=False),
            Classification(None, "gpt_rejected:noise", 0.3, persist=True),
        ]
    )
    pipeline = make_pipeline(tmp_path, classifier)
    # Two candidates through the classify loop need a real social adapter; stub
    # the collection step instead of the whole adapters module.
    items = [social_item("t1"), social_item("t2")]

    class StubAdapter:
        source = Source.TWITTER

        async def collect(self, client):
            return CollectionResult(
                items, AdapterHealth(Source.TWITTER, HealthStatus.OK, "stub", len(items))
            )

    pipeline.official_adapters = []
    pipeline.social_adapters = [StubAdapter()]  # type: ignore[list-item]
    result = await pipeline.run()

    decisions = pipeline.db.audit_decisions_for_run(str(result["run_id"]))
    by_key = {(d["source"], d["item_id"]): d for d in decisions}
    deferred = by_key[("twitter", "t1")]
    assert deferred["stage"] == "classify"
    assert deferred["outcome"] == "deferred"
    assert deferred["reason"] == "gpt_cycle_budget_exhausted"
    assert deferred["persist"] == 0
    assert json.loads(deferred["payload"])["text"] == "We got into YC S26"
    rejected = by_key[("twitter", "t2")]
    assert rejected["outcome"] == "rejected"


@pytest.mark.asyncio
async def test_outbox_row_carries_run_id(tmp_path) -> None:
    alert = Alert(
        AlertKind.EARLY_FOUNDER, social_item("t1"), "early:harbor", 0.95
    )
    classifier = FakeClassifier([Classification(alert, "gpt_confirmed:x", 0.95)])
    pipeline = make_pipeline(tmp_path, classifier)

    class StubAdapter:
        source = Source.TWITTER

        async def collect(self, client):
            return CollectionResult(
                [social_item("t1")],
                AdapterHealth(Source.TWITTER, HealthStatus.OK, "stub", 1),
            )

    pipeline.official_adapters = []
    pipeline.social_adapters = [StubAdapter()]  # type: ignore[list-item]
    result = await pipeline.run()

    with pipeline.db.connect() as connection:
        row = connection.execute(
            "SELECT run_id FROM slack_outbox WHERE dedup_key='early:harbor'"
        ).fetchone()
    assert row is not None
    assert str(row["run_id"]) == result["run_id"]


# --- classifier per-item isolation ------------------------------------------


@pytest.mark.asyncio
async def test_one_exploding_item_does_not_fail_the_cycle(tmp_path) -> None:
    class ExplodingClassifier:
        def begin_cycle(self) -> None: ...

        async def classify(self, item, names, hosts, handles) -> Classification:
            if item.item_id == "t1":
                raise RuntimeError("unexpected")
            return Classification(None, "gpt_rejected:noise", 0.1)

    pipeline = make_pipeline(tmp_path, ExplodingClassifier())
    results = await pipeline._classify_social_items(
        [social_item("t1"), social_item("t2")], set(), set(), set()
    )
    assert results[0].reason == "classify_error:RuntimeError"
    assert results[0].persist is False
    assert results[1].reason == "gpt_rejected:noise"


# --- review-band thresholds --------------------------------------------------


def _classifier(**kwargs) -> GPTSocialClassifier:
    return GPTSocialClassifier(None, "test-model", 5.0, 0, 1, 0.5, 10, 0.9, **kwargs)


def test_review_band_defaults_to_min_confidence() -> None:
    classifier = _classifier()
    assert classifier.review_min_confidence == 0.5


def test_explicit_review_band_is_kept() -> None:
    classifier = _classifier(review_min_confidence=0.75)
    assert classifier.review_min_confidence == 0.75


def test_settings_threshold_ordering_flag() -> None:
    good = Settings(openai_min_confidence=0.6, openai_review_min_confidence=0.75,
                    openai_immediate_min_confidence=0.9)
    assert good.openai_thresholds_ordered is True
    inverted = Settings(openai_min_confidence=0.8, openai_review_min_confidence=0.7,
                        openai_immediate_min_confidence=0.9)
    assert inverted.openai_thresholds_ordered is False


# --- YC identity refresh ------------------------------------------------------


def test_existing_slug_with_new_founder_handle_refreshes_row(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    assert db.upsert_yc_company("acme", "acme", "Acme", "acme.test", [], {"slug": "acme"}) is True
    # Same name/host, but the catalog now exposes a founder handle. The old row
    # compared only name/host and skipped the update, so suppression sets built
    # from founder_handles stayed stale forever.
    changed = db.upsert_yc_company(
        "acme", "acme", "Acme", "acme.test", ["acmefounder"], {"slug": "acme"}
    )
    assert changed is False  # not a new slug
    _, _, handles = db.official_identities()
    assert handles == {"acmefounder"}


def test_unchanged_slug_still_skips_the_rewrite(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    db.upsert_yc_company("acme", "acme", "Acme", "acme.test", ["h1"], {"slug": "acme"})
    _, _, handles = db.official_identities()
    assert handles == {"h1"}
    # Identical second sync takes the cheap no-rewrite path.
    with db.connect() as connection:
        row = connection.execute(
            "SELECT last_seen_at FROM yc_companies WHERE slug='acme'"
        ).fetchone()
    assert row is not None


# --- ingest still behaves -----------------------------------------------------


def test_yc_ingest_new_slug_still_alerts_after_refresh_fix(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))

    ingest_yc_directory(db, run_official_result(yc_item("acme", "Acme")))
    alerts = ingest_yc_directory(
        db,
        run_official_result(yc_item("acme", "Acme"), yc_item("beta", "Beta")),
        now=datetime.now(UTC),
    )
    assert [a.dedup_key for a in alerts] == ["yc:beta"]


def test_old_listing_remains_ineligible(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    ingest_yc_directory(db, run_official_result(yc_item("acme", "Acme")))
    stale = yc_item("old", "OldCo", launched_at=datetime.now(UTC) - timedelta(days=30))
    alerts = ingest_yc_directory(db, run_official_result(yc_item("acme", "Acme"), stale))
    assert alerts == []
