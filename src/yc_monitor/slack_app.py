from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import parse_qs

from slack_sdk.web.async_client import AsyncWebClient

from yc_monitor.config import Settings
from yc_monitor.db import Database
from yc_monitor.models import Alert
from yc_monitor.slack_format import build_demo_alert, format_alert, format_status_blocks

logger = logging.getLogger(__name__)


class SlackNotifier:
    def __init__(self, settings: Settings, db: Database | None = None) -> None:
        self.settings = settings
        self.channel = settings.slack_channel_id
        self.db = db

    def _bot_token(self) -> str | None:
        if self.settings.slack_bot_token:
            return self.settings.slack_bot_token
        if self.db is not None:
            return self.db.latest_slack_bot_token()
        return None

    @property
    def configured(self) -> bool:
        return bool(self._bot_token()) and bool(self.channel)

    async def send(
        self, alert: Alert, demo: bool = False, thread_ts: str | None = None
    ) -> dict[str, str] | None:
        token = self._bot_token()
        if not token or not self.channel:
            return None
        text, blocks = format_alert(alert, demo=demo)
        client = AsyncWebClient(token=token)
        kwargs: dict[str, Any] = {
            "channel": self.channel,
            "text": text,
            "blocks": blocks,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        response = await client.chat_postMessage(**kwargs)
        payload = getattr(response, "data", response)
        data = payload if isinstance(payload, dict) else {}
        ts = str(data.get("ts") or "")
        channel = str(data.get("channel") or self.channel)
        if not ts:
            return {"channel": channel}
        return {"channel": channel, "ts": ts}

    async def post_ephemeral(self, user_id: str, channel_id: str, text: str) -> None:
        """Post a follow-up only the invoking user can see.

        Slash-command work that outlives the 3-second Slack ack posts its
        results here. Failures are swallowed (logged) rather than raised: a
        missing token or a Slack hiccup must not turn into an unhandled
        background-task error after the command already acknowledged.
        """
        if not user_id or not channel_id:
            logger.warning("Skipping ephemeral follow-up: missing user or channel")
            return
        token = self._bot_token()
        if not token:
            return
        client = AsyncWebClient(token=token)
        try:
            await client.chat_postEphemeral(channel=channel_id, user=user_id, text=text)
        except Exception:  # noqa: BLE001 -- follow-ups must never crash the task
            logger.warning("Failed to post ephemeral follow-up")


async def send_test_alert(settings: Settings, db: Database) -> dict[str, Any]:
    notifier = SlackNotifier(settings, db)
    if not notifier.configured:
        return {"status": "skipped", "reason": "slack_not_configured", "demo": True}
    posted = await notifier.send(build_demo_alert(), demo=True)
    return {
        "status": "sent" if posted else "failed",
        "channel": settings.slack_channel_id,
        "demo": True,
    }


def verify_slack_signature(
    signing_secret: str, timestamp: str, body: bytes, signature: str
) -> bool:
    if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > 60 * 5:
        return False
    basestring = f"v0:{timestamp}:{body.decode('utf-8')}".encode()
    digest = hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)


def slash_command_payload(body: bytes) -> dict[str, str]:
    parsed = parse_qs(body.decode(), keep_blank_values=True)
    return {key: values[0] if values else "" for key, values in parsed.items()}


# The slow actions (`/yc scan`, `scan dry`, `review`, `retry`) cannot finish
# inside Slack's 3-second command timeout, so handle_slash_command answers with
# one of these acks and the server routes the real work to a background task.
# Keyed by internal action id; the route matches on the ack text to decide what
# to schedule, so a denied or unknown command never gets backgrounded.
ACK_TEXTS = {
    "scan_requested": "Live scan started. Results will arrive here in a few minutes.",
    "dry_scan_requested": "Dry scan started. Results will arrive here shortly.",
    "review_requested": "Review re-judge started. Results will arrive here shortly.",
    "retry_requested": "Retry started. Confirmation will follow.",
}


