from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import parse_qs

from slack_sdk.web.async_client import AsyncWebClient

from yc_monitor.config import Settings
from yc_monitor.db import Database
from yc_monitor.models import Alert
from yc_monitor.slack_format import build_demo_alert, format_alert, format_status_blocks


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


def handle_slash_command(command: str, text: str, status: dict[str, Any]) -> dict[str, Any]:
    if command not in {"/yc", "/yc-status"}:
        return {
            "response_type": "ephemeral",
            "text": "Unknown command. Use `/yc status` or `/yc`.",
        }
    argument = text.strip().lower()
    parts = argument.split()
    action = parts[0] if parts else "status"
    if action in {"", "status"}:
        return {
            "response_type": "ephemeral",
            "text": "YC Launch Monitor status",
            "blocks": format_status_blocks(status),
        }
    if action == "help":
        return {
            "response_type": "ephemeral",
            "text": "Supported: `/yc`, `/yc status`, `/yc scan dry`, `/yc leads`, `/yc retry`.",
        }
    if action == "scan":
        dry = len(parts) > 1 and parts[1] == "dry"
        return {
            "response_type": "ephemeral",
            "text": "dry_scan_requested" if dry else "scan_requested",
        }
    if action == "leads":
        return {"response_type": "ephemeral", "text": "leads_requested"}
    if action == "retry":
        return {"response_type": "ephemeral", "text": "retry_requested"}
    return {
        "response_type": "ephemeral",
        "text": "Supported: `/yc`, `/yc status`, `/yc scan dry`, `/yc leads`, `/yc retry`.",
    }
