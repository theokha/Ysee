from __future__ import annotations

from yc_monitor.db import Database
from yc_monitor.models import CanonicalItem, Source
from yc_monitor.slack_format import format_leads_blocks


def _lead(
    dedup_key: str,
    source: str,
    item_id: str,
    disposition: str,
    company_name: str | None = None,
    reason: str | None = None,
    alerted_at: str | None = None,
    first_seen_at: str | None = None,
    author_name: str | None = None,
    snippet: str | None = None,
) -> dict[str, object]:
    return {
        "dedup_key": dedup_key,
        "source": source,
        "item_id": item_id,
        "company_name": company_name,
        "disposition": disposition,
        "reason": reason,
        "alerted_at": alerted_at,
        "first_seen_at": first_seen_at or alerted_at,
        "author_name": author_name,
        "snippet": snippet,
    }


def _text(blocks: list[dict[str, object]]) -> str:
    parts: list[str] = []
    for block in blocks:
        text = block.get("text")
        if isinstance(text, dict):
            parts.append(str(text.get("text") or ""))
        elif block.get("type") == "header":
            header = text if isinstance(text, dict) else {}
            parts.append(str(header.get("text") or ""))
    return "\n".join(parts)


def test_empty_leads_render_no_leads_yet() -> None:
    blocks = format_leads_blocks([])
    rendered = _text(blocks)
    assert "No leads yet." in rendered


def test_evidence_rows_fold_into_alerted_company() -> None:
    leads = [
        _lead(
            "twitter:20952",
            "twitter",
            "20952",
            "evidence",
            company_name="Kairo",
            reason="gpt_confirmed:founder self announcement",
            alerted_at="2026-09-01T12:00:00+00:00",
        ),
        _lead(
            "early:kairo",
            "twitter",
            "20952",
            "alerted",
            company_name="Kairo",
            reason="gpt_confirmed:founder self announcement",
            alerted_at="2026-09-01T12:00:00+00:00",
        ),
    ]
    rendered = _text(format_leads_blocks(leads))
    assert rendered.count("Kairo") == 1
    assert "Alerted" in rendered
    assert "X" in rendered


def test_review_section_strips_gpt_prefix() -> None:
    leads = [
        _lead(
            "linkedin:441",
            "linkedin",
            "441",
            "review",
            company_name="Harbor",
            reason="gpt_review:unresolved_company_handle",
            first_seen_at="2026-09-02T08:00:00+00:00",
        ),
    ]
    rendered = _text(format_leads_blocks(leads))
    assert "Review queue" in rendered
    assert "unresolved_company_handle" in rendered
    assert "gpt_review:" not in rendered


def test_review_row_without_company_name_falls_back_to_source() -> None:
    leads = [
        _lead(
            "twitter:777",
            "twitter",
            "777",
            "review",
            reason="gpt_review:contradictory_verdict",
            first_seen_at="2026-09-02T08:00:00+00:00",
        ),
    ]
    rendered = _text(format_leads_blocks(leads))
    assert "X post" in rendered


def test_review_row_renders_author_name_when_no_company() -> None:
    leads = [
        _lead(
            "linkedin:900",
            "linkedin",
            "900",
            "review",
            reason="gpt_review:unresolved_company_handle",
            author_name="Dana Reyes",
            first_seen_at="2026-09-02T08:00:00+00:00",
        ),
    ]
    rendered = _text(format_leads_blocks(leads))
    assert "Dana Reyes" in rendered
    assert "LinkedIn post" not in rendered


def test_company_name_wins_over_author_name() -> None:
    leads = [
        _lead(
            "linkedin:901",
            "linkedin",
            "901",
            "review",
            company_name="Harbor",
            reason="gpt_review:needs_human_check",
            author_name="Dana Reyes",
            first_seen_at="2026-09-02T08:00:00+00:00",
        ),
    ]
    rendered = _text(format_leads_blocks(leads))
    assert "Harbor - needs_human_check" in rendered
    assert "Dana Reyes" not in rendered


def test_review_row_appends_snippet_excerpt() -> None:
    leads = [
        _lead(
            "linkedin:902",
            "linkedin",
            "902",
            "review",
            reason="gpt_review:unresolved_company_handle",
            author_name="Dana Reyes",
            first_seen_at="2026-09-02T08:00:00+00:00",
            snippet="We just opened our YC interview slot and the product ships next week.",
        ),
    ]
    rendered = _text(format_leads_blocks(leads))
    assert "> We just opened our YC interview slot" in rendered
    assert rendered.count("> ") == 1


def test_review_row_without_snippet_has_no_excerpt() -> None:
    leads = [
        _lead(
            "linkedin:903",
            "linkedin",
            "903",
            "review",
            reason="gpt_review:unresolved_company_handle",
            author_name="Dana Reyes",
            first_seen_at="2026-09-02T08:00:00+00:00",
            snippet="",
        ),
    ]
    rendered = _text(format_leads_blocks(leads))
    assert "> " not in rendered


