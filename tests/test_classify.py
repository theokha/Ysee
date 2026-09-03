from yc_monitor.classify import classify_social, matches_official_name, normalize_company
from yc_monitor.models import AlertKind, CanonicalItem, Source


def item(text: str) -> CanonicalItem:
    return CanonicalItem(
        source=Source.TWITTER,
        item_id="123",
        company_name="Acme AI",
        canonical_url="https://x.com/founder/status/123",
        content_text=text,
        founder_handle="founder",
    )


def test_founder_announcement_is_early() -> None:
    result = classify_social(
        item("We got into YC S26! Excited to keep building Acme AI."), set(), set(), set()
    )
    assert result.alert
    assert result.alert.kind == AlertKind.EARLY_FOUNDER
    assert result.confidence >= 0.8


def test_official_company_is_suppressed() -> None:
    result = classify_social(
        item("We got into YC S26!"), {normalize_company("Acme AI")}, set(), set()
    )
    assert result.alert is None
    assert result.reason == "company_already_official"


def test_application_is_rejected() -> None:
    result = classify_social(item("We applied to YC W27, wish me luck"), set(), set(), set())
    assert result.alert is None


def test_generic_one_word_name_does_not_false_match() -> None:
    assert not matches_official_name("Harbor", {"almanac"})
    assert not matches_official_name("AI", {"almanac"})
    assert not matches_official_name("Labs", {"harbor labs"})
    assert matches_official_name("Almanac", {"almanac"})
    assert matches_official_name("Almanac AI", {"almanac"})
    assert matches_official_name("Almanac HQ", {"almanac hq"})
    assert matches_official_name("Acme Robotics Platform", {"acme robotics"})


def test_website_and_founder_handle_suppression() -> None:
    post = CanonicalItem(
        source=Source.TWITTER,
        item_id="123",
        company_name="Harbor Labs",
        canonical_url="https://x.com/jane/status/123",
        content_text="We got into YC S26 building Harbor Labs",
        company_url="https://usealmanac.com",
        founder_handle="jane",
    )
    by_site = classify_social(post, set(), {"usealmanac.com"}, set())
    assert by_site.alert is None
    assert by_site.reason == "website_already_official"
    by_handle = classify_social(item("We got into YC S26!"), set(), set(), {"founder"})
    assert by_handle.alert is None
    assert by_handle.reason == "founder_already_official"


# --- one-word official name subset matching -----------------------------------
# Found in live rescans: "Nori Robotics (YC S26)" vs official "nori" (nori.ai)
# was not suppressed because token matching required both names multi-token.


def test_one_word_official_name_suppresses_longer_candidate() -> None:
    assert matches_official_name("Nori Robotics", {"nori"})
    assert matches_official_name("Nori Robotics Platform", {"nori"})


def test_one_word_official_generic_word_does_not_suppress() -> None:
    # "cloud" is not distinctive; a different "Cloud Systems" startup must alert.
    assert not matches_official_name("Cloud Systems", {"cloud"})
    assert not matches_official_name("Labs Venture", {"labs"})


def test_short_one_word_official_name_does_not_suppress() -> None:
    # Under the 4-char floor even distinctive-looking short names stay exact-only.
    assert not matches_official_name("AIQ Systems", {"aiq"})


def test_exact_match_still_shortest_path() -> None:
    assert matches_official_name("nori", {"nori"})
