from __future__ import annotations

from typing import Any

import httpx

from yc_monitor.models import AdapterHealth, CanonicalItem, CollectionResult, HealthStatus, Source

OFFICIAL_DIRECTORY_URL = "https://speedrun.a16z.com/companies"
OFFICIAL_API_URL = "https://speedrun-api.a16z.com/api/companies/companies/"
MAX_PAGE_SIZE = 100


class YCSpeedrunAdapter:
    """Monitor a16z Speedrun's official public company directory.

    The bounty calls this "YC Speedrun", but Speedrun is an a16z program. The
    source enum is retained for database compatibility; alert copy identifies
    the program correctly as a16z Speedrun.
    """

    source = Source.YC_SPEEDRUN

    def __init__(self, url: str | None = OFFICIAL_API_URL) -> None:
        self.url = url or None

    async def collect(self, client: httpx.AsyncClient) -> CollectionResult:
        if not self.url:
            return CollectionResult(
                [],
                AdapterHealth(
                    self.source,
                    HealthStatus.SKIPPED,
                    "a16z Speedrun API is disabled by configuration",
                ),
            )

        records = await fetch_all_companies(client, self.url)
        items = [item for record in records if (item := normalize_speedrun_company(record))]
        return CollectionResult(
            items,
            AdapterHealth(
                self.source,
                HealthStatus.OK,
                (
                    f"Official a16z Speedrun directory fetched ({len(items)} companies); "
                    f"directory={OFFICIAL_DIRECTORY_URL}"
                ),
                len(items),
            ),
        )


async def fetch_all_companies(
    client: httpx.AsyncClient, api_url: str = OFFICIAL_API_URL
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    offset = 0
    expected_count: int | None = None
    while expected_count is None or offset < expected_count:
        response = await client.get(
            api_url,
            params={"limit": MAX_PAGE_SIZE, "offset": offset, "ordering": "name"},
            timeout=30,
            follow_redirects=True,
        )
        if response.status_code in {404, 410}:
            raise ValueError(f"Speedrun API returned {response.status_code}")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("Speedrun API response was not an object")
        raw_results = payload.get("results")
        count = payload.get("count")
        if not isinstance(raw_results, list) or not isinstance(count, int):
            raise TypeError("Speedrun API response was missing count/results")
        expected_count = count
        page = [record for record in raw_results if isinstance(record, dict)]
        records.extend(page)
        if not raw_results:
            break
        offset += len(raw_results)
    return records


def normalize_speedrun_company(record: dict[str, Any]) -> CanonicalItem | None:
    company_id = str(record.get("id") or "").strip()
    slug = str(record.get("slug") or "").strip()
    name = str(record.get("name") or "").strip()
    if not company_id or not slug or not name:
        return None

    founders = record.get("founder_set")
    founder_name: str | None = None
    if isinstance(founders, list) and founders and isinstance(founders[0], dict):
        first = str(founders[0].get("first_name") or "").strip()
        last = str(founders[0].get("last_name") or "").strip()
        founder_name = f"{first} {last}".strip() or None

    cohort = str(record.get("cohort") or "").strip() or "Speedrun"
    return CanonicalItem(
        source=Source.YC_SPEEDRUN,
        item_id=company_id,
        company_name=name,
        canonical_url=f"https://speedrun.a16z.com/companies/{slug}",
        company_url=str(record.get("website_url") or "").strip() or None,
        description=str(record.get("preamble") or record.get("description") or "").strip(),
        founder_name=founder_name,
        batch=f"a16z {cohort}",
        raw=record,
    )
