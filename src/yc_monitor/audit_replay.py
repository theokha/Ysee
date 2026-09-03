"""Offline replay of a captured run's classify decisions under current settings.

Reads audit_decisions rows recorded by a live or audit-only run and re-classifies
each candidate without collecting, mutating seen_items/watermarks/outbox, or
calling Slack. The output is an old-vs-new transition report so threshold or
prompt changes can be evaluated against real scan data before enabling them.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from yc_monitor.db import Database
from yc_monitor.gpt_classify import SocialJudge
from yc_monitor.pipeline import _item_from_review_row


@dataclass(slots=True)
class ReplayReport:
    run_id: str
    replayed: int
    transitions: Counter[str]
    per_reason: Counter[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "replayed": self.replayed,
            "transitions": dict(self.transitions),
            "per_reason": dict(self.per_reason),
        }


def _row_to_item(row: dict[str, Any]) -> Any:
    """Adapt an audit_decisions row to the review-row shape _item_from_review_row reads."""
    adapted = {
        "dedup_key": row["dedup_key"],
        "source": row["source"],
        "item_id": row["item_id"],
        "company_name": None,
        "canonical_url": "",
        "reason": row.get("reason"),
        "payload": row.get("payload"),
    }
    return _item_from_review_row(adapted)


async def replay_run(
    db: Database,
    run_id: str,
    classifier: SocialJudge,
    names: set[str],
    hosts: set[str],
    handles: set[str],
) -> ReplayReport:
    """Replay every classify-stage decision captured for `run_id`.

    Only the deterministic path is exercised here: pass a classifier configured
    with the settings under evaluation. Dry-run safety is structural — this
    function never writes.
    """
    rows = [
        row
        for row in db.audit_decisions_for_run(run_id)
        if row["stage"] == "classify"
    ]
    transitions: Counter[str] = Counter()
    per_reason: Counter[str] = Counter()
    for row in rows:
        item = _row_to_item(row)
        if item is None:
            transitions[f'{row["outcome"]}->unparseable'] += 1
            continue
        classification = await classifier.classify(item, names, hosts, handles)
        new_outcome = (
            "alert"
            if classification.alert
            else "review"
            if classification.reason.startswith("gpt_review:")
            else "rejected"
            if classification.persist
            else "deferred"
        )
        transitions[f'{row["outcome"]}->{new_outcome}'] += 1
        per_reason[classification.reason.split(":", 1)[0]] += 1
    return ReplayReport(run_id, len(rows), transitions, per_reason)


def summarize_run(db: Database, run_id: str) -> dict[str, Any]:
    """Aggregate outcome/reason counts for a captured run without replaying."""
    rows = db.audit_decisions_for_run(run_id)
    outcomes: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for row in rows:
        outcomes[str(row["outcome"])] += 1
        reason = str(row.get("reason") or "")
        reasons[reason.split(":", 1)[0]] += 1
    return {
        "run_id": run_id,
        "decisions": len(rows),
        "outcomes": dict(outcomes),
        "reason_prefixes": dict(reasons),
        "alerts": [
            str(row["dedup_key"])
            for row in rows
            if row["stage"] == "classify" and row["outcome"] == "alert"
        ],
    }
