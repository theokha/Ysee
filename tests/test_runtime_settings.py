"""Runtime-adjustable settings: db round trip, override application, and /yc config."""

from __future__ import annotations

import pytest

from yc_monitor.adapters.registry import build_adapters
from yc_monitor.config import Settings
from yc_monitor.db import Database
from yc_monitor.pipeline import MonitorPipeline
from yc_monitor.runtime_settings import (
    SETTING_SPECS,
    apply_runtime_settings,
    effective_value,
    format_config_block,
)
from yc_monitor.slack_app import handle_slash_command

ADMIN = "UADMIN"
ADMINS = {ADMIN}


def make_db(tmp_path) -> Database:
    return Database(str(tmp_path / "runtime.db"))


def make_settings(tmp_path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_path": str(tmp_path / "runtime.db"),
        "openai_api_key": None,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def run_command(
    text: str,
    db: Database | None,
    user_id: str = ADMIN,
    admin_users: set[str] | None = ADMINS,
) -> dict[str, object]:
    return handle_slash_command(
        "/yc",
        text,
        {"seen": {}, "outbox": {}},
        user_id=user_id,
        db=db,
        admin_users=admin_users,
    )


# --- db round trip -----------------------------------------------------------


def test_set_get_reset_round_trip(tmp_path) -> None:
    db = make_db(tmp_path)
    assert db.get_runtime_setting("poll_interval_hours") is None
    assert db.all_runtime_settings() == {}

    db.set_runtime_setting("poll_interval_hours", "12")
    assert db.get_runtime_setting("poll_interval_hours") == "12"
    db.set_runtime_setting("openai_max_calls_per_cycle", "10")
    assert db.all_runtime_settings() == {
        "openai_max_calls_per_cycle": "10",
        "poll_interval_hours": "12",
    }

    # Overwrite, not duplicate.
    db.set_runtime_setting("poll_interval_hours", "4")
    assert db.all_runtime_settings()["poll_interval_hours"] == "4"

    db.reset_runtime_setting("poll_interval_hours")
    assert db.get_runtime_setting("poll_interval_hours") is None
    assert db.all_runtime_settings() == {"openai_max_calls_per_cycle": "10"}
    # Resetting an absent or already-reset key is a no-op, not an error.
    db.reset_runtime_setting("poll_interval_hours")
    db.reset_runtime_setting("never_set")


# --- apply_runtime_settings --------------------------------------------------


def test_apply_runtime_settings_returns_identical_defaults_when_nothing_stored(tmp_path) -> None:
    db = make_db(tmp_path)
    settings = make_settings(tmp_path)
    applied = apply_runtime_settings(db, settings)
    assert applied == settings
    assert applied is settings  # no overrides means the same object, no copy


def test_apply_runtime_settings_overrides_stored_values(tmp_path) -> None:
    db = make_db(tmp_path)
    settings = make_settings(tmp_path)
    assert settings.openai_max_calls_per_cycle == 25
    db.set_runtime_setting("openai_max_calls_per_cycle", "10")
    db.set_runtime_setting("twitter_current_batches", "s26, w27")
    db.set_runtime_setting("openai_min_confidence", "0.5")

    applied = apply_runtime_settings(db, settings)
    assert applied is not settings
    assert applied.openai_max_calls_per_cycle == 10
    assert applied.twitter_current_batches == "S26,W27"
    assert applied.openai_min_confidence == 0.5
    # The base settings object is untouched.
    assert settings.openai_max_calls_per_cycle == 25


def test_apply_runtime_settings_reverts_after_reset(tmp_path) -> None:
    db = make_db(tmp_path)
    base = make_settings(tmp_path)
    db.set_runtime_setting("openai_max_calls_per_cycle", "10")
    assert apply_runtime_settings(db, base).openai_max_calls_per_cycle == 10

    db.reset_runtime_setting("openai_max_calls_per_cycle")
    reverted = apply_runtime_settings(db, base)
    assert reverted == base
    assert reverted.openai_max_calls_per_cycle == 25


def test_apply_runtime_settings_ignores_empty_string_stored_value(tmp_path) -> None:
    db = make_db(tmp_path)
    base = make_settings(tmp_path)
    db.set_runtime_setting("poll_interval_hours", "")
    assert apply_runtime_settings(db, base).poll_interval_hours == base.poll_interval_hours


def test_every_spec_env_attr_exists_on_settings(tmp_path) -> None:
    settings = make_settings(tmp_path)
    for spec in SETTING_SPECS.values():
        assert hasattr(settings, spec.env_attr), spec.key
        assert spec.key == spec.env_attr


def test_effective_value_prefers_stored_override(tmp_path) -> None:
    db = make_db(tmp_path)
    settings = make_settings(tmp_path)
    spec = SETTING_SPECS["openai_max_calls_per_cycle"]
    assert effective_value(db, spec, settings) == 25
    db.set_runtime_setting(spec.key, "7")
    assert effective_value(db, spec, settings) == 7
    db.reset_runtime_setting(spec.key)
    assert effective_value(db, spec, settings) == 25


# --- format_config_block -----------------------------------------------------


def test_format_config_block_marks_overridden_key(tmp_path) -> None:
    db = make_db(tmp_path)
    settings = make_settings(tmp_path)
    db.set_runtime_setting("openai_max_calls_per_cycle", "10")

    rendered = format_config_block(db, settings)
    # The override marker is glued to the bold label: *Label** for overridden keys.
    assert "*GPT calls per cycle**: `10`" in rendered
    assert "`openai_max_calls_per_cycle`" in rendered
    assert "runtime override active" in rendered
    # An unset sibling keeps a bare label with its .env default.
    assert "*Scan interval*: `8`" in rendered


def test_format_config_block_without_overrides_has_no_marker(tmp_path) -> None:
    db = make_db(tmp_path)
    settings = make_settings(tmp_path)
    rendered = format_config_block(db, settings)
    assert "*GPT calls per cycle*: `25`" in rendered
    assert "runtime override active" in rendered  # legend always shown


# --- coercer validation ------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("twitter_current_batches", "F26,XYZ"),
        ("twitter_current_batches", "F2"),
        ("twitter_current_batches", ""),
        ("openai_max_calls_per_cycle", "201"),
        ("openai_max_calls_per_cycle", "-1"),
        ("openai_max_calls_per_cycle", "notanint"),
        ("openai_max_calls_per_day", "2001"),
        ("poll_interval_hours", "0"),
        ("poll_interval_hours", "49"),
        ("twitter_lookback_days", "31"),
        ("linkedin_total_posts", "0"),
        ("yc_official_alert_max_age_days", "31"),
        ("openai_min_confidence", "1.5"),
        ("openai_immediate_min_confidence", "-0.1"),
        ("openai_immediate_min_confidence", "abc"),
    ],
)
def test_invalid_values_raise_value_error(key: str, bad_value: str) -> None:
    with pytest.raises(ValueError):
        SETTING_SPECS[key].coerce(bad_value)