def handle_slash_command(
    command: str,
    text: str,
    status: dict[str, Any],
    user_id: str = "",
    db: Database | None = None,
    admin_users: set[str] | None = None,
) -> dict[str, Any]:
    if command not in {"/yc", "/yc-status"}:
        return {
            "response_type": "ephemeral",
            "text": "Unknown command. Use `/yc status` or `/yc`.",
        }
    parts = text.strip().split()
    action = parts[0].lower() if parts else "status"
    if action in {"", "status"}:
        return {
            "response_type": "ephemeral",
            "text": "YC Launch Monitor status",
            "blocks": format_status_blocks(status),
        }
    if action == "help":
        return {
            "response_type": "ephemeral",
            "text": (
                "Commands:\n"
                "`/yc` or `/yc status` - monitor status\n"
                "`/yc config` - show adjustable settings\n"
                "`/yc config set <key> <value>` - change a setting (admin)\n"
                "`/yc config reset <key>` - back to .env default (admin)\n"
                "`/yc scan` - run a live cycle now (admin)\n"
                "`/yc scan dry` - dry-run a cycle, nothing posted (admin)\n"
                "`/yc review` - re-judge queued review posts with current filters (admin)\n"
                "`/yc leads` - recent detections\n"
                "`/yc retry` - retry failed Slack deliveries\n"
                "Scans, review, and retry run in the background; the result is DM'd to you when done."
            ),
        }
    if action == "config":
        return _handle_config(parts[1:], user_id, db, admin_users)
    if action in {"scan", "review", "leads", "retry"}:
        if action in {"scan", "review"} and admin_users is not None and user_id not in admin_users:
            return {
                "response_type": "ephemeral",
                "text": "Only admins can trigger scans (they spend API budget).",
            }
        if action == "scan":
            dry = len(parts) > 1 and parts[1].lower() == "dry"
            key = "dry_scan_requested" if dry else "scan_requested"
            return {"response_type": "ephemeral", "text": ACK_TEXTS[key]}
        if action == "review":
            return {"response_type": "ephemeral", "text": ACK_TEXTS["review_requested"]}
        if action == "leads":
            return {"response_type": "ephemeral", "text": "leads_requested"}
        return {"response_type": "ephemeral", "text": ACK_TEXTS["retry_requested"]}
    return {
        "response_type": "ephemeral",
        "text": "Unknown command. Try `/yc help`.",
    }


def _handle_config(
    args: list[str],
    user_id: str,
    db: Database | None,
    admin_users: set[str] | None,
) -> dict[str, Any]:
    from yc_monitor.runtime_settings import SETTING_SPECS, format_config_block

    sub = args[0].lower() if args else "show"
    if sub in {"", "show", "list"}:
        if db is None:
            return {"response_type": "ephemeral", "text": "Config store unavailable."}
        return {
            "response_type": "ephemeral",
            "text": "Adjustable settings",
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": format_config_block(db, _settings())},
                }
            ],
        }
    if sub not in {"set", "reset"}:
        return {
            "response_type": "ephemeral",
            "text": "Usage: `/yc config`, `/yc config set <key> <value>`, `/yc config reset <key>`.",
        }
    if admin_users is not None and user_id not in admin_users:
        return {
            "response_type": "ephemeral",
            "text": "Only admins can change settings.",
        }
    if db is None:
        return {"response_type": "ephemeral", "text": "Config store unavailable."}
    if len(args) < 2 or args[1] not in SETTING_SPECS:
        keys = ", ".join(sorted(SETTING_SPECS))
        return {
            "response_type": "ephemeral",
            "text": f"Unknown key. Adjustable keys: {keys}",
        }
    key = args[1]
    spec = SETTING_SPECS[key]
    if sub == "reset":
        db.reset_runtime_setting(key)
        return {
            "response_type": "ephemeral",
            "text": f"Reset {key} to the .env default. Applies at the next cycle.",
        }
    if len(args) < 3:
        return {
            "response_type": "ephemeral",
            "text": f"Usage: `/yc config set {key} <value>`.",
        }
    raw_value = " ".join(args[2:])
    try:
        coerced = spec.coerce(raw_value)
    except ValueError as exc:
        return {
            "response_type": "ephemeral",
            "text": f"Invalid value for {key}: {exc}",
        }
    db.set_runtime_setting(key, str(coerced))
    return {
        "response_type": "ephemeral",
        "text": f"Set {key} = {coerced}. Applies at the next cycle.",
    }


def _settings() -> Any:
    from yc_monitor.config import get_settings

    return get_settings()
