from __future__ import annotations

from typing import Protocol

import httpx

from yc_monitor.models import CollectionResult, Source


class SourceAdapter(Protocol):
    source: Source

    async def collect(self, client: httpx.AsyncClient) -> CollectionResult: ...
