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
    canonical_url: str | None = None,
    author_handle: str | None = None,
) -> dict[str, object]:
    return {
        "dedup_key": dedup_key,
        "source": source,
        "item_id": item_id,
        "company_name": company_name,
        "canonical_url": canonical_url,
        "disposition": disposition,
        "reason": reason,
        "alerted_at": alerted_at,
        "first_seen_at": first_seen_at or alerted_at,
        "author_name": author_name,
        "author_handle": author_handle,
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
    assert "Could not verify the company handle" in rendered
    assert "gpt_review:" not in rendered
    assert "unresolved_company_handle" not in rendered


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
    # An unmapped slug still gets unpacked rather than shown raw.
    assert "*Harbor* · Needs human check" in rendered
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
    assert "Could not verify the company handle" in rendered


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
    assert len([line for line in rendered.split("\n") if line.startswith("🚨")]) == 10
    # The count reflects everything found, and the gap is stated rather than
    # left for the reader to wrongly conclude there were only ten.
    assert "*Alerted (15)*" in rendered
    assert "+5 more" in rendered


def test_alerted_row_links_to_the_source() -> None:
    """A name with no link is a dead end -- the reader has to go hunt it down."""
    leads = [
        _lead(
            "early:devika",
            "twitter",
            "20952",
            "alerted",
            company_name="Devika",
            canonical_url="https://x.com/dev_ana/status/20952",
            alerted_at="2026-09-03T12:00:00+00:00",
        ),
    ]
    rendered = _text(format_leads_blocks(leads))
    assert "<https://x.com/dev_ana/status/20952|*Devika*>" in rendered


def test_row_without_a_url_still_renders_the_name() -> None:
    leads = [
        _lead(
            "yc:acme",
            "yc_directory",
            "acme",
            "alerted",
            company_name="Acme",
            alerted_at="2026-09-03T12:00:00+00:00",
        ),
    ]
    rendered = _text(format_leads_blocks(leads))
    assert "*Acme*" in rendered
    assert "<|" not in rendered


def test_early_signals_are_visually_distinct_from_catalog_rows() -> None:
    """The rare founder signal is the product; a catalog listing is routine."""
    leads = [
        _lead(
            "early:devika",
            "twitter",
            "1",
            "alerted",
            company_name="Devika",
            alerted_at="2026-09-03T12:00:00+00:00",
        ),
        _lead(
            "speedrun:zoah",
            "yc_speedrun",
            "zoah",
            "alerted",
            company_name="Zoah",
            alerted_at="2026-09-03T11:00:00+00:00",
        ),
    ]
    rendered = _text(format_leads_blocks(leads))
    devika = next(line for line in rendered.split("\n") if "Devika" in line)
    zoah = next(line for line in rendered.split("\n") if "Zoah" in line)
    assert devika[0] != zoah[0]


def test_social_row_attributes_the_announcer() -> None:
    leads = [
        _lead(
            "early:devika",
            "twitter",
            "1",
            "alerted",
            company_name="Devika",
            author_handle="dev_ana",
            alerted_at="2026-09-03T12:00:00+00:00",
        ),
    ]
    assert "@dev_ana" in _text(format_leads_blocks(leads))


def test_catalog_row_has_no_author_attribution() -> None:
    """A directory listing has no announcer, so inventing one would mislead."""
    leads = [
        _lead(
            "yc:acme",
            "yc_directory",
            "acme",
            "alerted",
            company_name="Acme",
            author_name="Some Scraper",
            author_handle="scraper",
            alerted_at="2026-09-03T12:00:00+00:00",
        ),
    ]
    rendered = _text(format_leads_blocks(leads))
    assert "scraper" not in rendered
    assert "Some Scraper" not in rendered


def test_date_appears_once_per_day_not_once_per_row() -> None:
    # Distinct times within one PT day: grouping on the rendered timestamp
    # rather than the calendar day gives every row its own heading.
    leads = [
        _lead(
            f"speedrun:co{index}",
            "yc_speedrun",
            f"co{index}",
            "alerted",
            company_name=f"Co{index}",
            alerted_at=f"2026-09-03T1{index}:00:00+00:00",
        )
        for index in range(3)
    ]
    rendered = _text(format_leads_blocks(leads))
    assert rendered.count("<!date^") == 1
    assert len([line for line in rendered.split("\n") if line.startswith("📋")]) == 3


def test_separate_days_each_get_a_heading() -> None:
    leads = [
        _lead(
            "speedrun:today",
            "yc_speedrun",
            "a",
            "alerted",
            company_name="Today Co",
            alerted_at="2026-09-03T12:00:00+00:00",
        ),
        _lead(
            "speedrun:yesterday",
            "yc_speedrun",
            "b",
            "alerted",
            company_name="Yesterday Co",
            alerted_at="2026-09-02T12:00:00+00:00",
        ),
    ]
    rendered = _text(format_leads_blocks(leads))
    assert rendered.count("<!date^") == 2


def test_early_alerted_row_shows_the_quoted_post() -> None:
    """The quote is the evidence behind a judged signal, so it travels with it."""
    leads = [
        _lead(
            "early:devika",
            "twitter",
            "1",
            "alerted",
            company_name="Devika",
            snippet="We got into YC F26 building Devika, an agent for legal ops.",
            alerted_at="2026-09-03T12:00:00+00:00",
        ),
    ]
    rendered = _text(format_leads_blocks(leads))
    assert "> We got into YC F26 building Devika" in rendered


def test_catalog_alerted_row_shows_no_excerpt() -> None:
    """Nothing to weigh on a confirmed listing -- an excerpt is just noise."""
    leads = [
        _lead(
            "yc:acme",
            "yc_directory",
            "acme",
            "alerted",
            company_name="Acme",
            snippet="Logistics AI for freight brokers.",
            alerted_at="2026-09-03T12:00:00+00:00",
        ),
    ]
    assert "> " not in _text(format_leads_blocks(leads))


def test_company_name_cannot_break_out_of_its_link() -> None:
    """Slack decodes < and > in mrkdwn, so a raw name could forge a link."""
    leads = [
        _lead(
            "early:evil",
            "twitter",
            "1",
            "alerted",
            company_name="<https://evil.test|Click me>",
            canonical_url="https://x.com/a/1",
            alerted_at="2026-09-03T12:00:00+00:00",
        ),
    ]
    rendered = _text(format_leads_blocks(leads))
    line = next(line for line in rendered.split("\n") if "evil.test" in line)
    # The delimiters are neutralized, so the name cannot open a second link --
    # it renders as inert text inside the real one.
    assert "&lt;https://evil.test" in line
    assert line.count("<") == 1
    assert line.startswith("🚨 <https://x.com/a/1|")


def test_leads_query_supplies_every_field_the_formatter_reads(tmp_path) -> None:
    """recent_leads and the formatter share a contract that nothing else pins."""
    db = Database(str(tmp_path / "state.db"))
    item = CanonicalItem(
        Source.TWITTER,
        "20952",
        "Devika",
        "https://x.com/dev_ana/status/20952",
        content_text="We got into YC F26 building Devika",
        founder_handle="dev_ana",
        raw={
            "text": "We got into YC F26 building Devika",
            "author": {"name": "Ana Dev", "userName": "dev_ana"},
        },
    )
    db.reserve_item("early:devika", item, "alerted", "gpt_confirmed:founder self announcement")
    lead = db.recent_leads(10)[0]
    assert lead["canonical_url"] == "https://x.com/dev_ana/status/20952"
    assert lead["author_handle"] == "dev_ana"
    rendered = _text(format_leads_blocks(db.recent_leads(10)))
    assert "<https://x.com/dev_ana/status/20952|*Devika*>" in rendered
    assert "@dev_ana" in rendered


def test_utc_midnight_rows_group_by_local_day() -> None:
    """UTC evening and the next UTC morning are the same PT day.

    Grouping in UTC would split a single afternoon into two headings.
    """
    leads = [
        _lead(
            "speedrun:late",
            "yc_speedrun",
            "a",
            "alerted",
            company_name="Late Co",
            alerted_at="2026-09-04T05:00:00+00:00",  # Sep 3, 10pm PT
        ),
        _lead(
            "speedrun:early",
            "yc_speedrun",
            "b",
            "alerted",
            company_name="Early Co",
            alerted_at="2026-09-03T20:00:00+00:00",  # Sep 3, 1pm PT
        ),
    ]
    rendered = _text(format_leads_blocks(leads))
    assert rendered.count("<!date^") == 1
