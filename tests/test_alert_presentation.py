from __future__ import annotations

from datetime import UTC, datetime

from yc_monitor.classify import official_alert
from yc_monitor.models import Alert, AlertKind, CanonicalItem, Source
from yc_monitor.slack_format import (
    format_alert,
    format_status_blocks,
    humanize_reason,
)


def _social_item(**overrides: object) -> CanonicalItem:
    defaults: dict[str, object] = {
        "source": Source.TWITTER,
        "item_id": "1",
        "company_name": "Harbor",
        "canonical_url": "https://x.com/alice/status/1",
        "content_text": "We got into YC F26 building Harbor",
        "founder_handle": "alice",
    }
    defaults.update(overrides)
    return CanonicalItem(**defaults)  # type: ignore[arg-type]


def test_notification_text_carries_the_company_name() -> None:
    """The push notification is the header plus the one token worth waking for."""
    alert = Alert(AlertKind.EARLY_FOUNDER, _social_item(), "early:harbor", 0.91)
    text, _ = format_alert(alert)
    assert "Harbor" in text
    assert "EARLY YC ACCEPTANCE" in text


def test_notification_text_survives_a_missing_company_name() -> None:
    alert = Alert(AlertKind.EARLY_FOUNDER, _social_item(company_name=None), "early:x", 0.7)
    text, _ = format_alert(alert)
    assert "EARLY YC ACCEPTANCE" in text
    assert text.endswith("before YC")


def test_early_alert_shows_confidence_and_reason() -> None:
    alert = Alert(
        AlertKind.EARLY_FOUNDER,
        _social_item(),
        "early:harbor",
        0.91,
        reason="gpt_confirmed:founder states acceptance",
    )
    _, blocks = format_alert(alert)
    rendered = str(blocks)
    assert "Confidence 0.91" in rendered
    assert "founder states acceptance" in rendered


def test_official_alert_shows_no_confidence_score() -> None:
    """A catalog listing is a fact, not an inference -- a score would mislead."""
    item = CanonicalItem(Source.YC_DIRECTORY, "acme", "Acme", "https://yc.test/acme")
    _, blocks = format_alert(official_alert(item))
    assert "Confidence" not in str(blocks)


def test_alert_kinds_are_visually_distinct() -> None:
    item = _social_item()
    early, _ = format_alert(Alert(AlertKind.EARLY_FOUNDER, item, "early:harbor"))
    official, _ = format_alert(
        official_alert(CanonicalItem(Source.YC_DIRECTORY, "acme", "Acme", "https://yc.test/acme"))
    )
    assert early[0] != official[0]


def test_timestamps_render_in_the_viewers_timezone() -> None:
    alert = Alert(
        AlertKind.EARLY_FOUNDER,
        _social_item(),
        "early:harbor",
        detected_at=datetime(2026, 9, 3, 20, 30, tzinfo=UTC),
    )
    _, blocks = format_alert(alert)
    rendered = str(blocks)
    assert "<!date^" in rendered
    # The pipe fallback still carries a readable date for push and exports.
    assert "PT" in rendered


def test_long_post_is_marked_as_truncated() -> None:
    alert = Alert(AlertKind.EARLY_FOUNDER, _social_item(content_text="y" * 900), "early:harbor")
    _, blocks = format_alert(alert)
    rendered = str(blocks)
    assert "…" in rendered
    assert "y" * 600 not in rendered


def test_short_post_is_not_marked_as_truncated() -> None:
    alert = Alert(AlertKind.EARLY_FOUNDER, _social_item(content_text="Short one."), "early:h")
    _, blocks = format_alert(alert)
    assert "…" not in str(blocks)


def test_humanize_reason_maps_known_slugs_and_unpacks_unknown_ones() -> None:
    assert humanize_reason("gpt_review:unresolved_company_handle") == (
        "Could not verify the company handle"
    )
    assert humanize_reason("some_new_gate") == "Some new gate"
    # Free-text model prose passes through rather than being mangled.
    assert humanize_reason("gpt_confirmed:founder says they got in") == (
        "founder says they got in"
    )


def test_status_renders_timestamps_not_raw_iso() -> None:
    blocks = format_status_blocks(
        {
            "official_yc_companies": 12,
            "last_run": {"status": "completed", "finished_at": "2026-09-03T23:26:41+00:00"},
            "next_run_at": "2026-09-04T07:26:41+00:00",
        }
    )
    rendered = str(blocks)
    assert "<!date^" in rendered
    assert "2026-09-03T23:26:41" not in rendered


def test_status_keeps_an_unparseable_timestamp_visible() -> None:
    blocks = format_status_blocks({"next_run_at": "not-a-date"})
    assert "not-a-date" in str(blocks)


def test_reason_and_confidence_survive_the_outbox_round_trip() -> None:
    """Alerts are serialized into slack_outbox and rebuilt at delivery time, so
    anything the footer needs has to make the trip or it vanishes on send."""
    from yc_monitor.models import alert_from_dict, alert_to_dict

    original = Alert(
        AlertKind.EARLY_FOUNDER,
        _social_item(),
        "early:harbor",
        0.91,
        reason="gpt_confirmed:founder states acceptance",
    )
    restored = alert_from_dict(alert_to_dict(original))
    assert restored.reason == original.reason
    assert restored.confidence == 0.91
    assert "Confidence 0.91" in str(format_alert(restored)[1])


def test_alert_without_a_reason_still_renders() -> None:
    """Older outbox rows predate the reason field and must not break delivery."""
    alert = Alert(AlertKind.EARLY_FOUNDER, _social_item(), "early:harbor", 0.8)
    _, blocks = format_alert(alert)
    rendered = str(blocks)
    assert "Confidence 0.80" in rendered
    assert "None" not in rendered


def test_speedrun_and_yc_launches_are_headlined_differently() -> None:
    """A Speedrun company headlined "Not yet listed by YC" names the wrong program."""
    item = _social_item()
    speedrun, blocks = format_alert(Alert(AlertKind.EARLY_SPEEDRUN_LAUNCH, item, "early:h"))
    yc, _ = format_alert(Alert(AlertKind.EARLY_YC_LAUNCH, item, "early:h"))
    assert "SPEEDRUN" in speedrun and "a16z" in str(blocks)
    assert "YC" in yc
    assert speedrun != yc


def test_author_bio_survives_the_outbox_round_trip() -> None:
    """A bio-sourced company name is only auditable if the bio makes the trip."""
    from yc_monitor.models import alert_from_dict, alert_to_dict

    item = _social_item(company_name="Baro")
    item.author_bio = "Co-Founder & CEO @ Baro, a16z SR007."
    restored = alert_from_dict(alert_to_dict(Alert(AlertKind.EARLY_SPEEDRUN_LAUNCH, item, "early:baro")))
    assert restored.item.author_bio == "Co-Founder & CEO @ Baro, a16z SR007."


def test_alert_payload_without_a_bio_still_loads() -> None:
    """Rows written before author_bio existed must not break delivery."""
    from yc_monitor.models import alert_from_dict, alert_to_dict

    payload = alert_to_dict(Alert(AlertKind.EARLY_FOUNDER, _social_item(), "early:harbor"))
    del payload["item"]["author_bio"]
    assert alert_from_dict(payload).item.author_bio is None
