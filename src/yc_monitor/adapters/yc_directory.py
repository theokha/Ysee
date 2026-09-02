from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from yc_monitor.models import AdapterHealth, CanonicalItem, CollectionResult, HealthStatus, Source

logger = logging.getLogger(__name__)

CATALOG_URL = "https://yc-oss.github.io/api/companies/all.json"
LATEST_CHANGES_URL = "https://yc-oss.github.io/api/changes/latest.json"
_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_HANDLE_URL = re.compile(
    r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/@?([A-Za-z0-9_]{1,15})(?:[/?#]|$)",
    re.IGNORECASE,
)


class YCDirectoryAdapter:
    source = Source.YC_DIRECTORY

    def __init__(self, latest_changes_url: str | None = LATEST_CHANGES_URL) -> None:
        self.latest_changes_url = latest_changes_url or None

    async def collect(self, client: httpx.AsyncClient) -> CollectionResult:
        response = await client.get(CATALOG_URL, timeout=30)
        response.raise_for_status()
        companies = response.json()
        if not isinstance(companies, list):
            raise TypeError("YC catalog response was not a list")
        changes = await self._fetch_latest_changes(client)
        added_slugs = latest_added_slugs(changes)
        items = []
        for company in companies:
            if not isinstance(company, dict):
                continue
            enriched = dict(company)
            enriched["_latest_changes_added"] = str(company.get("slug") or "") in added_slugs
            items.append(normalize_company(enriched))
        detail = f"YC catalog fetched ({len(items)} companies)"
        if changes:
            detail = f"{detail}; {format_latest_changes_health(changes)}"
        elif self.latest_changes_url:
            detail = f"{detail}; latest-changes probe failed"
        else:
            detail = f"{detail}; latest-changes probe disabled"
        return CollectionResult(
            items,
            AdapterHealth(self.source, HealthStatus.OK, detail, len(items)),
        )

    async def _fetch_latest_changes(self, client: httpx.AsyncClient) -> dict[str, Any] | None:
        if not self.latest_changes_url:
            return None
        try:
            response = await client.get(self.latest_changes_url, timeout=15)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.warning("YC latest-changes probe failed: %s", type(exc).__name__)
            return None
        return payload if isinstance(payload, dict) else None


def latest_added_slugs(payload: dict[str, Any] | None) -> set[str]:
    if not payload or not isinstance(payload.get("added"), list):
        return set()
    return {
        str(record.get("slug") or "").strip()
        for record in payload["added"]
        if isinstance(record, dict) and record.get("slug")
    }


def format_latest_changes_health(payload: dict[str, Any]) -> str:
    parts = ["latest-changes probe ok"]
    generated = payload.get("generated_at")
    if isinstance(generated, str) and generated.strip():
        parts.append(f"generated_at={generated.strip()}")
    summary = payload.get("summary")
    added = summary.get("added") if isinstance(summary, dict) else None
    if isinstance(added, int):
        parts.append(f"added={added}")
    return "; ".join(parts)


def normalize_company(company: dict[str, object]) -> CanonicalItem:
    slug = str(company.get("slug") or company.get("id") or "").strip()
    name = str(company.get("name") or slug)
    website = str(company.get("website") or "") or None
    launched = company.get("launched_at")
    published_at = None
    if isinstance(launched, (int, float)) and not isinstance(launched, bool):
        published_at = datetime.fromtimestamp(float(launched), UTC)
    return CanonicalItem(
        source=Source.YC_DIRECTORY,
        item_id=slug,
        company_name=name,
        canonical_url=str(company.get("url") or f"https://www.ycombinator.com/companies/{slug}"),
        company_url=website,
        description=str(company.get("one_liner") or ""),
        batch=str(company.get("batch") or "") or None,
        published_at=published_at,
        raw=company,
    )


def website_host(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url if "://" in url else f"https://{url}").hostname
    return host.lower().removeprefix("www.") if host else None


def extract_founder_handles(payload: dict[str, Any] | None) -> list[str]:
    """Pull founder X/Twitter handles from a yc-oss payload when present.

    The public all.json schema currently has no founder/social fields. Nested
    ``founders`` / ``twitter*`` / ``x_*`` keys are still parsed so a richer
    payload does not require a code change. Generic display names are ignored.
    """
    if not payload:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def add(handle: str | None) -> None:
        if not handle or handle in seen:
            return
        seen.add(handle)
        found.append(handle)

    for key in (
        "twitter_url",
        "twitter",
        "twitter_username",
        "twitter_handle",
        "x_url",
        "x_username",
        "x_handle",
    ):
        add(_coerce_handle(payload.get(key)))

    people = payload.get("founders") or payload.get("founder") or payload.get("team")
    if isinstance(people, dict):
        people = [people]
    if isinstance(people, list):
        for person in people:
            if isinstance(person, str):
                add(_coerce_handle(person))
            elif isinstance(person, dict):
                for key in (
                    "twitter_url",
                    "twitter",
                    "twitter_username",
                    "twitter_handle",
                    "x_url",
                    "x_username",
                    "x_handle",
                    "username",
                    "handle",
                ):
                    add(_coerce_handle(person.get(key)))
    return found


def _coerce_handle(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or " " in text:
        return None
    match = _HANDLE_URL.search(text)
    if match:
        candidate = match.group(1).lower()
        return candidate if candidate not in {"i", "intent", "share", "search"} else None
    handle = text.lstrip("@").lower()
    if handle.startswith("http") or "/" in handle:
        return None
    if not _HANDLE.fullmatch(handle):
        return None
    if handle in {"twitter", "x", "yc", "ycombinator"}:
        return None
    return handle
