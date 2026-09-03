"""`POST /slack/interactions`: the endpoint Slack button clicks will arrive on.

No buttons are wired yet, so these tests pin the security boundary rather than
any behaviour: an unsigned, replayed, or malformed payload must not reach
handler code, and an action nobody handles must still answer 200 so Slack does
not retry it or show the clicker a failure.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.responses import Response
from fastapi.testclient import TestClient

from yc_monitor.config import Settings
from yc_monitor.pond_server import create_app
from yc_monitor.slack_app import interaction_actions, interaction_payload

SECRET = "interactions-secret"


def _settings(tmp_path, signing_secret: str | None = SECRET) -> Settings:
    return Settings(
        database_path=str(tmp_path / "pond.db"),
        slack_signing_secret=signing_secret,
        scheduler_run_immediately=False,
        slack_bot_token=None,
        slack_channel_id=None,
        slack_ops_channel_id=None,
        openai_api_key=None,
    )


def _body(payload: dict[str, object]) -> bytes:
    return urlencode({"payload": json.dumps(payload)}).encode()


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    basestring = f"v0:{timestamp}:{body.decode()}".encode()
    return "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()


def _post(
    client: TestClient,
    body: bytes,
    *,
    secret: str = SECRET,
    timestamp: str | None = None,
    signature: str | None = None,
) -> Response:
    stamp = timestamp if timestamp is not None else str(int(time.time()))
    return client.post(
        "/slack/interactions",
        content=body,
        headers={
            "X-Slack-Request-Timestamp": stamp,
            "X-Slack-Signature": signature or _sign(secret, stamp, body),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )


def _block_actions(action_id: str = "approve_lead") -> dict[str, object]:
    return {
        "type": "block_actions",
        "user": {"id": "U123", "name": "theo"},
        "channel": {"id": "C123"},
        "response_url": "https://hooks.slack.test/actions/1",
        "actions": [{"action_id": action_id, "type": "button", "value": "early:harbor"}],
    }


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as test_client:
        yield test_client


def test_correctly_signed_interaction_is_accepted(client: TestClient) -> None:
    response = _post(client, _body(_block_actions()))
    assert response.status_code == 200


def test_wrong_signature_is_rejected(client: TestClient) -> None:
    body = _body(_block_actions())
    timestamp = str(int(time.time()))
    forged = _sign("not-the-secret", timestamp, body)
    response = _post(client, body, timestamp=timestamp, signature=forged)
    assert response.status_code == 401


def test_signature_from_a_different_body_is_rejected(client: TestClient) -> None:
    """A captured signature cannot be reused to authorize a swapped payload."""
    timestamp = str(int(time.time()))
    stolen = _sign(SECRET, timestamp, _body(_block_actions("approve_lead")))
    response = _post(
        client,
        _body(_block_actions("delete_everything")),
        timestamp=timestamp,
        signature=stolen,
    )
    assert response.status_code == 401


def test_replayed_old_request_is_rejected(client: TestClient) -> None:
    """Slack's window is 5 minutes; a validly signed but stale request is a replay."""
    body = _body(_block_actions())
    stale = str(int(time.time()) - 60 * 10)
    response = _post(client, body, timestamp=stale, signature=_sign(SECRET, stale, body))
    assert response.status_code == 401


def test_future_dated_request_is_rejected(client: TestClient) -> None:
    body = _body(_block_actions())
    ahead = str(int(time.time()) + 60 * 10)
    response = _post(client, body, timestamp=ahead, signature=_sign(SECRET, ahead, body))
    assert response.status_code == 401


def test_missing_signature_headers_are_rejected(client: TestClient) -> None:
    response = client.post("/slack/interactions", content=_body(_block_actions()))
    assert response.status_code == 401


def test_non_numeric_timestamp_is_rejected(client: TestClient) -> None:
    body = _body(_block_actions())
    response = _post(client, body, timestamp="not-a-number", signature="v0=deadbeef")
    assert response.status_code == 401


def test_endpoint_is_unavailable_when_no_signing_secret_is_configured(tmp_path) -> None:
    """Without a secret nothing can be verified, so refuse rather than trust."""
    with TestClient(create_app(_settings(tmp_path, signing_secret=None))) as client:
        response = _post(client, _body(_block_actions()), secret="anything")
    assert response.status_code == 503


def test_unknown_action_id_is_ignored_not_an_error(client: TestClient) -> None:
    """Slack retries on non-2xx and shows the clicker a warning; a no-op must not."""
    response = _post(client, _body(_block_actions("some_button_we_never_shipped")))
    assert response.status_code == 200
    assert response.json() == {}


def test_malformed_payload_does_not_crash_the_endpoint(client: TestClient) -> None:
    for body in (b"payload=not-json", b"payload=", b"", b"payload=%5B1%2C2%5D"):
        assert _post(client, body).status_code == 200


def test_payload_with_unexpected_field_shapes_still_answers_200(client: TestClient) -> None:
    """Every field is attacker-shaped once the signature is the only gate."""
    odd = {"type": "block_actions", "user": "not-an-object", "actions": [{"action_id": None}]}
    assert _post(client, _body(odd)).status_code == 200


# --- payload parsing --------------------------------------------------------


def test_interaction_payload_unwraps_the_json_form_field() -> None:
    payload = interaction_payload(_body(_block_actions()))
    assert payload["type"] == "block_actions"
    assert payload["user"] == {"id": "U123", "name": "theo"}


def test_interaction_payload_returns_empty_for_unusable_bodies() -> None:
    assert interaction_payload(b"") == {}
    assert interaction_payload(b"payload=") == {}
    assert interaction_payload(b"payload=not-json") == {}
    # Valid JSON that is not an object -- indexing it later would raise.
    assert interaction_payload(b"payload=%5B1%2C2%5D") == {}
    assert interaction_payload(b"\xff\xfe") == {}


def test_interaction_actions_reads_the_action_list() -> None:
    actions = interaction_actions(interaction_payload(_body(_block_actions())))
    assert [action["action_id"] for action in actions] == ["approve_lead"]


def test_interaction_actions_tolerates_a_missing_or_odd_action_list() -> None:
    assert interaction_actions({}) == []
    assert interaction_actions({"actions": None}) == []
    assert interaction_actions({"actions": "approve"}) == []
    # Non-dict entries are dropped rather than poisoning the loop.
    assert interaction_actions({"actions": ["x", {"action_id": "a"}]}) == [{"action_id": "a"}]
