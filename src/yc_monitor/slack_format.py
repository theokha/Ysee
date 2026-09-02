from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from yc_monitor.models import Alert, AlertKind, CanonicalItem, Source

HEADERS = {
    AlertKind.EARLY_FOUNDER: "EARLY YC SIGNAL | Founder announced before YC",
    AlertKind.OFFICIAL_YC: "NEW YC COMPANY",
    AlertKind.OFFICIAL_SPEEDRUN: "NEW a16z SPEEDRUN COMPANY",
}

STATUSES = {
    AlertKind.EARLY_FOUNDER: "Founder announced / not yet officially listed by YC",
    AlertKind.OFFICIAL_YC: "Confirmed by YC",
    AlertKind.OFFICIAL_SPEEDRUN: "Confirmed by a16z Speedrun",
}


def format_alert(alert: Alert, demo: bool = False) -> tuple[str, list[dict[str, object]]]:
    item = alert.item
    header = HEADERS[alert.kind]
    if alert.upgrade_from:
        header = (
            "YC CONFIRMED | previously an early founder signal"
            if alert.kind == AlertKind.OFFICIAL_YC
            else "SPEEDRUN CONFIRMED | previously an early founder signal"
        )
    if demo:
        header = f"DEMO | {header}"

    fields = [
        f"*Company:*\n{item.company_name or 'Not extracted'}",
        f"*Source:*\n{_source_label(item.source)}",
        f"*Status:*\n{STATUSES[alert.kind]}",
    ]
    if item.founder_name or item.founder_handle:
        founder = item.founder_name or "Founder"
        if item.founder_handle:
            founder += f" (@{item.founder_handle.lstrip('@')})"
        fields.append(f"*Founder:*\n{founder}")
    if item.batch:
        fields.append(f"*Batch:*\n{item.batch}")

    detected = alert.detected_at.astimezone(ZoneInfo("America/Los_Angeles")).strftime(
        "%b. %-d, %Y, %-I:%M %p PT"
    )
    blocks: list[dict[str, object]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header[:150]}},
        {"type": "section", "fields": [{"type": "mrkdwn", "text": value} for value in fields]},
    ]
    if demo:
        blocks.insert(
            1,
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "*DEMO ALERT* | this is not a real YC detection."}
                ],
            },
        )
    if alert.upgrade_note:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": alert.upgrade_note}],
            }
        )

    if alert.kind == AlertKind.EARLY_FOUNDER and item.content_text.strip():
        quote = item.content_text.strip().replace("\n", " ")[:500]
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Original post:*\n> {quote}"},
            }
        )
    else:
        description = item.description or item.content_text or "No description available."
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Description:*\n{description[:2500]}"},
            }
        )

    actions = _action_buttons(item)
    if actions:
        blocks.append({"type": "actions", "elements": actions})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Detected {detected}"}]})
    return header, blocks


def _source_label(source: Source) -> str:
    return {
        Source.YC_DIRECTORY: "YC Directory",
        Source.YC_SPEEDRUN: "a16z Speedrun",
        Source.YC_LAUNCHES: "Launch YC",
        Source.TWITTER: "X",
        Source.LINKEDIN: "LinkedIn",
    }[source]


def _action_buttons(item: CanonicalItem) -> list[dict[str, object]]:
    buttons: list[dict[str, object]] = []
    seen: set[str] = set()

    def add(label: str, url: str | None, style: str | None = None) -> None:
        if not url or not url.startswith("http") or url in seen:
            return
        seen.add(url)
        element: dict[str, object] = {
            "type": "button",
            "text": {"type": "plain_text", "text": label[:75]},
            "url": url,
        }
        if style:
            element["style"] = style
        buttons.append(element)

    if item.source in {Source.TWITTER, Source.LINKEDIN}:
        add("Open original post", item.canonical_url, "primary")
        add("Company site", item.company_url)
        add("Author profile", item.author_url)
    else:
        add("Open directory profile", item.canonical_url, "primary")
        add("Company site", item.company_url)
    return buttons[:5]


def format_status_blocks(status: dict[str, Any]) -> list[dict[str, object]]:
    seen_raw = status.get("seen")
    outbox_raw = status.get("outbox")
    gpt_raw = status.get("gpt")
    last_raw = status.get("last_run")
    seen: dict[str, Any] = seen_raw if isinstance(seen_raw, dict) else {}
    outbox: dict[str, Any] = outbox_raw if isinstance(outbox_raw, dict) else {}
    gpt: dict[str, Any] = gpt_raw if isinstance(gpt_raw, dict) else {}
    last: dict[str, Any] = last_raw if isinstance(last_raw, dict) else {}
    lines = [
        f"*YC companies tracked:* {status.get('official_yc_companies', 0)}",
        f"*Last run:* {last.get('status') or 'none'} ({last.get('finished_at') or 'n/a'})",
        f"*Next run:* {status.get('next_run_at') or 'not scheduled'}",
        f"*Alerts:* sent {outbox.get('sent', 0)}, pending {outbox.get('pending', 0)}, failed {outbox.get('failed', 0)}",
        f"*Seen:* alerted {seen.get('alerted', 0)}, rejected {seen.get('rejected', 0)}, evidence {seen.get('evidence', 0)}",
    ]
    if gpt:
        lines.append(
            f"*GPT last cycle:* {gpt.get('calls', 0)} calls, "
            f"{gpt.get('accepted', 0)} accepted, {gpt.get('rejected', 0)} rejected"
        )
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "YC Launch Monitor status"},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
    ]


def build_demo_alert() -> Alert:
    item = CanonicalItem(
        source=Source.YC_DIRECTORY,
        item_id="demo-alert",
        company_name="Demo Company",
        canonical_url="https://www.ycombinator.com/companies",
        description=(
            "DEMO only. This message verifies Slack delivery and is not a real YC company detection."
        ),
        batch="DEMO",
    )
    return Alert(kind=AlertKind.OFFICIAL_YC, item=item, dedup_key="demo:test-alert")
