from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError

from yc_monitor.__main__ import parser
from yc_monitor.classify import official_alert
from yc_monitor.config import Settings
from yc_monitor.db import Database
from yc_monitor.models import CanonicalItem, Source
from yc_monitor.pipeline import MonitorPipeline
from yc_monitor.slack_app import SlackNotifier, send_test_alert
from yc_monitor.slack_format import build_demo_alert, format_alert


def yc_item(slug: str = "acme") -> CanonicalItem:
    return CanonicalItem(
        Source.YC_DIRECTORY,
        slug,
        slug.title(),
        f"https://yc.test/{slug}",
        description="Logistics AI",
    )


def settings_for(tmp_path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_path": str(tmp_path / "state.db"),
        "slack_bot_token": "xoxb-env",
        "slack_channel_id": "C123",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_slack_failure_then_retry_and_sent_does_not_repeat(tmp_path) -> None:
    cfg = settings_for(tmp_path)
    pipeline = MonitorPipeline(cfg)
    alert = official_alert(yc_item())
    assert pipeline.db.reserve_alert(alert)
    assert pipeline.db.outbox_status(alert.dedup_key) == "pending"

    async def fail(_alert, demo: bool = False, thread_ts: str | None = None) -> dict[str, str]:
        raise SlackApiError("slack down", {"ok": False, "error": "internal_error"})

    pipeline.notifier.send = fail  # type: ignore[method-assign]
    assert await pipeline.deliver_outbox() == 0
    assert pipeline.db.outbox_status(alert.dedup_key) == "failed"
    assert pipeline.db.status()["seen"].get("pending") == 1

    sent: list[str] = []

    async def ok(item, demo: bool = False, thread_ts: str | None = None) -> dict[str, str]:
        sent.append(item.dedup_key)
        return {"channel": "C123", "ts": "1.0"}

    pipeline.notifier.send = ok  # type: ignore[method-assign]
    assert await pipeline.deliver_outbox() == 1
    assert sent == [alert.dedup_key]
    assert pipeline.db.outbox_status(alert.dedup_key) == "sent"
    assert pipeline.db.status()["seen"].get("alerted") == 1

    sent.clear()
    assert await pipeline.deliver_outbox() == 0
    assert sent == []
    assert pipeline.db.outbox_status(alert.dedup_key) == "sent"


@pytest.mark.asyncio
async def test_dry_run_does_not_mutate_alert_or_outbox_state(tmp_path) -> None:
    cfg = settings_for(tmp_path, openai_api_key=None)
    pipeline = MonitorPipeline(cfg)
    pipeline.official_adapters = []
    social = CanonicalItem(
        Source.TWITTER,
        "dry-social",
        "Dry Run Co",
        "https://x.com/founder/status/dry-social",
        content_text="We got into YC S26!",
        founder_handle="founder",
    )

    class FakeSocial:
        source = Source.TWITTER

        async def collect(self, client: object):
            from yc_monitor.models import AdapterHealth, CollectionResult, HealthStatus

            return CollectionResult(
                [social], AdapterHealth(Source.TWITTER, HealthStatus.OK, "fixture", 1)
            )

    pipeline.social_adapters = [FakeSocial()]

    async def boom() -> int:
        raise AssertionError("dry-run must not drain the outbox")

    pipeline.deliver_outbox = boom  # type: ignore[method-assign]
    result = await pipeline.run(dry_run=True)
    assert result["alert_count"] == 1
    assert result["delivered_count"] == 0
    assert pipeline.db.status()["outbox"] == {}
    assert pipeline.db.status()["seen"] == {}


@pytest.mark.asyncio
async def test_oauth_token_used_when_env_token_absent(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    db.save_slack_install("TTEAM", "xoxb-from-oauth")
    notifier = SlackNotifier(
        settings_for(tmp_path, slack_bot_token=None, database_path=str(tmp_path / "state.db")),
        db,
    )
    assert notifier.configured
    with patch("yc_monitor.slack_app.AsyncWebClient") as client_cls:
        client = MagicMock()
        client.chat_postMessage = AsyncMock()
        client_cls.return_value = client
        posted = await notifier.send(official_alert(yc_item()))
        assert posted is not None
        client_cls.assert_called_once_with(token="xoxb-from-oauth")
        client.chat_postMessage.assert_awaited_once()


@pytest.mark.asyncio
async def test_env_token_preferred_over_oauth_install(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    db.save_slack_install("TTEAM", "xoxb-from-oauth")
    notifier = SlackNotifier(settings_for(tmp_path), db)
    with patch("yc_monitor.slack_app.AsyncWebClient") as client_cls:
        client = MagicMock()
        client.chat_postMessage = AsyncMock()
        client_cls.return_value = client
        await notifier.send(official_alert(yc_item()))
        client_cls.assert_called_once_with(token="xoxb-env")


def test_latest_slack_bot_token_is_most_recent_install(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    db.save_slack_install("TOLD", "xoxb-old")
    db.save_slack_install("TNEW", "xoxb-new")
    assert db.latest_slack_bot_token() == "xoxb-new"


def test_demo_alert_is_labeled_and_not_source_evidence() -> None:
    alert = build_demo_alert()
    text, blocks = format_alert(alert, demo=True)
    rendered = str(blocks)
    assert text.startswith("DEMO |")
    assert "DEMO ALERT" in rendered
    assert "not a real YC detection" in rendered
    assert alert.dedup_key == "demo:test-alert"
    assert "x.com/" not in rendered
    assert "linkedin.com" not in rendered.lower()


def test_early_alert_quotes_original_post() -> None:
    from yc_monitor.models import Alert, AlertKind

    item = CanonicalItem(
        Source.TWITTER,
        "1",
        "Harbor",
        "https://x.com/alice/status/1",
        content_text="We got into YC F26 building Harbor",
        founder_handle="alice",
        author_url="https://x.com/alice",
    )
    text, blocks = format_alert(Alert(AlertKind.EARLY_FOUNDER, item, "early:harbor"))
    rendered = str(blocks)
    assert "Original post" in rendered
    assert "Open original post" in rendered
    assert "EARLY YC ACCEPTANCE" in text
    assert "https://x.com/alice/status/1" in rendered
    assert "DEMO ALERT" not in rendered


def test_test_alert_command_seam() -> None:
    args = parser().parse_args(["test-alert"])
    assert args.command == "test-alert"


@pytest.mark.asyncio
async def test_send_test_alert_does_not_write_outbox_or_tokens(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    cfg = settings_for(tmp_path)
    with patch("yc_monitor.slack_app.AsyncWebClient") as client_cls:
        client = MagicMock()
        client.chat_postMessage = AsyncMock()
        client_cls.return_value = client
        result = await send_test_alert(cfg, db)
    assert result == {"status": "sent", "channel": "C123", "demo": True}
    assert "token" not in str(result).lower()
    assert db.status()["outbox"] == {}
    posted_kwargs = client.chat_postMessage.await_args.kwargs
    assert posted_kwargs["text"].startswith("DEMO |")
    assert "DEMO ALERT" in str(posted_kwargs["blocks"])
