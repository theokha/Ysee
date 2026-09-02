from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from yc_monitor.models import AdapterHealth, CanonicalItem, CollectionResult, HealthStatus, Source

LAUNCHES_URL = "https://www.ycombinator.com/launches"


class YCLaunchesAdapter:
    """Official Launch YC feed (founder launch posts on ycombinator.com/launches)."""

    source = Source.YC_LAUNCHES

    async def collect(self, client: httpx.AsyncClient) -> CollectionResult:
        response = await client.get(
            LAUNCHES_URL,
            headers={"Accept": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
            raise TypeError("Launch YC response was missing hits")
        items = [
            item
            for record in payload["hits"]
            if isinstance(record, dict) and (item := normalize_launch(record))
        ]
        return CollectionResult(
            items,
            AdapterHealth(
                self.source,
                HealthStatus.OK,
                f"Launch YC fetched ({len(items)} posts)",
                len(items),
            ),
        )


def normalize_launch(record: dict[str, Any]) -> CanonicalItem | None:
    launch_id = str(record.get("id") or record.get("slug") or "").strip()
    raw_company = record.get("company")
    company: dict[str, Any] = raw_company if isinstance(raw_company, dict) else {}
    name = str(company.get("name") or record.get("title") or "").strip()
    slug = str(company.get("slug") or "").strip()
    url = str(record.get("search_path") or "")
    if not launch_id or not name or not url:
        return None
    published = None
    created = record.get("created_at")
    if isinstance(created, str):
        try:
            published = datetime.fromisoformat(created)
        except ValueError:
            published = None
    return CanonicalItem(
        source=Source.YC_LAUNCHES,
        item_id=launch_id,
        company_name=name,
        canonical_url=url,
        company_url=str(company.get("url") or "") or None,
        description=str(record.get("tagline") or record.get("title") or ""),
        batch=str(company.get("batch") or "") or None,
        published_at=published,
        raw={"slug": slug, **record},
    )