@pytest.mark.parametrize(
    ("key", "good_value", "expected"),
    [
        ("twitter_current_batches", "f26, w27", "F26,W27"),
        ("openai_max_calls_per_cycle", "0", 0),
        ("openai_max_calls_per_cycle", "10", 10),
        ("openai_min_confidence", "0.8", 0.8),
        ("poll_interval_hours", "48", 48),
        ("twitter_lookback_days", "30", 30),
        ("yc_official_alert_max_age_days", "1", 1),
        ("linkedin_total_posts", "100", 100),
    ],
)
def test_valid_values_coerce(key: str, good_value: str, expected: object) -> None:
    assert SETTING_SPECS[key].coerce(good_value) == expected


# --- /yc config subcommand ---------------------------------------------------


def test_config_command_renders_blocks(tmp_path) -> None:
    db = make_db(tmp_path)
    response = run_command("config", db, admin_users=ADMINS)
    assert response["response_type"] == "ephemeral"
    blocks = response["blocks"]
    assert isinstance(blocks, list) and blocks
    rendered = str(blocks)
    assert "Adjustable settings" == response["text"]
    assert "openai_max_calls_per_cycle" in rendered
    assert "GPT calls per cycle" in rendered


def test_config_without_db_reports_unavailable(tmp_path) -> None:
    response = run_command("config", None, admin_users=ADMINS)
    assert response["text"] == "Config store unavailable."


def test_config_set_stores_and_confirms(tmp_path) -> None:
    db = make_db(tmp_path)
    response = run_command("config set openai_max_calls_per_cycle 10", db, admin_users=ADMINS)
    assert response["response_type"] == "ephemeral"
    assert response["text"] == (
        "Set openai_max_calls_per_cycle = 10. Applies at the next cycle."
    )
    assert db.get_runtime_setting("openai_max_calls_per_cycle") == "10"


