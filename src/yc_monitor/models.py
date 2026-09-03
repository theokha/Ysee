from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Source(StrEnum):
    YC_DIRECTORY = "yc_directory"
    YC_SPEEDRUN = "yc_speedrun"
    TWITTER = "twitter"
    LINKEDIN = "linkedin"
    YC_LAUNCHES = "yc_launches"


class HealthStatus(StrEnum):
    OK = "ok"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class AlertKind(StrEnum):
    EARLY_FOUNDER = "early_founder"
    EARLY_YC_LAUNCH = "early_yc_launch"
    EARLY_SPEEDRUN_LAUNCH = "early_speedrun_launch"
    OFFICIAL_YC = "official_yc"
    OFFICIAL_SPEEDRUN = "official_speedrun"


@dataclass(slots=True)
class CanonicalItem:
    source: Source
    item_id: str
    company_name: str | None
    canonical_url: str
    description: str = ""
    content_text: str = ""
    company_url: str | None = None
    founder_name: str | None = None
    founder_handle: str | None = None
    author_url: str | None = None
    batch: str | None = None
    published_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AdapterHealth:
    source: Source
    status: HealthStatus
    detail: str = ""
    item_count: int = 0


@dataclass(slots=True)
class CollectionResult:
    items: list[CanonicalItem]
    health: AdapterHealth


@dataclass(slots=True)
class Alert:
    kind: AlertKind
    item: CanonicalItem
    dedup_key: str
    confidence: float = 1.0
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    upgrade_from: str | None = None
    upgrade_note: str | None = None


@dataclass(slots=True)
class Classification:
    alert: Alert | None
    reason: str
    confidence: float
    persist: bool = True


def alert_to_dict(alert: Alert) -> dict[str, Any]:
    item = alert.item
    return {
        "kind": str(alert.kind),
        "dedup_key": alert.dedup_key,
        "confidence": alert.confidence,
        "detected_at": alert.detected_at.isoformat(),
        "upgrade_from": alert.upgrade_from,
        "upgrade_note": alert.upgrade_note,
        "item": {
            "source": str(item.source),
            "item_id": item.item_id,
            "company_name": item.company_name,
            "canonical_url": item.canonical_url,
            "description": item.description,
            "content_text": item.content_text,
            "company_url": item.company_url,
            "founder_name": item.founder_name,
            "founder_handle": item.founder_handle,
            "author_url": item.author_url,
            "batch": item.batch,
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "raw": item.raw,
        },
    }


def alert_from_dict(payload: dict[str, Any]) -> Alert:
    item_data = payload["item"]
    published = item_data.get("published_at")
    item = CanonicalItem(
        source=Source(item_data["source"]),
        item_id=item_data["item_id"],
        company_name=item_data.get("company_name"),
        canonical_url=item_data["canonical_url"],
        description=item_data.get("description") or "",
        content_text=item_data.get("content_text") or "",
        company_url=item_data.get("company_url"),
        founder_name=item_data.get("founder_name"),
        founder_handle=item_data.get("founder_handle"),
        author_url=item_data.get("author_url"),
        batch=item_data.get("batch"),
        published_at=datetime.fromisoformat(published) if published else None,
        raw=item_data.get("raw") or {},
    )
    return Alert(
        kind=AlertKind(payload["kind"]),
        item=item,
        dedup_key=payload["dedup_key"],
        confidence=float(payload.get("confidence", 1.0)),
        detected_at=datetime.fromisoformat(payload["detected_at"]),
        upgrade_from=payload.get("upgrade_from"),
        upgrade_note=payload.get("upgrade_note"),
    )
