from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from yc_monitor.config import Settings
from yc_monitor.db import Database
from yc_monitor.gpt_classify import suppress_official
from yc_monitor.models import AdapterHealth, CanonicalItem, CollectionResult, HealthStatus, Source
from yc_monitor.pipeline import MonitorPipeline, ingest_speedrun_directory, ingest_yc_directory
from yc_monitor.scheduler import (
    MONITOR_JOB_ID,
    build_scheduler,
    job_next_run_iso,
    schedule_first_run,
)


def yc_item(
    slug: str,
    name: str,
    *,
    added: bool = True,
    launched_at: datetime | None = datetime(2026, 9, 1, tzinfo=UTC),
) -> CanonicalItem:
    return CanonicalItem(
        Source.YC_DIRECTORY,
        slug,
        name,
        f"https://yc.test/{slug}",
        company_url=f"https://{slug}.test",
        published_at=launched_at,
        raw={"_latest_changes_added": added},
    )


def yc_result(*items: CanonicalItem, status: HealthStatus = HealthStatus.OK) -> CollectionResult:
    return CollectionResult(
        list(items),
        AdapterHealth(Source.YC_DIRECTORY, status, "test", len(items)),
    )


def test_legacy_yc_pending_rows_without_outbox_are_repaired(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    db = Database(str(path))
    item = yc_item("legacy", "Legacy")
    db.upsert_yc_company("legacy", "legacy", "Legacy", "legacy.test", [], {})
    db.reserve_item("yc:legacy", item, "pending")
    assert db.status()["seen"] == {"pending": 1}

    repaired = Database(str(path))
    assert repaired.status()["seen"] == {}
    assert repaired.is_yc_catalog_bootstrapped()


def test_speedrun_first_fetch_baselines_then_alerts_new_company(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    first = CanonicalItem(
        Source.YC_SPEEDRUN,
        "speed-1",
        "Speed One",
        "https://speedrun.a16z.com/companies/speed-one",
        batch="a16z SR007",
    )
    health = AdapterHealth(Source.YC_SPEEDRUN, HealthStatus.OK, "fixture", 1)
    assert ingest_speedrun_directory(db, CollectionResult([first], health)) == []
    assert db.has_seen_item("speedrun:speed one")
    assert db.status()["outbox"] == {}

    second = CanonicalItem(
        Source.YC_SPEEDRUN,
        "speed-2",
        "Speed Two",
        "https://speedrun.a16z.com/companies/speed-two",
        batch="a16z SR008",
    )
    alerts = ingest_speedrun_directory(db, CollectionResult([first, second], health))
    assert [alert.dedup_key for alert in alerts] == ["speedrun:speed two"]
    assert db.outbox_status("speedrun:speed two") == "pending"


def test_speedrun_listed_company_counts_as_official(tmp_path) -> None:
    """A Speedrun listing must suppress a later "not yet listed" social alert.

    Live regression: Baro was on speedrun.a16z.com 2026-09-02 and still alerted
    as unlisted on 2026-09-03, because official_identities() read yc_companies
    only and Speedrun ingest never writes that table.
    """
    db = Database(str(tmp_path / "state.db"))
    listed = CanonicalItem(
        Source.YC_SPEEDRUN,
        "baro",
        "Baro",
        "https://speedrun.a16z.com/companies/baro",
        batch="a16z SR007",
    )
    health = AdapterHealth(Source.YC_SPEEDRUN, HealthStatus.OK, "fixture", 1)
    ingest_speedrun_directory(db, CollectionResult([listed], health))

    names, _, _ = db.official_identities()
    assert "baro" in names

    tweet = CanonicalItem(
        Source.TWITTER,
        "tweet-baro",
        "Baro",
        "https://x.com/brianyoungilcho/status/1",
        content_text="I'm the co-founder and CEO of an a16z @speedrun-backed company",
        founder_handle="brianyoungilcho",
    )
    suppressed = suppress_official(tweet, names, set(), set())
    assert suppressed is not None
    assert suppressed.reason == "company_already_official"


def test_yc_catalog_names_still_suppress_after_speedrun_union(tmp_path) -> None:
    """Merging Speedrun names must not disturb the YC directory identities."""
    db = Database(str(tmp_path / "state.db"))
    db.upsert_yc_company(
        "almanac", "almanac", "Almanac", "usealmanac.com", ["janedoe"], {}, aliases=["almanac hq"]
    )
    speedrun = CanonicalItem(
        Source.YC_SPEEDRUN,
        "speed-1",
        "Speed One",
        "https://speedrun.a16z.com/companies/speed-one",
    )
    ingest_speedrun_directory(
        db,
        CollectionResult([speedrun], AdapterHealth(Source.YC_SPEEDRUN, HealthStatus.OK, "f", 1)),
    )

    names, hosts, handles = db.official_identities()
    assert {"almanac", "almanac hq", "speed one"} <= names
    assert hosts == {"usealmanac.com"}
    assert handles == {"janedoe"}


def test_speedrun_dry_run_does_not_consume_new_company(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    first = CanonicalItem(
        Source.YC_SPEEDRUN,
        "speed-1",
        "Speed One",
        "https://speedrun.a16z.com/companies/speed-one",
    )
    health = AdapterHealth(Source.YC_SPEEDRUN, HealthStatus.OK, "fixture", 1)
    ingest_speedrun_directory(db, CollectionResult([first], health))
    second = CanonicalItem(
        Source.YC_SPEEDRUN,
        "speed-2",
        "Speed Two",
        "https://speedrun.a16z.com/companies/speed-two",
    )
    preview = ingest_speedrun_directory(
        db, CollectionResult([first, second], health), enqueue=False
    )
    assert [alert.dedup_key for alert in preview] == ["speedrun:speed two"]
    assert not db.has_seen_item("speedrun:speed two")


def test_first_yc_catalog_fetch_baselines_without_alerts(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    alerts = ingest_yc_directory(db, yc_result(yc_item("acme", "Acme"), yc_item("beta", "Beta")))
    assert alerts == []
    assert db.is_yc_catalog_bootstrapped()
    assert db.status()["official_yc_companies"] == 2


def test_yc_dry_run_previews_without_mutating_snapshot(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    ingest_yc_directory(db, yc_result(yc_item("acme", "Acme")))
    preview = ingest_yc_directory(
        db,
        yc_result(yc_item("acme", "Acme"), yc_item("gamma", "Gamma")),
        enqueue=False,
        now=datetime(2026, 9, 2, tzinfo=UTC),
    )
    assert [alert.dedup_key for alert in preview] == ["yc:gamma"]
    assert not db.has_yc_company("gamma")
    assert db.status()["outbox"] == {}

    live = ingest_yc_directory(
        db,
        yc_result(yc_item("acme", "Acme"), yc_item("gamma", "Gamma")),
        now=datetime(2026, 9, 2, tzinfo=UTC),
    )
    assert [alert.dedup_key for alert in live] == ["yc:gamma"]


def test_later_yc_catalog_fetch_alerts_only_new_slug(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    ingest_yc_directory(db, yc_result(yc_item("acme", "Acme"), yc_item("beta", "Beta")))
    alerts = ingest_yc_directory(
        db,
        yc_result(yc_item("acme", "Acme"), yc_item("beta", "Beta"), yc_item("gamma", "Gamma")),
        now=datetime(2026, 9, 2, tzinfo=UTC),
    )
    assert [alert.dedup_key for alert in alerts] == ["yc:gamma"]
    assert db.status()["official_yc_companies"] == 3


def test_official_listing_upgrades_previous_early_signal(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    ingest_yc_directory(db, yc_result(yc_item("acme", "Acme")))
    early = CanonicalItem(
        Source.TWITTER,
        "tweet-gamma",
        "Gamma",
        "https://x.com/founder/status/1",
        content_text="We got into YC F26!",
        founder_handle="founder",
    )
    db.reserve_item("early:gamma", early, "alerted", "founder_self_announcement")
    alerts = ingest_yc_directory(
        db,
        yc_result(yc_item("acme", "Acme"), yc_item("gamma", "Gamma")),
        now=datetime(2026, 9, 2, tzinfo=UTC),
    )
    assert len(alerts) == 1
    assert alerts[0].upgrade_from == "early:gamma"
    assert alerts[0].upgrade_note
    assert "early founder signal" in alerts[0].upgrade_note


def test_historical_local_diff_never_alerts(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    ingest_yc_directory(db, yc_result(yc_item("baseline", "Baseline")))
    historical = yc_item(
        "belozfi",
        "BelozFi",
        added=True,
        launched_at=datetime(2022, 3, 6, tzinfo=UTC),
    )
    alerts = ingest_yc_directory(
        db,
        yc_result(historical),
        now=datetime(2026, 9, 2, tzinfo=UTC),
    )
    assert alerts == []
    assert db.has_yc_company("belozfi")
    assert db.status()["outbox"] == {}


def test_recent_listing_alerts_even_if_missing_from_daily_added_feed(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    ingest_yc_directory(db, yc_result(yc_item("baseline", "Baseline")))
    ascii_co = yc_item(
        "ascii",
        "Ascii",
        added=False,
        launched_at=datetime(2026, 8, 31, tzinfo=UTC),
    )
    alerts = ingest_yc_directory(
        db,
        yc_result(ascii_co),
        now=datetime(2026, 9, 2, tzinfo=UTC),
    )
    assert [alert.dedup_key for alert in alerts] == ["yc:ascii"]


def test_recent_authoritative_change_alerts(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    ingest_yc_directory(db, yc_result(yc_item("baseline", "Baseline")))
    orca = yc_item(
        "orca-aerospace",
        "Orca Aerospace",
        added=True,
        launched_at=datetime(2026, 9, 2, 1, 33, tzinfo=UTC),
    )
    alerts = ingest_yc_directory(
        db,
        yc_result(orca),
        now=datetime(2026, 9, 2, 2, 0, tzinfo=UTC),
    )
    assert [alert.dedup_key for alert in alerts] == ["yc:orca-aerospace"]


def test_recent_leads_cli_payload(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    item = CanonicalItem(
        Source.TWITTER,
        "tweet-1",
        "Harbor",
        "https://x.com/alice/status/1",
        content_text="We got into YC F26",
    )
    db.reserve_item("early:harbor", item, "alerted", "gpt_confirmed_founder_self_announcement")
    leads = db.recent_leads(5)
    assert leads[0]["dedup_key"] == "early:harbor"
    assert leads[0]["company_name"] == "Harbor"
    assert leads[0]["reason"] == "gpt_confirmed_founder_self_announcement"


def test_seen_social_post_lookup(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    item = CanonicalItem(
        Source.TWITTER,
        "social-post",
        None,
        "https://x.com/example/status/social-post",
        content_text="YC S26 discussion",
    )
    db.reserve_item("twitter:social-post", item, "rejected", "noise")
    assert db.has_seen_item("twitter:social-post")
    assert not db.has_seen_item("twitter:new-post")


def test_failed_yc_fetch_does_not_mark_bootstrap(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    alerts = ingest_yc_directory(db, yc_result(yc_item("acme", "Acme"), status=HealthStatus.FAILED))
    assert alerts == []
    assert not db.is_yc_catalog_bootstrapped()
    assert db.status()["official_yc_companies"] == 0


def test_scheduler_immediate_first_run_does_not_overlap() -> None:
    pipeline = MagicMock()
    scheduler = build_scheduler(pipeline, interval_hours=8)
    job = scheduler.get_job(MONITOR_JOB_ID)
    assert job is not None
    assert job.max_instances == 1
    assert job.coalesce is True
    assert job.next_run_time is None

    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    first = schedule_first_run(scheduler, 8, run_immediately=True, now=now)
    assert first == now + timedelta(seconds=30)
    assert job_next_run_iso(scheduler) == first.isoformat()
    later = schedule_first_run(scheduler, 8, run_immediately=False, now=now)
    assert later.isoformat() == "2026-09-01T20:00:00+00:00"


@pytest.mark.asyncio
async def test_almanac_official_identity_suppresses_social_end_to_end(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    almanac = CanonicalItem(
        Source.YC_DIRECTORY,
        "almanac",
        "Almanac",
        "https://www.ycombinator.com/companies/almanac",
        company_url="https://usealmanac.com",
        raw={
            "name": "Almanac",
            "slug": "almanac",
            "former_names": ["Almanac HQ"],
            "website": "https://usealmanac.com",
            "founders": [{"twitter_url": "https://x.com/janedoe"}],
        },
    )
    ingest_yc_directory(db, yc_result(almanac))
    names, hosts, handles = db.official_identities()
    assert "almanac" in names
    assert "almanac hq" in names
    assert "usealmanac.com" in hosts
    assert "janedoe" in handles

    settings = Settings(database_path=str(tmp_path / "state.db"), openai_api_key=None)
    pipeline = MonitorPipeline(settings)
    pipeline.db = db
    pipeline.official_adapters = []
    pipeline.social_adapters = []

    by_handle = CanonicalItem(
        Source.TWITTER,
        "tweet-almanac-handle",
        "Harbor",
        "https://x.com/janedoe/status/1",
        content_text="We got into YC S26 building Harbor",
        founder_handle="janedoe",
    )
    by_name = CanonicalItem(
        Source.TWITTER,
        "tweet-almanac-name",
        "Almanac",
        "https://x.com/other/status/2",
        content_text="We got into YC S26! Almanac is live.",
        founder_handle="otherfounder",
    )
    by_site = CanonicalItem(
        Source.TWITTER,
        "tweet-almanac-site",
        "Harbor Labs",
        "https://x.com/third/status/3",
        content_text="We got into YC S26 building Harbor Labs",
        company_url="https://usealmanac.com",
        founder_handle="thirdfounder",
    )

    class FakeSocial:
        source = Source.TWITTER

        async def collect(self, client: object) -> CollectionResult:
            return CollectionResult(
                [by_handle, by_name, by_site],
                AdapterHealth(Source.TWITTER, HealthStatus.OK, "fixture", 3),
            )

    pipeline.social_adapters = [FakeSocial()]
    result = await pipeline.run(dry_run=True)
    assert result["alert_count"] == 0
    # Dry runs report candidates without mutating social dedup or alert state.
    assert db.status()["seen"] == {}
    assert db.status()["outbox"] == {}


@pytest.mark.asyncio
async def test_pipeline_includes_gpt_stats(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "state.db"),
        openai_max_calls_per_cycle=25,
    )
    pipeline = MonitorPipeline(settings)
    pipeline.official_adapters = []
    pipeline.social_adapters = []
    result = await pipeline.run(dry_run=True)
    assert result["gpt"]["max_calls"] == 25
    assert result["gpt"]["calls"] == 0
    status = pipeline.db.status()
    assert status["gpt"]["max_calls"] == 25
    assert "next_run_at" in status
