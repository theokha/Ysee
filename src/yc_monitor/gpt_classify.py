from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from yc_monitor.classify import PROGRAM, classify_social, matches_official_name, normalize_company
from yc_monitor.models import Alert, AlertKind, CanonicalItem, Classification

SYSTEM_PROMPT = """You classify social posts for an early YC or a16z Speedrun founder monitor.
A positive means the author is currently announcing their own named company's YC or Speedrun
acceptance, participation, or launch as a newly accepted company. Accept natural wording such as
"we're YC S26" or "today we're launching X (YC S26)" only when it is clearly first-party and the
company name is explicit in the post. Reject applications, interviews, rejections, hiring, events,
directories, news, aggregators, third-party congratulations, quotes, replies that merely recount a
past acceptance, retrospective stories, speculation, and already-known-company chatter. A positive
must include a supported company_name; never infer one from unrelated words. Be conservative."""


class SocialJudgement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_founder_self_announcement: bool
    company_name: str | None
    batch: str | None
    confidence: float = Field(ge=0, le=1)
    reason: str
    noise_type: str | None


class SocialJudge(Protocol):
    async def classify(
        self,
        item: CanonicalItem,
        official_names: set[str],
        official_hosts: set[str],
        official_handles: set[str],
    ) -> Classification: ...


@dataclass(slots=True)
class GPTCycleStats:
    calls: int = 0
    accepted: int = 0
    rejected: int = 0
    deferred: int = 0
    prefiltered: int = 0
    capped: int = 0
    max_calls: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "deferred": self.deferred,
            "prefiltered": self.prefiltered,
            "capped": self.capped,
            "max_calls": self.max_calls,
        }


def stats_from_classifications(
    classifications: list[Classification], max_calls: int = 0
) -> GPTCycleStats:
    stats = GPTCycleStats(max_calls=max_calls)
    for result in classifications:
        _accumulate_stats(stats, result)
    return stats


def _accumulate_stats(
    stats: GPTCycleStats, result: Classification, *, prefiltered: bool = False
) -> None:
    if prefiltered:
        stats.prefiltered += 1
    if result.alert:
        stats.accepted += 1
    elif result.persist:
        stats.rejected += 1
    else:
        stats.deferred += 1


