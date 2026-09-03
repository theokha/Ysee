from unittest.mock import AsyncMock, MagicMock

import openai
import pytest

from yc_monitor.gpt_classify import GPTSocialClassifier, SocialJudgement
from yc_monitor.models import AlertKind, CanonicalItem, Source


def social_item(text: str, company: str | None = None) -> CanonicalItem:
    return CanonicalItem(
        Source.TWITTER,
        "tweet-1",
        company,
        "https://x.com/alice/status/tweet-1",
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


class FakeResolver:
    def __init__(self, result: bool | None) -> None:
        self.result = result

    async def resolve_official(
        self, handle: str, official_names: set[str], official_hosts: set[str]
    ) -> bool | None:
        return self.result


@pytest.mark.asyncio
async def test_gpt_accepts_founder_launch_and_extracts_fields() -> None:
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=True,
        is_first_party=True,
        is_current_announcement=True,
        is_accelerator_acceptance=True,
        company_name="Frontier Computing",
        program="YC",
        batch="YC S26",
        evidence_quotes=["Today we're launching Frontier Computing (YC S26)"],
        confidence=0.94,
        reason="First-party company launch",
        noise_type=None,
    ))
    item = social_item("Today we're launching Frontier Computing (YC S26)")
    result = await classifier.classify(item, set(), set(), set())
    assert result.alert
    assert result.alert.kind == AlertKind.EARLY_FOUNDER
    assert result.alert.dedup_key == "early:frontier computing"
    assert item.batch == "YC S26"


@pytest.mark.asyncio
async def test_gpt_rejects_positive_without_company_name() -> None:
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=True,
        company_name=None,
        batch="YC",
        confidence=0.95,
        reason="Author says they got into YC",
        noise_type=None,
    ))
    result = await classifier.classify(
        social_item("Since we just got into YC, everyone got curious"), set(), set(), set()
    )
    assert result.alert is None
    assert result.persist
    assert result.reason.startswith("gpt_review:")


@pytest.mark.asyncio
async def test_gpt_rejects_news() -> None:
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=False,
        company_name="Almanac",
        batch="YC S26",
        confidence=0.98,
        reason="Third-party news report",
        noise_type="news",
    ))
    result = await classifier.classify(
        social_item("Almanac YC S26 launches an AI agent"), set(), set(), set()
    )
    assert result.alert is None
    assert result.persist
    assert result.reason.startswith("gpt_rejected:")


THIRD_PARTY_TEXT = "Metal (YC S26), a robotics startup, just raised a $12M seed round"


@pytest.mark.asyncio
async def test_third_party_new_company_report_promotes_to_yc_launch() -> None:
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=False,
        is_third_party_report=True,
        signal_kind="new_company_report",
        company_name="Metal",
        program="YC",
        batch="YC S26",
        evidence_quotes=["Metal (YC S26)", "just raised a $12M seed round"],
        confidence=0.9,
        reason="News account reports YC S26 company Metal raised funding",
        noise_type=None,
    ))
    item = social_item(THIRD_PARTY_TEXT)
    result = await classifier.classify(item, set(), set(), set())
    assert result.alert
    assert result.alert.kind == AlertKind.EARLY_YC_LAUNCH
    assert result.alert.dedup_key == "early:metal"
    assert result.reason.startswith("gpt_new_company_report:")
    assert item.company_name == "Metal"
    assert item.batch == "YC S26"


@pytest.mark.asyncio
async def test_third_party_report_suppressed_when_company_already_official() -> None:
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=False,
        is_third_party_report=True,
        signal_kind="new_company_report",
        company_name="Metal",
        program="YC",
        batch="YC S26",
        evidence_quotes=["Metal (YC S26)"],
        confidence=0.9,
        reason="Reports YC S26 company Metal raised funding",
        noise_type=None,
    ))
    result = await classifier.classify(
        social_item(THIRD_PARTY_TEXT, "Metal"), {"metal"}, set(), set()
    )
    assert result.alert is None
    assert result.reason == "company_already_official"


