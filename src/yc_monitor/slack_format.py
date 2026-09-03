from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
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

# The rare early signals are the point of the product; official listings are
# routine and high-volume. Without a visual difference the valuable alert reads
# the same as the noise in a busy channel.
KIND_ICONS = {
    AlertKind.EARLY_FOUNDER: "\U0001f6a8",  # rotating light
    AlertKind.EARLY_YC_LAUNCH: "\U0001f680",  # rocket
    AlertKind.EARLY_SPEEDRUN_LAUNCH: "\U0001f680",
    AlertKind.OFFICIAL_YC: "\U0001f4cb",  # clipboard
    AlertKind.OFFICIAL_SPEEDRUN: "\U0001f4cb",
}

STATUSES = {
    AlertKind.EARLY_FOUNDER: "Founder announced YC acceptance / not yet officially listed",
    AlertKind.EARLY_YC_LAUNCH: "Founder launched current-batch YC company / not yet officially listed",
    AlertKind.EARLY_SPEEDRUN_LAUNCH: "Founder announced a16z Speedrun company / not yet officially listed",
    AlertKind.OFFICIAL_YC: "Confirmed by YC",
    AlertKind.OFFICIAL_SPEEDRUN: "Confirmed by a16z Speedrun",
}

# Internal gate identifiers leak straight into `/yc leads` without this. Keys are
# the reason strings minus their `gpt_review:` / `gpt_confirmed:` prefix; an
# unmapped reason falls back to the raw slug with underscores spaced out.
REASON_LABELS = {
    "founder_self_announcement": "Founder announced it themselves",
    "contradictory_verdict": "Model gave a contradictory verdict",
    "third_party_low_confidence": "Third-party report, low confidence",
    "third_party_missing_batch": "Third-party report, no batch code",
    "generic_or_missing_company": "Company name generic or missing",
    "unsupported_evidence": "Quoted evidence not found in the post",
    "invalid_or_missing_batch": "Batch code invalid or missing",
    "unresolved_company_handle": "Could not verify the company handle",
    "unresolved_product_owner": "Could not tell who owns the product",
    "not_first_party": "Not posted by the founder",
    "not_current": "Not a current announcement",
    "not_acceptance": "Not an acceptance announcement",
    "product_launch_only": "Product launch only, no acceptance",
    "retrospective": "Looking back, not new news",
    "satire_or_joke": "Reads as satire or a joke",
    "missing_company": "No company name given",
    "missing_evidence": "No supporting quote",
    "needs_review": "Needs a human look",
}


def humanize_reason(reason: str) -> str:
    """Turn an internal reason slug into something a reader can act on."""
    slug = reason.split(":", 1)[-1].strip() if ":" in reason else reason.strip()
    if not slug:
        return "Needs a human look"
    mapped = REASON_LABELS.get(slug)
    if mapped:
        return mapped
    # A free-text model reason (gpt_confirmed:<prose>) is already readable;
    # only bare snake_case slugs need unpacking.
    if " " in slug:
        return slug
    return slug.replace("_", " ").capitalize()


def _slack_date(moment: datetime, fallback_format: str) -> str:
    """Render a timestamp in each viewer's own timezone.

    Slack substitutes `<!date^...>` per-viewer; the text after the pipe is the
    fallback for surfaces that do not (push notifications, exports, email).
    """
    unix = int(moment.timestamp())
    fallback = moment.astimezone(ZoneInfo("America/Los_Angeles")).strftime(fallback_format)
    return f"<!date^{unix}^{{date_short_pretty}} at {{time}}|{fallback}>"


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
    header = f"{KIND_ICONS[alert.kind]} {header}"

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

    detected = _slack_date(alert.detected_at, "%b. %-d, %Y, %-I:%M %p PT")
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

    if alert.kind in _EARLY_KINDS and item.content_text.strip():
        quote = _truncate(item.content_text.strip().replace("\n", " "), 500)
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
                "text": {"type": "mrkdwn", "text": f"*Description:*\n{_truncate(description, 2500)}"},
            }
        )

    actions = _action_buttons(item)
    if actions:
        blocks.append({"type": "actions", "elements": actions})

    # An early signal is a judgement call, so show the judgement next to it: a
    # reader can weigh a 0.91 differently from a 0.66. Official listings are
    # facts from the catalog, not inferences, so they carry no score.
    footer = f"Detected {detected}"
    if alert.kind in _EARLY_KINDS:
        footer = f"Confidence {alert.confidence:.2f} · {footer}"
        if alert.reason:
            footer = f"{humanize_reason(alert.reason)} · {footer}"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})

    # `text` is the push notification, sidebar preview and screen-reader line.
    # The header alone makes every alert of a kind look identical on a phone,
    # so lead with the company name — the one token worth waking up for.
    notification = f"{header} — {item.company_name}" if item.company_name else header
    return notification, blocks


