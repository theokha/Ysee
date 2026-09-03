from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from yc_monitor.classify import normalize_company
from yc_monitor.models import Alert, AlertKind, CanonicalItem, Source

HEADERS = {
    AlertKind.EARLY_FOUNDER: "EARLY YC ACCEPTANCE | Founder announced before YC",
    AlertKind.EARLY_YC_LAUNCH: "EARLY YC LAUNCH | Not yet listed by YC",
    AlertKind.EARLY_SPEEDRUN_LAUNCH: "EARLY SPEEDRUN LAUNCH | Not yet listed by a16z",
    AlertKind.OFFICIAL_YC: "NEW YC COMPANY",
    AlertKind.OFFICIAL_SPEEDRUN: "NEW a16z SPEEDRUN COMPANY",
}

STATUSES = {
    AlertKind.EARLY_FOUNDER: "Founder announced YC acceptance / not yet officially listed",
    AlertKind.EARLY_YC_LAUNCH: "Founder launched current-batch YC company / not yet officially listed",
    AlertKind.EARLY_SPEEDRUN_LAUNCH: "Founder announced a16z Speedrun company / not yet officially listed",
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

    if alert.kind in {
        AlertKind.EARLY_FOUNDER,
        AlertKind.EARLY_YC_LAUNCH,
        AlertKind.EARLY_SPEEDRUN_LAUNCH,
    } and item.content_text.strip():
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


LEADS_MAX_PER_SECTION = 10

_COMPANY_KEY_PREFIXES = ("early:", "yc:", "speedrun:", "launch:")


@dataclass(slots=True)
class _Lead:
    dedup_key: str
    source: str
    item_id: str
    company_name: str | None
    disposition: str
    reason: str
    first_seen_at: str | None
    alerted_at: str | None

    @property
    def timestamp(self) -> datetime:
        return _parse_iso(self.alerted_at) or _parse_iso(self.first_seen_at) or datetime.now(UTC)

    @property
    def is_company_row(self) -> bool:
        return self.dedup_key.startswith(_COMPANY_KEY_PREFIXES)

    def label(self) -> str:
        if self.company_name:
            return self.company_name
        if self.source == Source.TWITTER.value:
            return "X post"
        if self.source == Source.LINKEDIN.value:
            return "LinkedIn post"
        return self.dedup_key

    def source_label(self) -> str:
        try:
            return _source_label(Source(self.source))
        except ValueError:
            return self.source


def format_leads_blocks(leads: list[dict[str, Any]]) -> list[dict[str, object]]:
    """Render recent seen_items rows as a grouped, deduplicated leads summary.

    Company rows (early:/yc:/speedrun:/launch:) become one "Alerted" line each;
    the post rows backing those alerts are folded away so one detection never
    renders twice. Rows parked by a review gate land in "Review queue".
    """
    parsed = [
        _Lead(
            dedup_key=str(row.get("dedup_key") or ""),
            source=str(row.get("source") or ""),
            item_id=str(row.get("item_id") or ""),
            company_name=(str(row["company_name"]) if row.get("company_name") else None),
            disposition=str(row.get("disposition") or ""),
            reason=str(row.get("reason") or ""),
            first_seen_at=(str(row["first_seen_at"]) if row.get("first_seen_at") else None),
            alerted_at=(str(row["alerted_at"]) if row.get("alerted_at") else None),
        )
        for row in leads
    ]

    # A social signal stores two linked rows sharing an item_id: the post row
    # (twitter:<id>, disposition evidence) and the company row (early:<name>).
    # Keep the company row, fold the post row away.
    alerted_item_ids = {lead.item_id for lead in parsed if lead.is_company_row}
    company_rows = [
        lead
        for lead in parsed
        if lead.is_company_row and lead.disposition in {"alerted", "pending"}
    ]
    review_rows = [
        lead
        for lead in parsed
        if lead.disposition == "review"
        and not (lead.item_id and lead.item_id in alerted_item_ids)
    ]

    alerted = _collapse_by_company(company_rows)[:LEADS_MAX_PER_SECTION]
    review = _collapse_by_company(review_rows)[:LEADS_MAX_PER_SECTION]

    if not alerted and not review:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": "No leads yet."}}]

    sections: list[str] = []
    if alerted:
        lines = [f"*Alerted ({len(alerted)})*"]
        lines.extend(f"• {_alerted_line(lead)}" for lead in alerted)
        sections.append("\n".join(lines))
    if review:
        lines = [f"*Review queue ({len(review)})*"]
        lines.extend(f"• {_review_line(lead)}" for lead in review)
        sections.append("\n".join(lines))

    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Recent leads"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n\n".join(sections)}},
    ]


def _alerted_line(lead: _Lead) -> str:
    date = lead.timestamp.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%b %-d")
    return f"{lead.label()} - {lead.source_label()}, {date}"


def _review_line(lead: _Lead) -> str:
    reason = lead.reason.removeprefix("gpt_review:") or "needs review"
    date = lead.timestamp.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%b %-d")
    return f"{lead.label()} - {reason}, {date}"


def _collapse_by_company(rows: list[_Lead]) -> list[_Lead]:
    best: dict[str, _Lead] = {}
    for lead in rows:
        key = normalize_company(lead.company_name) or lead.dedup_key
        current = best.get(key)
        if current is None or lead.timestamp > current.timestamp:
            best[key] = lead
    return sorted(best.values(), key=lambda lead: lead.timestamp, reverse=True)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


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