@pytest.mark.parametrize(
    ("overrides", "label"),
    [
        ({"is_retrospective": True}, "retrospective"),
        ({"signal_kind": "first_party_launch"}, "wrong_signal_kind"),
        ({"signal_kind": "none"}, "no_signal_kind"),
        ({"is_satire_or_joke": True}, "satire"),
    ],
)
@pytest.mark.asyncio
async def test_third_party_report_not_promoted_without_qualifiers(
    overrides: dict[str, object], label: str
) -> None:
    fields: dict[str, object] = {
        "is_founder_self_announcement": False,
        "is_third_party_report": True,
        "signal_kind": "new_company_report",
        "company_name": "Metal",
        "program": "YC",
        "batch": "YC S26",
        "evidence_quotes": ["Metal (YC S26)"],
        "confidence": 0.9,
        "reason": f"Reports YC S26 company Metal raising ({label})",
        "noise_type": None,
    }
    fields.update(overrides)
    classifier = classifier_with_result(SocialJudgement(**fields))
    result = await classifier.classify(social_item(THIRD_PARTY_TEXT), set(), set(), set())
    assert result.alert is None
    assert result.persist
    assert result.reason.startswith("gpt_rejected:")


@pytest.mark.asyncio
async def test_third_party_report_requires_supported_evidence() -> None:
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=False,
        is_third_party_report=True,
        signal_kind="new_company_report",
        company_name="Metal",
        program="YC",
        batch="YC S26",
        evidence_quotes=["a quote that appears nowhere in the post"],
        confidence=0.9,
        reason="Reports YC S26 company Metal raising funding",
        noise_type=None,
    ))
    result = await classifier.classify(social_item(THIRD_PARTY_TEXT), set(), set(), set())
    assert result.alert is None
    assert result.reason.startswith("gpt_rejected:")


@pytest.mark.asyncio
async def test_contradictory_negative_stores_review_company() -> None:
    text = "Harbor Labs (YC F26) just came out of stealth"
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=False,
        company_name="Harbor Labs",
        batch="YC F26",
        confidence=0.9,
        reason="The post clearly announces acceptance into YC F26 for Harbor Labs.",
        noise_type=None,
    ))
    item = social_item(text)
    result = await classifier.classify(item, set(), set(), set())
    assert result.alert is None
    assert result.reason == "gpt_review:contradictory_verdict"
    assert item.company_name == "Harbor Labs"


@pytest.mark.asyncio
async def test_contradictory_negative_ignores_generic_company() -> None:
    text = "Our startup just got into YC, here is what we learned"
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=False,
        company_name="startup",
        batch="YC F26",
        confidence=0.9,
        reason="The post explicitly announces acceptance into YC.",
        noise_type=None,
    ))
    item = social_item(text)
    result = await classifier.classify(item, set(), set(), set())
    assert result.reason == "gpt_review:contradictory_verdict"
    assert item.company_name is None


@pytest.mark.asyncio
async def test_almanac_alias_and_handle_override_gpt() -> None:
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=True,
        is_first_party=True,
        is_current_announcement=True,
        is_accelerator_acceptance=True,
        company_name="Almanac",
        program="YC",
        batch="YC S26",
        evidence_quotes=["We're launching Almanac (YC S26)"],
        confidence=0.99,
        reason="First-party",
        noise_type=None,
    ))
    by_name = await classifier.classify(
        social_item("We're launching Almanac (YC S26)", "Almanac"),
        {"almanac", "almanac hq"},
        {"usealmanac.com"},
        {"janedoe"},
    )
    assert by_name.alert is None
    assert by_name.reason == "company_already_official"
    by_handle = await classifier.classify(
        social_item("We got into YC S26 building Harbor", "Harbor"),
        set(),
        set(),
        {"alice"},
    )
    assert by_handle.alert is None
    assert by_handle.reason == "founder_already_official"