_EARLY_KINDS = frozenset(
    {
        AlertKind.EARLY_FOUNDER,
        AlertKind.EARLY_YC_LAUNCH,
        AlertKind.EARLY_SPEEDRUN_LAUNCH,
    }
)


def _truncate(text: str, limit: int) -> str:
    """Cut to `limit`, flagging that a cut happened.

    Silent truncation reads as the whole thing; a reader has no way to tell a
    complete 500-character post from a clipped one.
    """
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


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


# An `early:` row is a founder signal the model judged; the rest are catalog
# listings. The distinction is the product, so it leads every line.
_EARLY_KEY_PREFIX = "early:"

LEAD_ICON_EARLY = "\U0001f6a8"  # rotating light
LEAD_ICON_OFFICIAL = "\U0001f4cb"  # clipboard
LEAD_ICON_REVIEW = "❓"  # question mark


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
    canonical_url: str | None = None
    author_name: str | None = None
    author_handle: str | None = None
    snippet: str | None = None

    @property
    def timestamp(self) -> datetime:
        return _parse_iso(self.alerted_at) or _parse_iso(self.first_seen_at) or datetime.now(UTC)

    @property
    def is_company_row(self) -> bool:
        return self.dedup_key.startswith(_COMPANY_KEY_PREFIXES)

    @property
    def is_early(self) -> bool:
        return self.dedup_key.startswith(_EARLY_KEY_PREFIX)

    def label(self) -> str:
        if self.company_name:
            return self.company_name
        if self.author_name:
            return self.author_name
        if self.source == Source.TWITTER.value:
            return "X post"
        if self.source == Source.LINKEDIN.value:
            return "LinkedIn post"
        return self.dedup_key

    def linked_label(self) -> str:
        """Bold company name, hyperlinked when we know where the lead came from."""
        text = _escape_mrkdwn(self.label())
        url = self.canonical_url
        if url and url.startswith("http"):
            return f"<{_escape_url(url)}|*{text}*>"
        return f"*{text}*"

    def attribution(self) -> str | None:
        """Who announced it -- only meaningful for a social post."""
        if self.source not in {Source.TWITTER.value, Source.LINKEDIN.value}:
            return None
        if self.author_handle:
            return f"@{_escape_mrkdwn(self.author_handle)}"
        if self.author_name and self.author_name != self.label():
            return _escape_mrkdwn(self.author_name)
        return None

    def excerpt(self) -> str | None:
        if not self.snippet:
            return None
        collapsed = " ".join(self.snippet.split())
        if not collapsed:
            return None
        return _truncate(_escape_mrkdwn(collapsed), REVIEW_EXCERPT_MAX)

    def source_label(self) -> str:
        try:
            return _source_label(Source(self.source))
        except ValueError:
            return self.source


