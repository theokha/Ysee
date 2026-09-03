"""Runtime-adjustable settings backed by SQLite.

These override the .env defaults for knobs an operator may want to change
from Slack without a redeploy. Validation is strict: an invalid value is
rejected before it is ever stored, so a bad /yc config cannot brick a cycle.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from yc_monitor.config import Settings
from yc_monitor.db import Database


# key -> (label, validator/coercer, description, env default attribute)
@dataclass(frozen=True)
class SettingSpec:
    key: str
    label: str
    coerce: Callable[[str], Any]
    description: str
    env_attr: str


def _to_int(low: int, high: int) -> Callable[[str], int]:
    def coerce(value: str) -> int:
        parsed = int(value)
        if not low <= parsed <= high:
            raise ValueError(f"must be between {low} and {high}")
        return parsed
    return coerce


def _to_float(low: float, high: float) -> Callable[[str], float]:
    def coerce(value: str) -> float:
        parsed = float(value)
        if not low <= parsed <= high:
            raise ValueError(f"must be between {low} and {high}")
        return parsed
    return coerce


def _to_hours(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 48:
        raise ValueError("must be between 1 and 48 hours")
    return parsed


def _to_lookback(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 30:
        raise ValueError("must be between 1 and 30 days")
    return parsed


def _to_batches(value: str) -> str:
    codes = [code.strip().upper() for code in value.split(",") if code.strip()]
    pattern = re.compile(r"^[SWF]\d{2}$")
    invalid = [code for code in codes if not pattern.fullmatch(code)]
    if invalid:
        raise ValueError(f"invalid batch code(s): {', '.join(invalid)} (use S26/W27/F26 form)")
    if not codes:
        raise ValueError("at least one batch code required")
    if len(codes) > 6:
        raise ValueError("at most 6 batch codes")
    return ",".join(codes)


SETTING_SPECS: dict[str, SettingSpec] = {
    spec.key: spec
    for spec in (
        SettingSpec(
            "poll_interval_hours",
            "Scan interval",
            _to_hours,
            "Hours between automatic cycles (1-48).",
            "poll_interval_hours",
        ),
        SettingSpec(
            "openai_min_confidence",
            "Min confidence",
            _to_float(0.0, 1.0),
            "Lowest GPT confidence considered at all (0.0-1.0).",
            "openai_min_confidence",
        ),
        SettingSpec(
            "openai_immediate_min_confidence",
            "Immediate alert threshold",
            _to_float(0.0, 1.0),
            "Confidence needed to alert immediately (0.0-1.0); lower goes to review.",
            "openai_immediate_min_confidence",
        ),
        SettingSpec(
            "openai_max_calls_per_cycle",
            "GPT calls per cycle",
            _to_int(0, 200),
            "Hard cap on GPT classifications per cycle (0-200).",
            "openai_max_calls_per_cycle",
        ),
        SettingSpec(
            "openai_max_calls_per_day",
            "GPT calls per day",
            _to_int(0, 2000),
            "Daily GPT call budget across all cycles (0-2000).",
            "openai_max_calls_per_day",
        ),
        SettingSpec(
            "twitter_lookback_days",
            "X lookback",
            _to_lookback,
            "Days of X history to search per cycle (1-30).",
            "twitter_lookback_days",
        ),
        SettingSpec(
            "twitter_current_batches",
            "Current batch codes",
            _to_batches,
            "Comma-separated YC batch codes driving X queries and the LinkedIn "
            "batch-tag search, e.g. F26,W27,S27.",
            "twitter_current_batches",
        ),
        SettingSpec(
            "linkedin_total_posts",
            "LinkedIn posts per cycle",
            _to_int(1, 100),
            "Cycle-wide LinkedIn post budget (1-100).",
            "linkedin_total_posts",
        ),
        SettingSpec(
            "yc_official_alert_max_age_days",
            "Official YC alert window",
            _to_int(1, 30),
            "Max age of launched_at for official YC alerts (1-30 days).",
            "yc_official_alert_max_age_days",
        ),
    )
}


def effective_value(db: Database, spec: SettingSpec, settings: Settings) -> Any:
    stored = db.get_runtime_setting(spec.key)
    if stored is None or stored == "":
        return getattr(settings, spec.env_attr)
    return spec.coerce(stored)


def format_config_block(db: Database, settings: Settings) -> str:
    lines = []
    for spec in SETTING_SPECS.values():
        stored = db.get_runtime_setting(spec.key)
        default = getattr(settings, spec.env_attr)
        if stored is None or stored == "":
            value = default
            marker = ""
        else:
            value = spec.coerce(stored)
            marker = "*"
        lines.append(f"*{spec.label}*{marker}: `{value}` (key: `{spec.key}`)")
        lines.append(f"  _{spec.description}_")
    lines.append("")
    lines.append("_* = runtime override active. `/yc config set <key> <value>` to change, `/yc config reset <key>` to clear._")
    return "\n".join(lines)


def apply_runtime_settings(db: Database, settings: Settings) -> Settings:
    """Return a Settings copy with runtime overrides applied.

    The copy is always derived from `settings` and every stored override is
    re-read each call, so a `/yc config reset` reverts on the next cycle rather
    than leaving the previous override baked in.
    """
    changes: dict[str, Any] = {}
    for spec in SETTING_SPECS.values():
        stored = db.get_runtime_setting(spec.key)
        if stored is not None and stored != "":
            changes[spec.env_attr] = spec.coerce(stored)
    if not changes:
        return settings
    data = settings.model_dump()
    data.update(changes)
    return Settings(**data)