@pytest.mark.asyncio
async def test_official_identity_overrides_gpt() -> None:
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=True,
        is_first_party=True,
        is_current_announcement=True,
        is_accelerator_acceptance=True,
        company_name="Acme AI",
        program="YC",
        batch="YC S26",
        evidence_quotes=["We're launching Acme AI (YC S26)"],
        confidence=0.95,
        reason="First-party",
        noise_type=None,
    ))
    result = await classifier.classify(
        social_item("We're launching Acme AI (YC S26)"), {"acme"}, set(), set()
    )
    assert result.alert is None
    assert result.reason == "company_already_official"


@pytest.mark.asyncio
async def test_non_program_post_skips_gpt() -> None:
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=True,
        company_name="Acme",
        batch=None,
        confidence=1,
        reason="unused",
        noise_type=None,
    ))
    result = await classifier.classify(social_item("We launched a new feature"), set(), set(), set())
    assert result.reason == "missing_program_reference"
    assert classifier.client
    classifier.client.responses.parse.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (
            "I'm 30. I built GojiberryAI. Got accepted into YC. If I had to start from 0, here's what I'd do.",
            "retrospective_not_current_announcement",
        ),
        (
            "My life so far: college, McKinsey, got into YC, got married, still building.",
            "retrospective_not_current_announcement",
        ),
        (
            "Excited to announce I was accepted into YC P26 to build @trylightning",
            "invalid_yc_batch_code",
        ),
        (
            "hey im the founder of @wildcard_ai, we do AI search for ecomm, backed by yc. shoot me a DM",
            "yc_affiliation_without_current_acceptance",
        ),
    ],
)
async def test_production_noise_is_hard_rejected_before_gpt(text: str, reason: str) -> None:
    judgement = SocialJudgement(
        is_founder_self_announcement=True,
        is_first_party=True,
        is_current_announcement=True,
        is_accelerator_acceptance=True,
        company_name="Noise Co",
        program="YC",
        batch="YC F26",
        evidence_quotes=[text[:20]],
        confidence=0.99,
        reason="wrong",
    )
    classifier = classifier_with_result(judgement)
    result = await classifier.classify(social_item(text), set(), set(), set())
    assert result.alert is None
    assert result.reason == reason
    assert classifier.client
    classifier.client.responses.parse.assert_not_awaited()


@pytest.mark.asyncio
async def test_product_launch_only_goes_to_review() -> None:
    text = "We are launching GitNexus from Akon Labs. Akon Labs is YC S26."
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=True,
        is_first_party=True,
        is_current_announcement=True,
        is_accelerator_acceptance=False,
        is_product_launch_only=True,
        company_name="Akon Labs",
        product_name="GitNexus",
        program="YC",
        batch="YC S26",
        evidence_quotes=["launching GitNexus"],
        confidence=0.98,
        reason="Product launch by existing YC company",
    ))
    result = await classifier.classify(social_item(text), {"akon"}, set(), set())
    assert result.alert is None
    assert result.reason == "company_already_official"

    unseen = await classifier.classify(social_item(text), set(), set(), set())
    assert unseen.alert
    assert unseen.alert.kind == AlertKind.EARLY_YC_LAUNCH


@pytest.mark.asyncio
async def test_company_handle_crosscheck_suppresses_box_product() -> None:
    text = "we just got into the YC F26 batch to build box by @asciidotdev"
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=True,
        is_first_party=True,
        is_current_announcement=True,
        is_accelerator_acceptance=True,
        company_name="Ascii",
        product_name="box",
        company_handle="asciidotdev",
        program="YC",
        batch="YC F26",
        evidence_quotes=["we just got into the YC F26 batch", "box by @asciidotdev"],
        confidence=0.99,
        reason="Current first-party acceptance",
    ))
    result = await classifier.classify(social_item(text), {"ascii"}, set(), {"asciidotdev"})
    assert result.alert is None
    assert result.reason == "company_already_official"