def test_config_set_normalizes_before_storing(tmp_path) -> None:
    db = make_db(tmp_path)
    run_command("config set twitter_current_batches f26, w27", db, admin_users=ADMINS)
    assert db.get_runtime_setting("twitter_current_batches") == "F26,W27"


def test_config_set_invalid_value_returns_error_and_stores_nothing(tmp_path) -> None:
    db = make_db(tmp_path)
    response = run_command("config set openai_max_calls_per_cycle 999", db, admin_users=ADMINS)
    assert response["text"].startswith("Invalid value for openai_max_calls_per_cycle:")
    assert "between 0 and 200" in response["text"]
    assert db.get_runtime_setting("openai_max_calls_per_cycle") is None

    bad_batch = run_command("config set twitter_current_batches NOPE", db, admin_users=ADMINS)
    assert bad_batch["text"].startswith("Invalid value for twitter_current_batches:")
    assert db.get_runtime_setting("twitter_current_batches") is None


def test_config_set_bad_float_returns_error(tmp_path) -> None:
    db = make_db(tmp_path)
    response = run_command("config set openai_min_confidence 2.5", db, admin_users=ADMINS)
    assert response["text"].startswith("Invalid value for openai_min_confidence:")
    assert "between 0.0 and 1.0" in response["text"]
    assert db.get_runtime_setting("openai_min_confidence") is None


def test_config_set_unknown_key_lists_keys(tmp_path) -> None:
    db = make_db(tmp_path)
    response = run_command("config set not_a_real_key 5", db, admin_users=ADMINS)
    assert response["text"].startswith("Unknown key.")
    assert "openai_max_calls_per_cycle" in response["text"]


def test_config_set_missing_value_shows_usage(tmp_path) -> None:
    db = make_db(tmp_path)
    response = run_command("config set openai_max_calls_per_cycle", db, admin_users=ADMINS)
    assert response["text"] == "Usage: `/yc config set openai_max_calls_per_cycle <value>`."


def test_config_reset_clears_stored_value(tmp_path) -> None:
    db = make_db(tmp_path)
    run_command("config set openai_max_calls_per_cycle 10", db, admin_users=ADMINS)
    assert db.get_runtime_setting("openai_max_calls_per_cycle") == "10"

    response = run_command("config reset openai_max_calls_per_cycle", db, admin_users=ADMINS)
    assert response["text"] == (
        "Reset openai_max_calls_per_cycle to the .env default. Applies at the next cycle."
    )
    assert db.get_runtime_setting("openai_max_calls_per_cycle") is None
    assert "openai_max_calls_per_cycle" not in db.all_runtime_settings()


def test_config_reset_unknown_key(tmp_path) -> None:
    db = make_db(tmp_path)
    response = run_command("config reset not_a_real_key", db, admin_users=ADMINS)
    assert response["text"].startswith("Unknown key.")


# --- admin gating ------------------------------------------------------------


def test_config_set_rejected_for_non_admin_when_admins_configured(tmp_path) -> None:
    db = make_db(tmp_path)
    response = run_command("config set openai_max_calls_per_cycle 10", db, user_id="USTRANGER")
    assert response["text"] == "Only admins can change settings."
    assert db.all_runtime_settings() == {}


def test_config_reset_rejected_for_non_admin(tmp_path) -> None:
    db = make_db(tmp_path)
    response = run_command("config reset openai_max_calls_per_cycle", db, user_id="USTRANGER")
    assert response["text"] == "Only admins can change settings."


def test_admin_user_passes_config_set(tmp_path) -> None:
    db = make_db(tmp_path)
    response = run_command("config set openai_max_calls_per_cycle 10", db, user_id=ADMIN)
    assert response["text"].startswith("Set openai_max_calls_per_cycle = 10")
    assert db.get_runtime_setting("openai_max_calls_per_cycle") == "10"


def test_config_view_is_not_admin_gated(tmp_path) -> None:
    db = make_db(tmp_path)
    response = run_command("config", db, user_id="USTRANGER")
    assert "blocks" in response


def test_scan_blocked_for_non_admin_when_admins_configured() -> None:
    response = run_command("scan", None, user_id="USTRANGER")
    assert response["text"] == "Only admins can trigger scans (they spend API budget)."


def test_scan_allowed_for_admin() -> None:
    response = run_command("scan", None, user_id=ADMIN)
    assert response["text"] == "Live scan started. Results will arrive here in a few minutes."


