"""Background slash-command work: `/yc scan`, `/yc scan dry`, `review`, `retry`.

Slack gives a command 3 seconds to answer, but these actions run for minutes.
The route acknowledges immediately and `_run_background_scan` does the work on
the event loop, then DMs the result with `SlackNotifier.post_ephemeral`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from slack_sdk.errors import SlackApiError

from yc_monitor.config import Settings
from yc_monitor.pipeline import MonitorPipeline
from yc_monitor.pond_server import _run_background_scan
from yc_monitor.slack_app import ACK_TEXTS, SlackNotifier, handle_slash_command


def payload_for(**overrides: str) -> dict[str, str]:
    values = {"user_id": "U1", "channel_id": "C1", "text": "scan"}
    values.update(overrides)
    return values


def make_pipeline(tmp_path) -> tuple[MonitorPipeline, list[str]]:
    settings = Settings(
        database_path=str(tmp_path / "state.db"),
        openai_api_key=None,
        # Never let a developer's real .env Slack token post during tests.
        slack_bot_token=None,
        slack_channel_id=None,
        slack_ops_channel_id=None,
    )
    pipeline = MonitorPipeline(settings)
    sent: list[str] = []

    async def fake_ephemeral(user_id: str, channel_id: str, text: str) -> None:
        sent.append((user_id, channel_id, text))  # type: ignore[arg-type]

    pipeline.notifier.post_ephemeral = fake_ephemeral  # type: ignore[method-assign]
    return pipeline, sent


# --- work is dispatched per action and the result is DM'd --------------------


@pytest.mark.asyncio
async def test_scan_action_runs_live_cycle_and_posts_summary(tmp_path) -> None:
    pipeline, sent = make_pipeline(tmp_path)
    with patch.object(
        MonitorPipeline,
        "run",
        AsyncMock(return_value={"alert_count": 3, "delivered_count": 2}),
    ) as run:
        await _run_background_scan("scan_requested", payload_for(), pipeline)

    run.assert_awaited_once_with(dry_run=False)
    assert sent == [("U1", "C1", "Scan complete. 3 new alert(s), 2 delivered.")]


@pytest.mark.asyncio
async def test_dry_scan_action_runs_dry_cycle_and_posts_summary(tmp_path) -> None:
    pipeline, sent = make_pipeline(tmp_path)
    with patch.object(
        MonitorPipeline, "run", AsyncMock(return_value={"alert_count": 1})
    ) as run:
        await _run_background_scan("dry_scan_requested", payload_for(), pipeline)

    run.assert_awaited_once_with(dry_run=True)
    assert sent == [("U1", "C1", "Dry scan complete. 1 candidate(s), nothing posted.")]


@pytest.mark.asyncio
async def test_review_action_rejudges_queue_and_posts_summary(tmp_path) -> None:
    pipeline, sent = make_pipeline(tmp_path)
    with patch.object(
        MonitorPipeline,
        "rejudge_review_queue",
        AsyncMock(
            return_value={
                "reviewed": 2,
                "promoted": ["twitter:1"],
                "cleared": 1,
                "deferred": 0,
                "promoted_names": ["Harbor"],
            }
        ),
    ) as rejudge:
        await _run_background_scan("review_requested", payload_for(), pipeline)

    rejudge.assert_awaited_once_with(25)
    assert sent and "Re-reviewed 2 queued post(s)" in sent[0][2]
    assert "1 promoted (and sent)" in sent[0][2]
    assert "New alerts: Harbor" in sent[0][2]


@pytest.mark.asyncio
async def test_retry_action_drains_outbox_and_posts_confirmation(tmp_path) -> None:
    pipeline, sent = make_pipeline(tmp_path)
    with patch.object(
        MonitorPipeline, "deliver_outbox", AsyncMock(return_value=4)
    ) as deliver:
        await _run_background_scan("retry_requested", payload_for(), pipeline)

    deliver.assert_awaited_once_with()
    assert sent == [("U1", "C1", "Retried outbox. Delivered 4 message(s).")]


@pytest.mark.asyncio
async def test_unknown_action_is_ignored(tmp_path) -> None:
    pipeline, sent = make_pipeline(tmp_path)
    with patch.object(MonitorPipeline, "run", AsyncMock()) as run:
        await _run_background_scan("leads_requested", payload_for(), pipeline)

    run.assert_not_awaited()
    assert sent == []


# --- failures reach the user instead of vanishing with the task --------------


@pytest.mark.asyncio
async def test_pipeline_failure_posts_safe_message_without_traceback(tmp_path) -> None:
    pipeline, sent = make_pipeline(tmp_path)
    with patch.object(
        MonitorPipeline, "run", AsyncMock(side_effect=RuntimeError("boom"))
    ):
        await _run_background_scan("scan_requested", payload_for(), pipeline)

    assert len(sent) == 1
    text = sent[0][2]
    assert "failed" in text.lower()
    assert "RuntimeError" in text
    assert "boom" not in text  # exception internals never leak to Slack


@pytest.mark.asyncio
async def test_notifier_failure_after_success_is_swallowed(tmp_path) -> None:
    pipeline, _ = make_pipeline(tmp_path)
    with patch.object(
        MonitorPipeline, "run", AsyncMock(return_value={"alert_count": 0})
    ):

        async def exploding_ephemeral(user_id: str, channel_id: str, text: str) -> None:
            raise SlackApiError("slack down", {"ok": False, "error": "internal_error"})

        pipeline.notifier.post_ephemeral = exploding_ephemeral  # type: ignore[method-assign]
        # Must not raise: the work already succeeded and the ack already went out.
        await _run_background_scan("scan_requested", payload_for(), pipeline)


# --- SlackNotifier.post_ephemeral --------------------------------------------


@pytest.mark.asyncio
async def test_post_ephemeral_sends_channel_user_and_text(tmp_path) -> None:
    notifier = SlackNotifier(
        Settings(
            database_path=str(tmp_path / "state.db"),
            slack_bot_token="xoxb-test",
            slack_channel_id="C123",
        )
    )
    with patch("yc_monitor.slack_app.AsyncWebClient") as client_cls:
        client_cls.return_value.chat_postEphemeral = AsyncMock()
        await notifier.post_ephemeral("U1", "C42", "Scan complete.")

    client_cls.assert_called_once_with(token="xoxb-test")
    client_cls.return_value.chat_postEphemeral.assert_awaited_once_with(
        channel="C42", user="U1", text="Scan complete."
    )


@pytest.mark.asyncio
async def test_post_ephemeral_uses_install_token_and_swallows_errors(tmp_path) -> None:
    database = _DatabaseStub("xoxb-installed")
    notifier = SlackNotifier(
        Settings(database_path=str(tmp_path / "state.db"), slack_bot_token=None),
        database,
    )
    with patch("yc_monitor.slack_app.AsyncWebClient") as client_cls:
        client_cls.return_value.chat_postEphemeral = AsyncMock(
            side_effect=SlackApiError("nope", {"ok": False, "error": "not_in_channel"})
        )
        await notifier.post_ephemeral("U1", "C42", "hello")

    client_cls.assert_called_once_with(token="xoxb-installed")  # install token preferred


@pytest.mark.asyncio
async def test_post_ephemeral_without_token_is_a_noop(tmp_path) -> None:
    notifier = SlackNotifier(
        Settings(
            database_path=str(tmp_path / "state.db"),
            slack_bot_token=None,
            slack_channel_id=None,
        )
    )
    with patch("yc_monitor.slack_app.AsyncWebClient") as client_cls:
        await notifier.post_ephemeral("U1", "C42", "hello")

    client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_post_ephemeral_without_user_or_channel_is_a_noop(tmp_path) -> None:
    notifier = SlackNotifier(
        Settings(
            database_path=str(tmp_path / "state.db"),
            slack_bot_token="xoxb-test",
            slack_channel_id="C123",
        )
    )
    with patch("yc_monitor.slack_app.AsyncWebClient") as client_cls:
        await notifier.post_ephemeral("", "", "hello")
        await notifier.post_ephemeral("U1", "", "hello")

    client_cls.assert_not_called()


# --- immediate acks returned by handle_slash_command -------------------------


def test_handle_slash_command_returns_background_acks() -> None:
    assert handle_slash_command("/yc", "scan", {})["text"] == ACK_TEXTS["scan_requested"]
    assert handle_slash_command("/yc", "scan dry", {})["text"] == ACK_TEXTS["dry_scan_requested"]
    assert handle_slash_command("/yc", "review", {})["text"] == ACK_TEXTS["review_requested"]
    assert handle_slash_command("/yc", "retry", {})["text"] == ACK_TEXTS["retry_requested"]
    for text in ACK_TEXTS.values():
        assert "started" in text or "follow" in text


class _DatabaseStub:
    """Duck-typed Database exposing only the token lookup."""

    def __init__(self, token: str) -> None:
        self._token = token

    def latest_slack_bot_token(self) -> str | None:
        return self._token