@pytest.mark.asyncio
async def test_unsupported_evidence_quote_goes_to_review() -> None:
    text = "We got into YC F26 with Harbor"
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=True,
        is_first_party=True,
        is_current_announcement=True,
        is_accelerator_acceptance=True,
        company_name="Harbor",
        program="YC",
        batch="YC F26",
        evidence_quotes=["a quote not in the post"],
        confidence=0.99,
        reason="Current acceptance",
    ))
    result = await classifier.classify(social_item(text), set(), set(), set())
    assert result.alert is None
    assert result.reason == "gpt_review:unsupported_evidence"


@pytest.mark.asyncio
async def test_kairo_speedrun_without_cohort_alerts_as_speedrun_launch() -> None:
    text = (
        "Two months ago, I left BCG and bet it all on my company, Kairo. "
        "Today I'm excited to announce that Kairo is backed by a16z @speedrun."
    )
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=True,
        is_first_party=True,
        is_current_announcement=True,
        is_accelerator_acceptance=True,
        company_name="Kairo",
        program="Speedrun",
        batch=None,
        evidence_quotes=["Today I'm excited to announce", "Kairo is backed by a16z"],
        confidence=0.95,
        reason="Current first-party a16z Speedrun announcement",
    ))
    result = await classifier.classify(social_item(text), set(), set(), set())
    assert result.alert
    assert result.alert.kind == AlertKind.EARLY_SPEEDRUN_LAUNCH


@pytest.mark.asyncio
async def test_contradictory_negative_verdict_goes_to_review() -> None:
    text = "since we just got into YC, everyone got curious about box"
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=False,
        company_name="box",
        batch="YC F26",
        confidence=0.9,
        reason="The post explicitly announces acceptance into YC and identifies the author.",
        noise_type=None,
    ))
    result = await classifier.classify(social_item(text), set(), set(), set())
    assert result.alert is None
    assert result.reason == "gpt_review:contradictory_verdict"


@pytest.mark.asyncio
async def test_resolver_true_suppresses_box_product() -> None:
    text = "we just got into the YC F26 batch to build box by @asciidotdev"
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=True,
        is_first_party=True,
        is_current_announcement=True,
        is_accelerator_acceptance=True,
        company_name="Ascii",
        product_name="box",
        company_handle="asciidotdev",
        program="YC",
        batch="YC F26",
        evidence_quotes=["we just got into the YC F26 batch", "box by @asciidotdev"],
        confidence=0.99,
        reason="Current first-party acceptance",
    ), resolver=FakeResolver(True))
    result = await classifier.classify(social_item(text), {"ascii"}, set(), set())
    assert result.alert is None
    assert result.reason == "company_already_official"


@pytest.mark.asyncio
async def test_unresolved_product_owner_goes_to_review() -> None:
    text = "we just got into the YC F26 batch to build box by @unknownmaker"
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=True,
        is_first_party=True,
        is_current_announcement=True,
        is_accelerator_acceptance=True,
        company_name="Unknown Co",
        product_name="box",
        company_handle="unknownmaker",
        program="YC",
        batch="YC F26",
        evidence_quotes=["we just got into the YC F26 batch"],
        confidence=0.95,
        reason="Current first-party acceptance",
    ), resolver=FakeResolver(None))
    result = await classifier.classify(social_item(text), set(), set(), set())
    assert result.alert is None
    assert result.reason == "gpt_review:unresolved_company_handle"


@pytest.mark.asyncio
async def test_yc_product_launch_alerts_as_yc_launch_kind() -> None:
    text = "Today we're launching Frontier Computing, built by us for the YC S26 batch."
    classifier = classifier_with_result(SocialJudgement(
        is_founder_self_announcement=True,
        is_first_party=True,
        is_current_announcement=True,
        is_accelerator_acceptance=False,
        is_product_launch_only=True,
        company_name="Frontier Computing",
        program="YC",
        batch="YC S26",
        evidence_quotes=["Today we're launching Frontier Computing"],
        confidence=0.93,
        reason="First-party current-batch launch, acceptance not stated",
    ))
    result = await classifier.classify(social_item(text), set(), set(), set())
    assert result.alert
    assert result.alert.kind == AlertKind.EARLY_YC_LAUNCH