def _escape_mrkdwn(text: str) -> str:
    """Slack decodes &, < and > in mrkdwn, so a raw name can break a link."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_url(url: str) -> str:
    # Inside a <url|label> link only the delimiters need neutralizing.
    return url.replace("<", "%3C").replace(">", "%3E").replace("|", "%7C")


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
            canonical_url=(str(row["canonical_url"]) if row.get("canonical_url") else None),
            author_name=(str(row["author_name"]) if row.get("author_name") else None),
            author_handle=(str(row["author_handle"]) if row.get("author_handle") else None),
            snippet=(str(row["snippet"]) if row.get("snippet") else None),
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

    all_alerted = _collapse_by_company(company_rows)
    all_review = _collapse_by_company(review_rows)
    alerted = all_alerted[:LEADS_MAX_PER_SECTION]
    review = all_review[:LEADS_MAX_PER_SECTION]

    if not alerted and not review:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": "No leads yet."}}]

    sections: list[str] = []
    if alerted:
        # Count the full set, not the shown slice: "Alerted (10)" over a capped
        # list reads as "there are ten", which is the wrong conclusion.
        lines = [f"*Alerted ({len(all_alerted)})*"]
        lines.extend(_grouped_by_day(alerted, _alerted_line))
        lines.extend(_overflow_note(len(all_alerted), len(alerted)))
        sections.append("\n".join(lines))
    if review:
        lines = [f"*Review queue ({len(all_review)})*"]
        lines.extend(_grouped_by_day(review, _review_line))
        lines.extend(_overflow_note(len(all_review), len(review)))
        sections.append("\n".join(lines))

    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Recent leads"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n\n".join(sections)}},
    ]


def _grouped_by_day(
    rows: list[_Lead], render: Callable[[_Lead], str]
) -> list[str]:
    """Emit one date subheader per day instead of repeating it on every row.

    The rows arrive newest-first, so the runs are already contiguous. The date
    moves off the line entirely, which frees the tail for the attribution that
    actually distinguishes two leads found on the same day.
    """
    lines: list[str] = []
    current: date | None = None
    for lead in rows:
        day = _day_key(lead.timestamp)
        if day != current:
            lines.append(f"_{_slack_date(lead.timestamp, '%b %-d')}_")
            current = day
        lines.append(render(lead))
    return lines


# Day boundaries have to be drawn in some timezone; PT is the one YC and the
# rest of this formatter already fall back to. Slack still renders each heading
# per-viewer, so a viewer far from PT can see two headings share a label — the
# grouping stays correct, it just splits their day where PT does.
_GROUPING_TZ = ZoneInfo("America/Los_Angeles")


def _day_key(moment: datetime) -> date:
    return moment.astimezone(_GROUPING_TZ).date()


def _overflow_note(total: int, shown: int) -> list[str]:
    if total <= shown:
        return []
    return [f"_+{total - shown} more_"]


def _alerted_line(lead: _Lead) -> str:
    icon = LEAD_ICON_EARLY if lead.is_early else LEAD_ICON_OFFICIAL
    parts = [f"{icon} {lead.linked_label()}", lead.source_label()]
    attribution = lead.attribution()
    if attribution:
        parts.append(attribution)
    line = " · ".join(parts)
    # The quote is the evidence for an early signal; a catalog listing has
    # nothing to weigh, so it stays a single scannable line.
    if lead.is_early:
        excerpt = lead.excerpt()
        if excerpt:
            line = f"{line}\n> {excerpt}"
    return line


REVIEW_EXCERPT_MAX = 120


def _review_line(lead: _Lead) -> str:
    reason = humanize_reason(lead.reason) if lead.reason else "Needs a human look"
    line = f"{LEAD_ICON_REVIEW} {lead.linked_label()} · {_escape_mrkdwn(reason)}"
    excerpt = lead.excerpt()
    if excerpt:
        line = f"{line}\n> {excerpt}"
    return line


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


def _friendly_iso(value: Any, *, empty: str = "n/a") -> str:
    """Render a stored ISO timestamp per-viewer, falling back to the raw value.

    Status carries timestamps as ISO strings, so an unparseable one is shown
    as-is rather than hidden — a malformed value is worth seeing.
    """
    if not value:
        return empty
    parsed = _parse_iso(str(value))
    if parsed is None:
        return str(value)
    return _slack_date(parsed, "%b %-d, %-I:%M %p PT")


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
        f"*Last run:* {last.get('status') or 'none'} ({_friendly_iso(last.get('finished_at'))})",
        f"*Next run:* {_friendly_iso(status.get('next_run_at'), empty='not scheduled')}",
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
