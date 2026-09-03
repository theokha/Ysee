from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from yc_monitor.classify import matches_official_name

TWITTER_USER_INFO_URL = "https://api.twitterapi.io/twitter/user/info"


class CompanyHandleResolver(Protocol):
    async def resolve_official(
        self, handle: str, official_names: set[str], official_hosts: set[str]
    ) -> bool | None: ...


class TwitterCompanyResolver:
    """Resolve a mentioned X handle to an official YC company identity.

    True means a strong name/domain match; False means fetched but no match;
    None means resolution failed and the candidate must go to review.
    """

    def __init__(self, api_key: str | None) -> None:
        self.api_key = api_key
        self._cache: dict[str, dict[str, Any] | None] = {}

    async def resolve_official(
        self, handle: str, official_names: set[str], official_hosts: set[str]
    ) -> bool | None:
        normalized = handle.lstrip("@").lower()
        if not normalized or not self.api_key:
            return None
        if normalized not in self._cache:
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        TWITTER_USER_INFO_URL,
                        headers={"X-API-Key": self.api_key},
                        params={"userName": normalized},
                        timeout=15,
                    )
                    response.raise_for_status()
                    payload = response.json()
            except (httpx.HTTPError, ValueError, TypeError):
                self._cache[normalized] = None
            else:
                data = payload.get("data") if isinstance(payload, dict) else None
                self._cache[normalized] = data if isinstance(data, dict) else payload if isinstance(payload, dict) else None
        profile = self._cache[normalized]
        if profile is None:
            return None
        names = {
            str(profile.get("name") or ""),
            str(profile.get("userName") or profile.get("username") or ""),
        }
        if any(matches_official_name(name, official_names) for name in names if name):
            return True
        for key in ("url", "website", "expandedUrl"):
            value = profile.get(key)
            if isinstance(value, str) and value:
                host = urlparse(value if "://" in value else f"https://{value}").hostname
                if host and host.lower().removeprefix("www.") in official_hosts:
                    return True
        return False