def test_review_snippet_is_truncated_and_single_line() -> None:
    snippet = (
        "line one with a hard break\nline two with\ttabs and   runs of spaces\n"
        + ("tail" * 60)
    )
    leads = [
        _lead(
            "linkedin:904",
            "linkedin",
            "904",
            "review",
            reason="gpt_review:unresolved_company_handle",
            author_name="Dana Reyes",
            first_seen_at="2026-09-02T08:00:00+00:00",
            snippet=snippet,
        ),
    ]
    rendered = _text(format_leads_blocks(leads))
    excerpt_lines = [line for line in rendered.split("\n") if line.startswith("> ")]
    assert len(excerpt_lines) == 1
    excerpt = excerpt_lines[0][2:]
    assert "\n" not in excerpt
    assert len(excerpt) == 120
    assert excerpt.startswith("line one with a hard break line two with tabs and runs of spaces")


def test_recent_leads_extracts_author_name_from_payload(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    review_item = CanonicalItem(
        Source.LINKEDIN,
        "rev-9",
        None,
        "https://linkedin.com/a/9",
        content_text="We are hiring a founding engineer for our seed round.",
        founder_name="Dana Reyes",
        raw={
            "text": "We are hiring a founding engineer for our seed round.",
            "author": {"name": "Dana Reyes", "userName": "danareyes"},
        },
    )
    db.reserve_item(
        "linkedin:rev-9", review_item, "review", "gpt_review:unresolved_company_handle"
    )
    leads = db.recent_leads(10)
    assert len(leads) == 1
    lead = leads[0]
    assert lead["author_name"] == "Dana Reyes"
    assert lead["snippet"].startswith("We are hiring a founding engineer")
    assert "payload" not in lead
    rendered = _text(format_leads_blocks(leads))
    assert "Dana Reyes" in rendered
    assert "> We are hiring a founding engineer" in rendered


def test_date_comes_from_alerted_at() -> None:
    leads = [
        _lead(
            "yc:acme",
            "yc_directory",
            "acme",
            "alerted",
            company_name="Acme",
            alerted_at="2026-09-01T23:30:00+00:00",
            first_seen_at="2026-08-01T00:00:00+00:00",
        ),
    ]
    rendered = _text(format_leads_blocks(leads))
    # 2026-09-01 23:30 UTC is Sep 1 in America/Los_Angeles.
    assert "Sep 1" in rendered
    assert "Aug" not in rendered


def test_duplicate_company_rows_collapse_to_most_recent() -> None:
    leads = [
        _lead(
            "yc:acme",
            "yc_directory",
            "acme",
            "alerted",
            company_name="Acme",
            alerted_at="2026-09-01T00:00:00+00:00",
        ),
        _lead(
            "launch:acme",
            "yc_launches",
            "acme",
            "alerted",
            company_name="Acme",
            alerted_at="2026-09-02T00:00:00+00:00",
        ),
    ]
    rendered = _text(format_leads_blocks(leads))
    assert rendered.count("Acme") == 1


def test_baseline_and_rejected_rows_never_reach_formatter(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    baseline_item = CanonicalItem(Source.YC_DIRECTORY, "oldco", "Oldco", "https://yc.test/oldco")
    rejected_item = CanonicalItem(
        Source.TWITTER, "noise-1", None, "https://x.com/a/1", content_text="noise"
    )
    review_item = CanonicalItem(
        Source.LINKEDIN, "rev-1", "Harbor", "https://linkedin.com/a/1"
    )
    alerted_item = CanonicalItem(
        Source.TWITTER, "20952", "Kairo", "https://x.com/b/2", content_text="got into YC"
    )
    db.reserve_item("yc:oldco", baseline_item, "baseline", "yc_bootstrap")
    db.reserve_item("twitter:noise-1", rejected_item, "rejected", "excluded_intent")
    db.reserve_item(
        "linkedin:rev-1", review_item, "review", "gpt_review:unresolved_company_handle"
    )
    db.reserve_item(
        "twitter:20952", alerted_item, "evidence", "gpt_confirmed:founder_self_announcement"
    )
    db.reserve_item(
        "early:kairo", alerted_item, "alerted", "gpt_confirmed:founder_self_announcement"
    )
    leads = db.recent_leads(25)
    assert [lead["dedup_key"] for lead in leads] == [
        "early:kairo",
        "twitter:20952",
        "linkedin:rev-1",
    ]
    rendered = _text(format_leads_blocks(leads))
    assert "Oldco" not in rendered
    assert "excluded_intent" not in rendered
    assert rendered.count("Kairo") == 1
    assert "unresolved_company_handle" in rendered


def test_sections_cap_at_ten_lines() -> None:
    leads = [
        _lead(
            f"early:company{index}",
            "twitter",
            str(1000 + index),
            "alerted",
            company_name=f"Company{index}",
            alerted_at="2026-09-01T00:00:00+00:00",
        )
        for index in range(15)
    ]
    rendered = _text(format_leads_blocks(leads))
    assert rendered.count("• ") == 10
