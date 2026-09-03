"""Replay and summarize captured run decisions offline."""

from __future__ import annotations

import json

import pytest

from yc_monitor.audit_replay import replay_run, summarize_run
from yc_monitor.db import Database
from yc_monitor.gpt_classify import GPTSocialClassifier


def _seed_run(db: Database, run_id: str = "run_x") -> None:
    db.record_audit_decision(
        run_id, "twitter", "t1", "twitter:t1", "classify", "deferred",
        "gpt_cycle_budget_exhausted", 0.0, persist=False,
        payload={"id": "t1", "text": "We got into YC S26",
                 "author": {"name": "A", "userName": "a"}},
    )
    db.record_audit_decision(
        run_id, "twitter", "t2", "twitter:t2", "classify", "rejected",
        "gpt_rejected:noise", 0.2, persist=True,
        payload={"id": "t2", "text": "Almanac YC S26 launches an AI agent",
                 "author": {"name": "B", "userName": "b"}},
    )


def test_summarize_run_aggregates_outcomes(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    _seed_run(db)
    summary = summarize_run(db, "run_x")
    assert summary["decisions"] == 2
    assert summary["outcomes"] == {"deferred": 1, "rejected": 1}
    assert summary["reason_prefixes"]["gpt_cycle_budget_exhausted"] == 1
    assert summary["reason_prefixes"]["gpt_rejected"] == 1


@pytest.mark.asyncio
async def test_replay_run_reports_transitions_without_writes(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    _seed_run(db)
    classifier = GPTSocialClassifier(None, "test-model", 5.0, 0, 1, 0.5, 10, 0.9)
    report = await replay_run(db, "run_x", classifier, set(), set(), set())
    assert report.replayed == 2
    assert set(report.transitions) <= {
        "deferred->alert", "deferred->rejected", "deferred->review", "deferred->deferred",
        "rejected->alert", "rejected->rejected", "rejected->review", "rejected->deferred",
    }
    # Replay is read-only: no seen_items rows were created.
    with db.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM seen_items").fetchone()[0]
    assert int(count) == 0


def test_replay_row_payload_round_trips(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    _seed_run(db)
    rows = db.audit_decisions_for_run("run_x")
    parsed = json.loads(str(rows[0]["payload"]))
    assert parsed["text"] == "We got into YC S26"