class GPTSocialClassifier:
    def __init__(
        self,
        api_key: str | None,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        max_concurrency: int,
        min_confidence: float,
        max_calls_per_cycle: int = 25,
        immediate_min_confidence: float = 0.9,
        daily_budget_check: Callable[[], bool] | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.enabled = bool(api_key)
        self.model = model
        self.max_retries = max_retries
        self.min_confidence = min_confidence
        self.immediate_min_confidence = immediate_min_confidence
        self.max_calls_per_cycle = max_calls_per_cycle
        self.daily_budget_check = daily_budget_check
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self._lock = asyncio.Lock()
        self.stats = GPTCycleStats(max_calls=max_calls_per_cycle)
        self.client = client or (
            AsyncOpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)
            if api_key
            else None
        )

    def begin_cycle(self) -> None:
        self.stats = GPTCycleStats(max_calls=self.max_calls_per_cycle)

    async def classify(
        self,
        item: CanonicalItem,
        official_names: set[str],
        official_hosts: set[str],
        official_handles: set[str],
    ) -> Classification:
        fallback = classify_social(item, official_names, official_hosts, official_handles)
        cheap = cheap_prefilter(item, official_handles)
        if cheap:
            await self._record(cheap, prefiltered=True)
            return cheap
        if not self.enabled or not self.client:
            await self._record(fallback)
            return fallback

        async with self._lock:
            if self.stats.calls >= self.max_calls_per_cycle or (
                self.daily_budget_check is not None and not self.daily_budget_check()
            ):
                capped = Classification(None, "gpt_cycle_budget_exhausted", 0.0, persist=False)
                self.stats.capped += 1
                _accumulate_stats(self.stats, capped)
                return capped
            self.stats.calls += 1

        try:
            judgement = await self._judge(item)
        except (openai.APIError, openai.APITimeoutError, ValueError, TypeError):
            if fallback.alert:
                await self._record(fallback)
                return fallback
            deferred = Classification(None, "openai_classification_deferred", 0.0, persist=False)
            await self._record(deferred)
            return deferred

        if not judgement.is_founder_self_announcement or judgement.confidence < self.min_confidence:
            reason = judgement.reason.strip() or judgement.noise_type or "gpt_noise"
            result = Classification(None, f"gpt_rejected:{reason[:160]}", judgement.confidence)
            await self._record(result)
            return result
        if judgement.confidence < getattr(self, "immediate_min_confidence", 0.9):
            reason = "gpt_review:" + (judgement.reason.strip() or "needs_review")
            result = Classification(None, reason[:180], judgement.confidence, persist=True)
            await self._record(result)
            return result

        if not judgement.company_name or not judgement.company_name.strip():
            result = Classification(
                None, "gpt_rejected:positive_without_company_name", judgement.confidence
            )
            await self._record(result)
            return result
        item.company_name = judgement.company_name.strip()
        if judgement.batch:
            item.batch = judgement.batch.strip()
        official = suppress_official(item, official_names, official_hosts, official_handles)
        if official:
            await self._record(official)
            return official

        normalized = normalize_company(item.company_name)
        if not normalized:
            normalized = f"founder-{item.founder_handle or item.item_id}"
        result = Classification(
            Alert(
                AlertKind.EARLY_FOUNDER,
                item,
                f"early:{normalized}",
                judgement.confidence,
            ),
            "gpt_confirmed_founder_self_announcement",
            judgement.confidence,
        )
        await self._record(result)
        return result

    async def _record(self, result: Classification, *, prefiltered: bool = False) -> None:
        async with self._lock:
            _accumulate_stats(self.stats, result, prefiltered=prefiltered)

    async def _judge(self, item: CanonicalItem) -> SocialJudgement:
        assert self.client is not None
        payload = json.dumps(
            {
                "source": item.source.value,
                "author_name": item.founder_name,
                "author_handle": item.founder_handle,
                "existing_company": item.company_name,
                "url": item.canonical_url,
                "text": item.content_text[:3000],
            },
            ensure_ascii=False,
        )
        last_error: Exception | None = None
        async with self.semaphore:
            for attempt in range(self.max_retries + 1):
                try:
                    response = await self.client.responses.parse(
                        model=self.model,
                        input=[
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": payload},
                        ],
                        text_format=SocialJudgement,
                    )
                    parsed = response.output_parsed
                    if not isinstance(parsed, SocialJudgement):
                        raise TypeError("OpenAI response did not contain structured output")
                    return parsed
                except (openai.APITimeoutError, openai.RateLimitError, openai.InternalServerError) as exc:
                    last_error = exc
                    if attempt >= self.max_retries:
                        raise
                    await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError("OpenAI retry loop exited unexpectedly") from last_error


def cheap_prefilter(
    item: CanonicalItem, official_handles: set[str]
) -> Classification | None:
    text = item.content_text.strip()
    if not text:
        return Classification(None, "empty_content", 0.0)
    if item.founder_handle and item.founder_handle.lower() in official_handles:
        return Classification(None, "founder_already_official", 0.0)
    if not PROGRAM.search(text):
        return Classification(None, "missing_program_reference", 0.0)
    return None


def suppress_official(
    item: CanonicalItem,
    official_names: set[str],
    official_hosts: set[str],
    official_handles: set[str],
) -> Classification | None:
    if item.founder_handle and item.founder_handle.lower() in official_handles:
        return Classification(None, "founder_already_official", 0.0)
    if matches_official_name(item.company_name, official_names):
        return Classification(None, "company_already_official", 0.0)
    if item.company_url:
        from yc_monitor.classify import _host

        host = _host(item.company_url)
        if host and host in official_hosts:
            return Classification(None, "website_already_official", 0.0)
    return None