@pytest.mark.asyncio
async def test_api_failure_defers_weak_candidate() -> None:
    request = MagicMock()
    client = MagicMock()
    client.responses.parse = AsyncMock(
        side_effect=openai.APIConnectionError(request=request)
    )
    classifier = GPTSocialClassifier("key", "test-model", 5, 0, 2, 0.65, client=client)
    result = await classifier.classify(
        social_item("Frontier Computing is in YC S26"), set(), set(), set()
    )
    assert result.alert is None
    assert not result.persist


@pytest.mark.asyncio
async def test_api_failure_keeps_deterministic_positive() -> None:
    request = MagicMock()
    client = MagicMock()
    client.responses.parse = AsyncMock(
        side_effect=openai.APIConnectionError(request=request)
    )
    classifier = GPTSocialClassifier("key", "test-model", 5, 0, 2, 0.65, client=client)
    result = await classifier.classify(
        social_item("We got into YC S26!", "Acme AI"), set(), set(), set()
    )
    assert result.alert
    assert result.persist


@pytest.mark.asyncio
async def test_no_key_uses_deterministic_classifier() -> None:
    classifier = GPTSocialClassifier(None, "test-model", 5, 0, 2, 0.65)
    result = await classifier.classify(
        social_item("We got into YC S26!", "Acme AI"), set(), set(), set()
    )
    assert result.alert


@pytest.mark.asyncio
async def test_gpt_cycle_budget_caps_calls_and_records_stats() -> None:
    judgement = SocialJudgement(
        is_founder_self_announcement=True,
        is_first_party=True,
        is_current_announcement=True,
        is_accelerator_acceptance=True,
        company_name="Frontier Computing",
        program="YC",
        batch="YC S26",
        evidence_quotes=["Today we're launching Frontier Computing (YC S26)"],
        confidence=0.94,
        reason="First-party company launch",
        noise_type=None,
    )
    response = MagicMock(output_parsed=judgement)
    client = MagicMock()
    client.responses.parse = AsyncMock(return_value=response)
    classifier = GPTSocialClassifier(
        "key", "test-model", 5, 0, 2, 0.65, max_calls_per_cycle=1, client=client
    )
    first = await classifier.classify(
        social_item("Today we're launching Frontier Computing (YC S26)"), set(), set(), set()
    )
    second = await classifier.classify(
        social_item("We got into YC S26 as Harbor Labs"), set(), set(), set()
    )
    skipped = await classifier.classify(social_item("We launched a new feature"), set(), set(), set())
    assert first.alert
    assert second.reason == "gpt_cycle_budget_exhausted"
    assert not second.persist
    assert skipped.reason == "missing_program_reference"
    assert classifier.client
    classifier.client.responses.parse.assert_awaited_once()
    assert classifier.stats.as_dict() == {
        "calls": 1,
        "accepted": 1,
        "rejected": 1,
        "deferred": 1,
        "prefiltered": 1,
        "capped": 1,
        "max_calls": 1,
    }


@pytest.mark.asyncio
async def test_gpt_stats_count_rejected_and_deferred() -> None:
    client = MagicMock()
    client.responses.parse = AsyncMock(
        side_effect=[
            MagicMock(output_parsed=SocialJudgement(
                is_founder_self_announcement=False,
                company_name="Almanac",
                batch="YC S26",
                confidence=0.98,
                reason="Third-party news report",
                noise_type="news",
            )),
            openai.APIConnectionError(request=MagicMock()),
        ]
    )
    classifier = GPTSocialClassifier(
        "key", "test-model", 5, 0, 2, 0.65, max_calls_per_cycle=5, client=client
    )
    rejected = await classifier.classify(
        social_item("Almanac YC S26 launches an AI agent"), set(), set(), set()
    )
    deferred = await classifier.classify(
        social_item("Frontier Computing is in YC S26"), set(), set(), set()
    )
    assert rejected.persist
    assert not deferred.persist
    assert classifier.stats.calls == 2
    assert classifier.stats.rejected == 1
    assert classifier.stats.deferred == 1
    assert classifier.stats.accepted == 0
