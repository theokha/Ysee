from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yc_monitor.models import AdapterHealth, Alert, CanonicalItem, alert_from_dict, alert_to_dict

YC_CATALOG_BOOTSTRAP_KEY = "yc_catalog_bootstrap_complete"
SPEEDRUN_CATALOG_BOOTSTRAP_KEY = "speedrun_catalog_bootstrap_complete"
YC_LAUNCHES_BOOTSTRAP_KEY = "yc_launches_bootstrap_complete"
LAST_GPT_STATS_KEY = "last_gpt_stats"
LAST_USAGE_KEY = "last_usage"
SCHEDULER_NEXT_RUN_KEY = "scheduler_next_run"


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS yc_companies (
                    slug TEXT PRIMARY KEY,
                    normalized_name TEXT NOT NULL,
                    name TEXT NOT NULL,
                    website_host TEXT,
                    founder_handles TEXT NOT NULL DEFAULT '[]',
                    name_aliases TEXT NOT NULL DEFAULT '[]',
                    payload TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS seen_items (
                    dedup_key TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    company_name TEXT,
                    canonical_url TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    reason TEXT,
                    payload TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    alerted_at TEXT
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    alert_count INTEGER NOT NULL DEFAULT 0,
                    health TEXT NOT NULL DEFAULT '[]',
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS pond_responses (
                    idempotency_key TEXT PRIMARY KEY,
                    response TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS slack_install (
                    team_id TEXT PRIMARY KEY,
                    bot_token TEXT NOT NULL,
                    installed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS slack_outbox (
                    dedup_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'failed', 'sent')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sent_at TEXT,
                    slack_channel TEXT,
                    slack_ts TEXT
                );
                """
            )
            self._ensure_name_aliases_column(db)
            self._ensure_outbox_slack_columns(db)
            self._repair_legacy_yc_bootstrap_state(db)

    def _ensure_name_aliases_column(self, db: sqlite3.Connection) -> None:
        existing = {str(row[1]) for row in db.execute("PRAGMA table_info(yc_companies)")}
        if "name_aliases" not in existing:
            db.execute("ALTER TABLE yc_companies ADD COLUMN name_aliases TEXT NOT NULL DEFAULT '[]'")

    def _ensure_outbox_slack_columns(self, db: sqlite3.Connection) -> None:
        existing = {str(row[1]) for row in db.execute("PRAGMA table_info(slack_outbox)")}
        if "slack_channel" not in existing:
            db.execute("ALTER TABLE slack_outbox ADD COLUMN slack_channel TEXT")
        if "slack_ts" not in existing:
            db.execute("ALTER TABLE slack_outbox ADD COLUMN slack_ts TEXT")

    def _repair_legacy_yc_bootstrap_state(self, db: sqlite3.Connection) -> None:
        """Remove pre-outbox YC baseline rows created by the original first run.

        Legacy builds inserted every historical company as `pending` before an outbox
        existed. A pending YC row with no matching outbox is therefore baseline state,
        not a deliverable alert. Current alerts always create both rows atomically.
        """
        company_count = int(db.execute("SELECT COUNT(*) FROM yc_companies").fetchone()[0])
        if company_count == 0:
            return
        db.execute(
            """DELETE FROM seen_items
               WHERE source='yc_directory' AND disposition='pending'
                 AND NOT EXISTS (
                   SELECT 1 FROM slack_outbox
                   WHERE slack_outbox.dedup_key=seen_items.dedup_key
                 )"""
        )
        db.execute(
            """INSERT INTO app_state(key, value, updated_at) VALUES (?, '1', ?)
               ON CONFLICT(key) DO NOTHING""",
            (YC_CATALOG_BOOTSTRAP_KEY, _now()),
        )

    def start_run(self, run_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs(run_id, started_at, status) VALUES (?, ?, 'running')",
                (run_id, _now()),
            )

    def finish_run(
        self,
        run_id: str,
        status: str,
        alerts: int,
        health: list[AdapterHealth],
        error: str | None = None,
    ) -> None:
        health_json = json.dumps(
            [
                {
                    "source": item.source.value,
                    "status": item.status.value,
                    "detail": item.detail,
                    "item_count": item.item_count,
                }
                for item in health
            ]
        )
        with self.connect() as db:
            db.execute(
                """UPDATE runs SET finished_at=?, status=?, alert_count=?, health=?, error=?
                   WHERE run_id=?""",
                (_now(), status, alerts, health_json, error, run_id),
            )

    def has_seen_item(self, key: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM seen_items WHERE dedup_key=?", (key,)
            ).fetchone()
        return row is not None

    def find_early_alert_key(self, company_name: str | None) -> str | None:
        from yc_monitor.classify import normalize_company

        key = f"early:{normalize_company(company_name)}"
        if key.endswith(":"):
            return None
        with self.connect() as db:
            row = db.execute(
                """SELECT dedup_key FROM seen_items
                   WHERE dedup_key=? AND disposition IN ('pending', 'alerted', 'evidence')""",
                (key,),
            ).fetchone()
        return str(row["dedup_key"]) if row else None

    def recent_leads(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT dedup_key, source, item_id, company_name, canonical_url, disposition,
                          reason, payload, first_seen_at, alerted_at
                   FROM seen_items
                   WHERE disposition IN ('alerted', 'pending', 'evidence')
                   ORDER BY COALESCE(alerted_at, first_seen_at) DESC
                   LIMIT ?""",
                (max(1, min(limit, 50)),),
            ).fetchall()
        leads: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            snippet = ""
            raw = record.pop("payload", None)
            if isinstance(raw, str) and raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        snippet = str(
                            parsed.get("text")
                            or parsed.get("content")
                            or parsed.get("one_liner")
                            or ""
                        )[:240]
                except json.JSONDecodeError:
                    snippet = ""
            record["snippet"] = snippet
            leads.append(record)
        return leads

    def reserve_item(
        self, key: str, item: CanonicalItem, disposition: str, reason: str | None = None
    ) -> bool:
        payload = json.dumps(item.raw, default=str)
        with self.connect() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO seen_items
                   (dedup_key, source, item_id, company_name, canonical_url, disposition,
                    reason, payload, first_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    key,
                    item.source.value,
                    item.item_id,
                    item.company_name,
                    item.canonical_url,
                    disposition,
                    reason,
                    payload,
                    _now(),
                ),
            )
            return cursor.rowcount == 1

    def mark_alerted(self, key: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE seen_items SET disposition='alerted', alerted_at=? WHERE dedup_key=?",
                (_now(), key),
            )

    def reserve_alert(self, alert: Alert) -> bool:
        item = alert.item
        item_payload = json.dumps(item.raw, default=str)
        outbox_payload = json.dumps(alert_to_dict(alert), default=str)
        now = _now()
        with self.connect() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO seen_items
                   (dedup_key, source, item_id, company_name, canonical_url, disposition,
                    reason, payload, first_seen_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, ?)""",
                (
                    alert.dedup_key,
                    item.source.value,
                    item.item_id,
                    item.company_name,
                    item.canonical_url,
                    item_payload,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                return False
            db.execute(
                """INSERT OR IGNORE INTO slack_outbox
                   (dedup_key, payload, status, attempts, created_at, updated_at)
                   VALUES (?, ?, 'pending', 0, ?, ?)""",
                (alert.dedup_key, outbox_payload, now, now),
            )
            return True

    def list_undelivered_alerts(self) -> list[Alert]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT payload FROM slack_outbox
                   WHERE status IN ('pending', 'failed')
                   ORDER BY created_at ASC"""
            ).fetchall()
        return [alert_from_dict(json.loads(row["payload"])) for row in rows]

    def mark_outbox_sent(
        self, key: str, slack_channel: str | None = None, slack_ts: str | None = None
    ) -> None:
        now = _now()
        with self.connect() as db:
            db.execute(
                """UPDATE slack_outbox
                   SET status='sent', sent_at=?, updated_at=?, last_error=NULL,
                       slack_channel=COALESCE(?, slack_channel),
                       slack_ts=COALESCE(?, slack_ts)
                   WHERE dedup_key=? AND status != 'sent'""",
                (now, now, slack_channel, slack_ts, key),
            )
            db.execute(
                "UPDATE seen_items SET disposition='alerted', alerted_at=? WHERE dedup_key=?",
                (now, key),
            )

    def mark_outbox_failed(self, key: str, error: str) -> None:
        now = _now()
        with self.connect() as db:
            db.execute(
                """UPDATE slack_outbox
                   SET status='failed', attempts=attempts+1, last_error=?, updated_at=?
                   WHERE dedup_key=? AND status != 'sent'""",
                (error, now, key),
            )

    def slack_thread_for_early_key(self, early_key: str | None) -> str | None:
        if not early_key:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT slack_ts FROM slack_outbox WHERE dedup_key=? AND slack_ts IS NOT NULL",
                (early_key,),
            ).fetchone()
        return str(row["slack_ts"]) if row and row["slack_ts"] else None

    def record_source_failure(self, source: str, detail: str) -> int:
        key = f"source_fail:{source}"
        current = int(self.get_state(key) or "0")
        nxt = current + 1
        self.put_state(key, str(nxt))
        self.put_state(f"source_fail_detail:{source}", detail[:240])
        return nxt

    def clear_source_failure(self, source: str) -> int:
        previous = int(self.get_state(f"source_fail:{source}") or "0")
        self.put_state(f"source_fail:{source}", "0")
        return previous

    def consume_daily_budget(self, name: str, limit: int) -> bool:
        if limit <= 0:
            return False
        from datetime import UTC, datetime

        key = f"budget:{name}:{datetime.now(UTC).date().isoformat()}"
        current = int(self.get_state(key) or "0")
        if current >= limit:
            return False
        self.put_state(key, str(current + 1))
        return True

    def outbox_status(self, key: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT status FROM slack_outbox WHERE dedup_key=?", (key,)
            ).fetchone()
        return str(row["status"]) if row else None

    def has_yc_company(self, slug: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM yc_companies WHERE slug=?", (slug,)
            ).fetchone()
        return row is not None

    def upsert_yc_company(
        self,
        slug: str,
        normalized_name: str,
        name: str,
        website_host: str | None,
        founder_handles: list[str],
        payload: dict[str, Any],
        aliases: set[str] | list[str] | None = None,
    ) -> bool:
        alias_values = sorted({value for value in (aliases or set()) if value})
        if normalized_name and normalized_name not in alias_values:
            alias_values.insert(0, normalized_name)
        with self.connect() as db:
            exists = db.execute("SELECT 1 FROM yc_companies WHERE slug=?", (slug,)).fetchone()
            if exists is not None:
                # Skip rewriting unchanged rows so a 6k-company catalog sync stays
                # cheap; only name/host changes are worth an UPDATE.
                row = db.execute(
                    "SELECT name, website_host FROM yc_companies WHERE slug=?", (slug,)
                ).fetchone()
                if (
                    row is not None
                    and row["name"] == name
                    and (row["website_host"] or None) == (website_host or None)
                ):
                    return False
            now = _now()
            db.execute(
                """INSERT INTO yc_companies
                   (slug, normalized_name, name, website_host, founder_handles, name_aliases,
                    payload, first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET
                    normalized_name=excluded.normalized_name, name=excluded.name,
                    website_host=excluded.website_host, founder_handles=excluded.founder_handles,
                    name_aliases=excluded.name_aliases, payload=excluded.payload,
                    last_seen_at=excluded.last_seen_at""",
                (
                    slug,
                    normalized_name,
                    name,
                    website_host,
                    json.dumps(founder_handles),
                    json.dumps(alias_values),
                    json.dumps(payload, default=str),
                    now,
                    now,
                ),
            )
            return exists is None

    def is_yc_catalog_bootstrapped(self) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM app_state WHERE key=?", (YC_CATALOG_BOOTSTRAP_KEY,)
            ).fetchone()
        return bool(row and row["value"] == "1")

    def mark_yc_catalog_bootstrapped(self) -> None:
        self.put_state(YC_CATALOG_BOOTSTRAP_KEY, "1")

    def get_state(self, key: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def put_state(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO app_state(key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, _now()),
            )

    def official_identities(self) -> tuple[set[str], set[str], set[str]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT normalized_name, website_host, founder_handles, name_aliases
                   FROM yc_companies"""
            ).fetchall()
        names: set[str] = set()
        hosts = {row["website_host"] for row in rows if row["website_host"]}
        handles: set[str] = set()
        for row in rows:
            names.add(row["normalized_name"])
            try:
                aliases = json.loads(row["name_aliases"] or "[]")
            except json.JSONDecodeError:
                aliases = []
            if isinstance(aliases, list):
                names.update(str(value) for value in aliases if value)
            handles.update(json.loads(row["founder_handles"] or "[]"))
        return names, hosts, handles

    def status(self) -> dict[str, Any]:
        with self.connect() as db:
            last = db.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
            counts = db.execute(
                "SELECT disposition, COUNT(*) count FROM seen_items GROUP BY disposition"
            ).fetchall()
            companies = db.execute("SELECT COUNT(*) count FROM yc_companies").fetchone()
            outbox = db.execute(
                "SELECT status, COUNT(*) count FROM slack_outbox GROUP BY status"
            ).fetchall()
        gpt_raw = self.get_state(LAST_GPT_STATS_KEY)
        try:
            gpt = json.loads(gpt_raw) if gpt_raw else None
        except json.JSONDecodeError:
            gpt = None
        return {
            "last_run": dict(last) if last else None,
            "seen": {row["disposition"]: row["count"] for row in counts},
            "official_yc_companies": companies["count"] if companies else 0,
            "outbox": {row["status"]: row["count"] for row in outbox},
            "next_run_at": self.get_state(SCHEDULER_NEXT_RUN_KEY),
            "gpt": gpt if isinstance(gpt, dict) else None,
            "usage": json.loads(self.get_state(LAST_USAGE_KEY) or "null"),
        }

    def save_slack_install(self, team_id: str, bot_token: str) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO slack_install(team_id, bot_token, installed_at) VALUES (?, ?, ?)
                   ON CONFLICT(team_id) DO UPDATE SET
                    bot_token=excluded.bot_token, installed_at=excluded.installed_at""",
                (team_id, bot_token, _now()),
            )

    def latest_slack_bot_token(self) -> str | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT bot_token FROM slack_install
                   ORDER BY installed_at DESC, rowid DESC LIMIT 1"""
            ).fetchone()
        return str(row["bot_token"]) if row else None

    def get_pond_response(self, key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT response FROM pond_responses WHERE idempotency_key=?", (key,)
            ).fetchone()
        return json.loads(row["response"]) if row else None

    def save_pond_response(self, key: str, response: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO pond_responses VALUES (?, ?, ?)",
                (key, json.dumps(response), _now()),
            )

    def get_runtime_setting(self, key: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM runtime_settings WHERE key=?", (key,)
            ).fetchone()
        return str(row["value"]) if row else None

    def set_runtime_setting(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO runtime_settings(key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, _now()),
            )

    def reset_runtime_setting(self, key: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM runtime_settings WHERE key=?", (key,))

    def all_runtime_settings(self) -> dict[str, str]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT key, value FROM runtime_settings ORDER BY key"
            ).fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def get_pond_task(self, task_id: str) -> dict[str, Any] | None:
        """Sync agents never return 202, so /tasks/{id} only needs a spec-shaped miss."""
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM pond_responses WHERE idempotency_key=?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        stored = self.get_pond_response(task_id)
        if stored is None:
            return None
        return {
            "task_id": task_id,
            "status": "completed",
            "run_id": stored.get("run_id"),
            "result": stored.get("result"),
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()
