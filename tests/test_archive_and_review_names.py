from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from yc_monitor.config import Settings
from yc_monitor.db import ARCHIVED_NOISE_KEYS, Database
from yc_monitor.gpt_classify import GENERIC_COMPANY, GPTSocialClassifier, SocialJudgement
from yc_monitor.models import CanonicalItem, Source
from yc_monitor.pond_server import create_app

NOISE_KEYS = (
    "early:gojiberryai",
    "early:generational healthcare",
    "early:trylightning",
    "early:wildcard ai",
    "early:gitnexus akon",
    "early:box",
)


def social_item(text: str, company: str | None = None) -> CanonicalItem:
    return CanonicalItem(
        Source.LINKEDIN,
        "post-1",
        company,
        "https://linkedin.com/alice/post-1",
        content_text=text,
        founder_name="Alice",
        founder_handle="alice",
    )


def classifier_with_result(
    judgement: SocialJudgement, resolver: object | None = None
) -> GPTSocialClassifier:
    response = MagicMock(output_parsed=judgement)
    client = MagicMock()
    client.responses.parse = AsyncMock(return_value=response)
    return GPTSocialClassifier(
        "key", "test-model", 5, 0, 2, 0.65,
        company_resolver=resolver,  # type: ignore[arg-type]
        client=client,
    )


def test_archived_noise_keys_are_exactly_the_six_verified_false_positives() -> None:
    assert ARCHIVED_NOISE_KEYS == frozenset(NOISE_KEYS)


def test_archive_known_noise_is_idempotent_and_spares_other_rows(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    for key in NOISE_KEYS:
        db.reserve_item(key, social_item("noise", "Noise"), "alerted")
    kairo = CanonicalItem(
        Source.TWITTER, "kairo-1", "Kairo", "https://x.com/a/kairo-1", content_text="YC"
    )
    db.reserve_item("early:kairo", kairo, "alerted")

    assert db.archive_known_noise() == 6
    assert db.archive_known_noise() == 0

    with db.connect() as connection:
        rows = {
            str(row["dedup_key"]): str(row["disposition"])
            for row in connection.execute(
                "SELECT dedup_key, disposition FROM seen_items WHERE dedup_key LIKE 'early:%'"
            )
        }
    assert all(rows[key] == "archived" for key in NOISE_KEYS)
    assert rows["early:kairo"] == "alerted"


def test_archive_leaves_rows_already_out_of_live_dispositions(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    db.reserve_item("early:box", social_item("box", "box"), "rejected")

    assert db.archive_known_noise() == 0
    with db.connect() as connection:
        row = connection.execute(
            "SELECT disposition FROM seen_items WHERE dedup_key='early:box'"
        ).fetchone()
    assert str(row["disposition"]) == "rejected"


def test_recent_leads_excludes_archived_rows(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    db.reserve_item("early:box", social_item("box", "box"), "alerted")
    kairo = CanonicalItem(
        Source.TWITTER, "kairo-1", "Kairo", "https://x.com/a/kairo-1", content_text="YC"
    )
    db.reserve_item("early:kairo", kairo, "alerted")

    keys = {lead["dedup_key"] for lead in db.recent_leads(25)}
    assert keys == {"early:box", "early:kairo"}
    db.archive_known_noise()
    assert [lead["dedup_key"] for lead in db.recent_leads(25)] == ["early:kairo"]


def test_create_app_archives_known_noise_at_startup(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "pond.db"),
        scheduler_run_immediately=False,
    )
    db = Database(settings.database_path)
    for key in NOISE_KEYS:
        db.reserve_item(key, social_item("noise", "Noise"), "pending")
    db.reserve_item("early:kairo", social_item("kairo", "Kairo"), "alerted")

    client = TestClient(create_app(settings))
    assert client.get("/healthz").status_code == 200

    assert [lead["dedup_key"] for lead in db.recent_leads(25)] == ["early:kairo"]


def test_startup_survives_archive_failure(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "pond.db"),
        scheduler_run_immediately=False,
    )
    with patch(
        "yc_monitor.db.Database.archive_known_noise",
        side_effect=RuntimeError("disk unavailable"),
    ):
        client = TestClient(create_app(settings))
        health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_review_path_stores_extracted_company_name() -> None:
    text = "Today we're launching Harbor, and we just got into YC."
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=True,
        is_first_party=True,
        is_current_announcement=True,
        is_accelerator_acceptance=True,
        company_name="Harbor",
        program="YC",
        batch="not-a-batch",
        evidence_quotes=["Today we're launching Harbor"],
        confidence=0.95,
        reason="Current first-party acceptance with an unparsable batch",
    ))
    item = social_item(text)
    result = await classifier.classify(item, set(), set(), set())
    assert result.alert is None
    assert result.reason.startswith("gpt_review:")
    assert item.company_name == "Harbor"


@pytest.mark.asyncio
async def test_generic_company_name_is_not_stored_on_review_rows() -> None:
    text = "Today we're launching our AI, and we just got into YC."
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=True,
        is_first_party=True,
        is_current_announcement=True,
        is_accelerator_acceptance=True,
        company_name="AI",
        program="YC",
        batch="not-a-batch",
        evidence_quotes=["Today we're launching our AI"],
        confidence=0.95,
        reason="Current first-party acceptance without a real company name",
    ))
    item = social_item(text)
    result = await classifier.classify(item, set(), set(), set())
    assert result.reason == "gpt_review:generic_or_missing_company"
    assert item.company_name is None


def test_generic_company_rejects_bare_ai() -> None:
    assert GENERIC_COMPANY.fullmatch("AI") is not None
    assert GENERIC_COMPANY.fullmatch("ai") is not None
    assert GENERIC_COMPANY.fullmatch("Harbor") is None


@pytest.mark.asyncio
async def test_incomplete_announcement_review_keeps_extracted_company() -> None:
    text = "Today we're launching Harbor with the support of YC."
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=True,
        is_first_party=False,
        is_current_announcement=True,
        is_accelerator_acceptance=True,
        company_name="Harbor",
        program="YC",
        batch="YC S26",
        evidence_quotes=["Today we're launching Harbor"],
        confidence=0.95,
        reason="Launch posted by a third party",
    ))
    item = social_item(text)
    result = await classifier.classify(item, set(), set(), set())
    assert result.reason == "gpt_review:not_first_party"
    assert item.company_name == "Harbor"


@pytest.mark.asyncio
async def test_unresolved_company_handle_review_keeps_extracted_company() -> None:
    class UnknownResolver:
        async def resolve_official(
            self, handle: str, official_names: set[str], official_hosts: set[str]
        ) -> bool | None:
            return None

    text = "Today we're launching Harbor (YC S26) by @harborhq"
    classifier = classifier_with_result(
        SocialJudgement(
            is_founder_self_announcement=True,
            is_first_party=True,
            is_current_announcement=True,
            is_accelerator_acceptance=True,
            company_name="Harbor",
            product_name=None,
            company_handle="harborhq",
            program="YC",
            batch="YC S26",
            evidence_quotes=["Today we're launching Harbor (YC S26)"],
            confidence=0.99,
            reason="Current first-party acceptance",
        ),
        resolver=UnknownResolver(),
    )
    item = social_item(text)
    result = await classifier.classify(item, set(), set(), set())
    assert result.reason == "gpt_review:unresolved_company_handle"
    assert item.company_name == "Harbor"
