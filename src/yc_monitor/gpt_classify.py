from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from yc_monitor.classify import PROGRAM, classify_social, matches_official_name, normalize_company
from yc_monitor.entity_resolver import CompanyHandleResolver
from yc_monitor.models import Alert, AlertKind, CanonicalItem, Classification

SYSTEM_PROMPT = """Classify a social post for an early YC or a16z Speedrun monitor.
An alert requires ALL of these: the author speaks for the named company; this post currently
announces accelerator acceptance or cohort participation; it is not merely a product launch; and
company, program, and evidence are explicit in the post. Reject biographies, retrospectives,
advice, jokes/satire, replies promoting an existing company, hiring, events, directories, news,
third-party congratulations, quotes, summaries, speculation, and generic 'backed by YC' promotion.
Separate a product from its owning company. Never invent a name, site, batch, or relationship.
Evidence quotes must be exact short substrings from the post supporting the decision."""


class SocialJudgement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_founder_self_announcement: bool
    is_first_party: bool = False
    is_current_announcement: bool = False
    is_accelerator_acceptance: bool = False
    is_product_launch_only: bool = False
    is_retrospective: bool = False
    is_satire_or_joke: bool = False
    company_name: str | None
    product_name: str | None = None
    company_handle: str | None = None
    program: str | None = None
    batch: str | None
    evidence_quotes: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    reason: str
    noise_type: str | None = None

    @property
    def is_complete_announcement(self) -> bool:
        # A current first-party launch that names the company and batch is a
        # complete signal even when it is not an explicit acceptance post;
        # that case alerts as a distinct launch kind rather than acceptance.
        if self.is_accelerator_acceptance and self.is_product_launch_only:
            return False
        if self.is_product_launch_only and not self.is_current_announcement:
            return False
        return all((
            self.is_founder_self_announcement,
            self.is_first_party,
            self.is_current_announcement,
            not self.is_retrospective,
            not self.is_satire_or_joke,
            bool(self.company_name and self.company_name.strip()),
            bool(self.evidence_quotes),
        ))