def test_scan_allowed_when_no_admin_list_configured() -> None:
    response = run_command("scan", None, user_id="UANYONE", admin_users=None)
    assert response["text"] == "Live scan started. Results will arrive here in a few minutes."


def test_config_set_open_when_no_admin_list_configured(tmp_path) -> None:
    db = make_db(tmp_path)
    response = run_command(
        "config set openai_max_calls_per_cycle 3", db, user_id="UANYONE", admin_users=None
    )
    assert response["text"].startswith("Set openai_max_calls_per_cycle = 3")
    assert db.get_runtime_setting("openai_max_calls_per_cycle") == "3"


def test_scan_dry_allowed_for_admin_and_blocked_for_stranger() -> None:
    assert run_command("scan dry", None, user_id=ADMIN)["text"] == (
        "Dry scan started. Results will arrive here shortly."
    )
    assert run_command("scan dry", None, user_id="USTRANGER")["text"] == (
        "Only admins can trigger scans (they spend API budget)."
    )


def test_help_mentions_config_commands() -> None:
    response = run_command("help", None, admin_users=ADMINS)
    assert "`/yc config`" in response["text"]
    assert "`/yc config set <key> <value>`" in response["text"]


# --- runtime overrides reach the next cycle ----------------------------------


@pytest.fixture
def no_network(monkeypatch) -> list[Settings]:
    """Keep cycles offline while recording the Settings each rebuild used."""
    seen: list[Settings] = []

    def fake_build(settings: Settings) -> tuple[list[object], list[object]]:
        seen.append(settings)
        return [], []

    monkeypatch.setattr("yc_monitor.pipeline.build_adapters", fake_build)
    return seen


@pytest.mark.asyncio
async def test_override_applies_and_reverts_across_cycles(tmp_path, no_network) -> None:
    """`/yc config set` must take effect next cycle and `reset` must revert."""
    base = make_settings(tmp_path)
    pipeline = MonitorPipeline(base)
    pipeline.official_adapters = []
    pipeline.social_adapters = []

    first = await pipeline.run(dry_run=True)
    assert first["gpt"]["max_calls"] == 25

    db = pipeline.db
    run_command("config set openai_max_calls_per_cycle 10", db)
    second = await pipeline.run(dry_run=True)
    assert second["gpt"]["max_calls"] == 10
    assert pipeline.settings.openai_max_calls_per_cycle == 10
    # Base settings stay pristine so a later reset can fall back to them.
    assert base.openai_max_calls_per_cycle == 25

    run_command("config reset openai_max_calls_per_cycle", db)
    third = await pipeline.run(dry_run=True)
    assert third["gpt"]["max_calls"] == 25
    assert pipeline.settings.openai_max_calls_per_cycle == 25
    # The override cycle rebuilds with 10 and the reset cycle rebuilds back to
    # the .env default; the steady-state first cycle does not rebuild at all.
    rebuilt = [s.openai_max_calls_per_cycle for s in no_network[1:]]
    assert rebuilt == [10, 25]


@pytest.mark.asyncio
async def test_override_reaches_live_gpt_classifier(tmp_path, no_network) -> None:
    """Adapters are rebuilt each cycle; the classifier limits must follow."""
    base = make_settings(tmp_path, openai_api_key=None)
    pipeline = MonitorPipeline(base)
    pipeline.official_adapters = []
    pipeline.social_adapters = []
    classifier = pipeline.social_classifier
    assert classifier.max_calls_per_cycle == 25

    run_command("config set openai_max_calls_per_cycle 4", pipeline.db)
    run_command("config set openai_min_confidence 0.4", pipeline.db)
    await pipeline.run(dry_run=True)
    assert classifier is pipeline.social_classifier  # refreshed, not replaced
    assert classifier.max_calls_per_cycle == 4
    assert classifier.min_confidence == 0.4


@pytest.mark.asyncio
async def test_cycle_rebuilds_adapters_with_overridden_batches(tmp_path, no_network) -> None:
    base = make_settings(tmp_path)
    pipeline = MonitorPipeline(base)
    pipeline.official_adapters = []
    pipeline.social_adapters = []
    run_command("config set twitter_current_batches S26", pipeline.db)
    await pipeline.run(dry_run=True)
    assert [s.twitter_current_batches for s in no_network[1:]] == ["S26"]
    # The same rebuild path is what a real cycle uses, so verify against it too.
    assert build_adapters(no_network[-1])[1][0].current_batches == ("S26",)
