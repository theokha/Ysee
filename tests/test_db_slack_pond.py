from unittest.mock import patch

from fastapi.testclient import TestClient

from yc_monitor.classify import official_alert
from yc_monitor.config import Settings
from yc_monitor.db import Database
from yc_monitor.models import CanonicalItem, Source
from yc_monitor.pond_server import create_app
from yc_monitor.slack_app import handle_slash_command
from yc_monitor.slack_format import format_alert


def test_database_deduplicates(tmp_path) -> None:
    db = Database(str(tmp_path / "state.db"))
    item = CanonicalItem(Source.YC_DIRECTORY, "acme", "Acme", "https://yc.test/acme")
    assert db.reserve_item("yc:acme", item, "pending")
    assert not db.reserve_item("yc:acme", item, "pending")


def test_slack_alert_has_required_fields() -> None:
    item = CanonicalItem(
        Source.YC_DIRECTORY, "acme", "Acme", "https://yc.test/acme", description="Logistics AI"
    )
    text, blocks = format_alert(official_alert(item))
    rendered = str(blocks)
    assert "NEW YC COMPANY" in text
    assert "Acme" in rendered
    assert "Source" in rendered
    assert "Logistics AI" in rendered
    assert "https://yc.test/acme" in rendered
    assert "Open directory profile" in rendered


def test_pond_manifest_and_auth(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "pond.db"),
        pond_access_key="pond-secret",
        scheduler_run_immediately=False,
    )
    client = TestClient(create_app(settings))
    manifest = client.get("/manifest")
    assert manifest.status_code == 200
    assert manifest.json()["protocol"] == "marketplace-agent"
    unauthorized = client.post("/runs", json={"run_id": "r1", "action_id": "get_status"})
    assert unauthorized.status_code == 401
    headers = {
        "Authorization": "Bearer pond-secret",
        "X-Agent-Protocol-Version": "1.0",
        "Idempotency-Key": "r1",
    }
    first = client.post("/runs", headers=headers, json={"run_id": "r1", "action_id": "get_status"})
    second = client.post("/runs", headers=headers, json={"run_id": "r1", "action_id": "get_status"})
    assert first.status_code == 200
    assert first.json() == second.json()
    assert "next_run_at" in first.json()["result"]
    assert "gpt" in first.json()["result"]


def test_scheduler_start_failure_does_not_take_down_http(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "pond.db"),
        pond_access_key="pond-secret",
        scheduler_run_immediately=True,
    )
    with patch("yc_monitor.pond_server.schedule_first_run", side_effect=RuntimeError("boom")):
        client = TestClient(create_app(settings))
        health = client.get("/healthz")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["speedrun"] in {"configured_a16z_official_api", "disabled"}


def test_slash_command_returns_status_blocks() -> None:
    payload = handle_slash_command("/yc", "status", {
        "official_yc_companies": 12,
        "seen": {"alerted": 3},
        "outbox": {"sent": 3},
        "last_run": {"status": "completed", "finished_at": "2026-09-02T00:00:00+00:00"},
        "next_run_at": "2026-09-02T08:00:00+00:00",
        "gpt": {"calls": 4, "accepted": 1, "rejected": 3},
    })
    assert payload["response_type"] == "ephemeral"
    rendered = str(payload["blocks"])
    assert "YC Launch Monitor status" in rendered
    assert "12" in rendered
    scan = handle_slash_command("/yc", "scan dry", {})
    assert scan["text"] == "dry_scan_requested"


def test_early_official_upgrade_card() -> None:
    item = CanonicalItem(
        Source.YC_DIRECTORY,
        "gamma",
        "Gamma",
        "https://www.ycombinator.com/companies/gamma",
        description="New batch listing",
        company_url="https://gamma.example",
    )
    alert = official_alert(
        item,
        upgrade_from="early:gamma",
        upgrade_note="Previously an early founder signal (`early:gamma`). Now listed in the official directory.",
    )
    text, blocks = format_alert(alert)
    rendered = str(blocks)
    assert "previously an early founder signal" in text
    assert "early:gamma" in rendered
    assert "Open directory profile" in rendered


def test_pond_list_leads(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "pond.db"),
        pond_access_key="pond-secret",
        scheduler_run_immediately=False,
    )
    db = Database(str(tmp_path / "pond.db"))
    item = CanonicalItem(Source.YC_DIRECTORY, "acme", "Acme", "https://yc.test/acme")
    db.reserve_item("yc:acme", item, "alerted")
    client = TestClient(create_app(settings))
    headers = {
        "Authorization": "Bearer pond-secret",
        "X-Agent-Protocol-Version": "1.0",
        "Idempotency-Key": "leads1",
    }
    response = client.post(
        "/runs",
        headers=headers,
        json={"run_id": "leads1", "action_id": "list_leads", "input": {"limit": 5}},
    )
    assert response.status_code == 200
    leads = response.json()["result"]["leads"]
    assert leads[0]["dedup_key"] == "yc:acme"