RETROSPECTIVE = re.compile(
    r"\b(my life so far|years? ago|when we got into|i remember when|looking back|"
    r"if i (?:had to )?start(?:ed)? (?:again|from 0)|got accepted into yc\.\s*if i)\b",
    re.IGNORECASE,
)
GENERIC_COMPANY = re.compile(
    r"^(?:the\s+)?(?:our\s+)?(?:startup|company|ai company|healthcare company|"
    r"generational healthcare company|product|project|team|ai)$",
    re.IGNORECASE,
)
VALID_YC_BATCH = re.compile(r"^(?:YC\s*)?[SWF]\d{2}$", re.IGNORECASE)
VALID_SPEEDRUN_BATCH = re.compile(r"^(?:A16Z\s*)?(?:SPEEDRUN\s*)?SR\d{3}$", re.IGNORECASE)
MENTIONED_HANDLE = re.compile(r"@([A-Za-z0-9_]{1,30})")
CURRENT_ANNOUNCEMENT = re.compile(
    r"\b(today|just|excited to announce|thrilled to announce|we(?:'|’)re joining|"
    r"we (?:just )?got into|accepted into|selected for)\b",
    re.IGNORECASE,
)


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
        company_resolver: CompanyHandleResolver | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.enabled = bool(api_key)
        self.model = model
        self.max_retries = max_retries
        self.min_confidence = min_confidence
        self.immediate_min_confidence = immediate_min_confidence
        self.max_calls_per_cycle = max_calls_per_cycle
        self.daily_budget_check = daily_budget_check
        self.company_resolver = company_resolver
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

        text_lower = item.content_text.lower()
        evidence_valid = bool(judgement.evidence_quotes) and all(
            quote.strip() and quote.strip().lower() in text_lower
            for quote in judgement.evidence_quotes[:4]
        )
        company = (judgement.company_name or "").strip()
        batch = (judgement.batch or "").strip()
        speedrun_program = _is_speedrun_program(judgement.program, item.content_text)
        valid_batch = _valid_batch(batch, judgement.program) or (
            speedrun_program and _explicit_speedrun_announcement(item.content_text)
        )
        company_valid = bool(company) and not GENERIC_COMPANY.fullmatch(company)

        reason_text = judgement.reason.strip()
        contradictory_negative = (
            not judgement.is_founder_self_announcement
            and any(
                phrase in reason_text.lower()
                for phrase in (
                    "explicitly announces acceptance",
                    "clearly announces acceptance",
                    "current first-party acceptance",
                )
            )
        )
        if contradictory_negative:
            result = Classification(None, "gpt_review:contradictory_verdict", judgement.confidence)
            await self._record(result)
            return result
        if not judgement.is_founder_self_announcement or judgement.confidence < self.min_confidence:
            reason = reason_text or judgement.noise_type or "gpt_noise"
            result = Classification(None, f"gpt_rejected:{reason[:160]}", judgement.confidence)
            await self._record(result)
            return result
        if not judgement.is_complete_announcement:
            _store_review_company(item, company)
            reason = _incomplete_reason(judgement)
            result = Classification(None, f"gpt_review:{reason}", judgement.confidence)
            await self._record(result)
            return result
        if not company_valid or not evidence_valid or not valid_batch:
            _store_review_company(item, company)
            failed = (
                "generic_or_missing_company" if not company_valid
                else "unsupported_evidence" if not evidence_valid
                else "invalid_or_missing_batch"
            )
            result = Classification(None, f"gpt_review:{failed}", judgement.confidence)
            await self._record(result)
            return result
        if judgement.confidence < self.immediate_min_confidence:
            _store_review_company(item, company)
            reason = "gpt_review:" + (judgement.reason.strip() or "needs_review")
            result = Classification(None, reason[:180], judgement.confidence, persist=True)
            await self._record(result)
            return result

        item.company_name = company
        item.batch = batch
        item.raw["classification"] = {
            "confidence": judgement.confidence,
            "reason": judgement.reason,
            "program": judgement.program,
            "batch": batch,
            "company_name": company,
            "product_name": judgement.product_name,
            "company_handle": judgement.company_handle,
            "evidence_quotes": judgement.evidence_quotes[:4],
        }
        official = suppress_official(item, official_names, official_hosts, official_handles)
        if official:
            await self._record(official)
            return official
        mentioned = {value.lower() for value in MENTIONED_HANDLE.findall(item.content_text)}
        company_handle = (judgement.company_handle or "").lstrip("@").lower()
        if company_handle:
            mentioned.add(company_handle)
        if mentioned & {value.lower() for value in official_handles}:
            result = Classification(None, "company_handle_already_official", 0.0)
            await self._record(result)
            return result
        if company_handle and self.company_resolver is not None:
            resolution = await self.company_resolver.resolve_official(
                company_handle, official_names, official_hosts
            )
            if resolution is True:
                result = Classification(None, "resolved_company_already_official", 0.0)
                await self._record(result)
                return result
            if resolution is None:
                _store_review_company(item, company)
                result = Classification(None, "gpt_review:unresolved_company_handle", judgement.confidence)
                await self._record(result)
                return result
        elif judgement.product_name and company_handle:
            _store_review_company(item, company)
            result = Classification(None, "gpt_review:unresolved_product_owner", judgement.confidence)
            await self._record(result)
            return result

        normalized = normalize_company(item.company_name)
        kind = _alert_kind(judgement)
        result = Classification(
            Alert(
                kind,
                item,
                f"early:{normalized}",
                judgement.confidence,
            ),
            f"gpt_confirmed:{judgement.reason[:160]}",
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
    if RETROSPECTIVE.search(text):
        return Classification(None, "retrospective_not_current_announcement", 0.0)
    invalid_batch = re.search(r"\bYC\s+([A-Z]\d{2})\b", text, re.IGNORECASE)
    if invalid_batch and not VALID_YC_BATCH.fullmatch(f"YC {invalid_batch.group(1)}"):
        return Classification(None, "invalid_yc_batch_code", 0.0)
    if re.search(r"\bbacked by yc\b", text, re.IGNORECASE) and not CURRENT_ANNOUNCEMENT.search(text):
        return Classification(None, "yc_affiliation_without_current_acceptance", 0.0)
    return None


def _is_speedrun_program(program: str | None, text: str) -> bool:
    return "speedrun" in (program or "").lower() or bool(
        re.search(r"\b(?:a16z\s+)?speedrun\b|@speedrun\b", text, re.IGNORECASE)
    )


def _explicit_speedrun_announcement(text: str) -> bool:
    return bool(
        CURRENT_ANNOUNCEMENT.search(text)
        and re.search(r"\b(?:a16z\s+)?speedrun\b|@speedrun\b", text, re.IGNORECASE)
    )


def _alert_kind(judgement: SocialJudgement) -> AlertKind:
    program = (judgement.program or "").lower()
    if "speedrun" in program:
        return AlertKind.EARLY_SPEEDRUN_LAUNCH
    if judgement.is_accelerator_acceptance and not judgement.is_product_launch_only:
        return AlertKind.EARLY_FOUNDER
    return AlertKind.EARLY_YC_LAUNCH


def _valid_batch(batch: str, program: str | None) -> bool:
    if not batch:
        return False
    normalized_program = (program or "").lower()
    if "speedrun" in normalized_program or "speedrun" in batch.lower() or batch.upper().startswith("SR"):
        return bool(VALID_SPEEDRUN_BATCH.fullmatch(batch.strip()))
    return bool(VALID_YC_BATCH.fullmatch(batch.strip()))


def _store_review_company(item: CanonicalItem, company: str) -> None:
    """Carry the extracted name onto review rows so /yc leads labels them.

    Without this the review row keeps the adapter's NULL company_name and renders
    as a bare "X post"/"LinkedIn post". Only a real name is stored: a generic
    label is exactly why the row went to review in the first place.
    """
    if company and not GENERIC_COMPANY.fullmatch(company):
        item.company_name = company


def _incomplete_reason(judgement: SocialJudgement) -> str:
    failures = []
    if not judgement.is_first_party:
        failures.append("not_first_party")
    if not judgement.is_current_announcement:
        failures.append("not_current")
    if not judgement.is_accelerator_acceptance:
        failures.append("not_acceptance")
    if judgement.is_product_launch_only:
        failures.append("product_launch_only")
    if judgement.is_retrospective:
        failures.append("retrospective")
    if judgement.is_satire_or_joke:
        failures.append("satire_or_joke")
    if not judgement.company_name:
        failures.append("missing_company")
    if not judgement.evidence_quotes:
        failures.append("missing_evidence")
    return ",".join(failures) or "incomplete_evidence"


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
