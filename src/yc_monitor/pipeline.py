from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from slack_sdk.errors import SlackClientError

from yc_monitor.adapters.base import SourceAdapter
from yc_monitor.adapters.registry import build_adapters
from yc_monitor.adapters.yc_directory import extract_founder_handles, website_host
from yc_monitor.classify import company_aliases, normalize_company, official_alert
from yc_monitor.config import Settings
from yc_monitor.db import (
    LAST_GPT_STATS_KEY,
    LAST_USAGE_KEY,
    SPEEDRUN_CATALOG_BOOTSTRAP_KEY,
    YC_LAUNCHES_BOOTSTRAP_KEY,
    Database,
)
from yc_monitor.entity_resolver import TwitterCompanyResolver
from yc_monitor.gpt_classify import (
    GPTCycleStats,
    GPTSocialClassifier,
    SocialJudge,
    stats_from_classifications,
)
from yc_monitor.models import (
    AdapterHealth,
    Alert,
    CanonicalItem,
    Classification,
    CollectionResult,
    HealthStatus,
    Source,
)
from yc_monitor.runtime_settings import apply_runtime_settings
from yc_monitor.slack_app import SlackNotifier

logger = logging.getLogger(__name__)


def ingest_official_result(
    db: Database,
    result: CollectionResult,
    *,
    enqueue: bool = True,
    yc_max_age_days: int = 7,
) -> list[Alert]:
    if result.health.source == Source.YC_DIRECTORY:
        return ingest_yc_directory(
            db, result, enqueue=enqueue, max_age_days=yc_max_age_days
        )
    if result.health.source == Source.YC_SPEEDRUN:
        return ingest_speedrun_directory(db, result, enqueue=enqueue)
    if result.health.source == Source.YC_LAUNCHES:
        return ingest_catalog_like_source(
            db,
            result,
            bootstrap_key=YC_LAUNCHES_BOOTSTRAP_KEY,
            bootstrap_reason="yc_launches_bootstrap",
            enqueue=enqueue,
        )
    return []


def ingest_catalog_like_source(
    db: Database,
    result: CollectionResult,
    *,
    bootstrap_key: str,
    bootstrap_reason: str,
    enqueue: bool = True,
) -> list[Alert]:
    if result.health.status != HealthStatus.OK:
        return []
    bootstrapped = db.get_state(bootstrap_key) == "1"
    alerts: list[Alert] = []
    for item in result.items:
        upgrade_from = db.find_early_alert_key(item.company_name)
        upgrade_note = (
            f"Previously an early founder signal (`{upgrade_from}`)."
            if upgrade_from
            else None
        )
        alert = official_alert(item, upgrade_from=upgrade_from, upgrade_note=upgrade_note)
        if not bootstrapped:
            if enqueue:
                db.reserve_item(alert.dedup_key, item, "baseline", bootstrap_reason)
            continue
        if not enqueue:
            if not db.has_seen_item(alert.dedup_key):
                alerts.append(alert)
            continue
        if db.reserve_alert(alert):
            alerts.append(alert)
    if enqueue:
        db.put_state(bootstrap_key, "1")
    return alerts


def ingest_speedrun_directory(
    db: Database, result: CollectionResult, *, enqueue: bool = True
) -> list[Alert]:
    if result.health.status != HealthStatus.OK:
        return []
    bootstrapped = db.get_state(SPEEDRUN_CATALOG_BOOTSTRAP_KEY) == "1"
    alerts: list[Alert] = []
    for item in result.items:
        upgrade_from = db.find_early_alert_key(item.company_name)
        upgrade_note = (
            f"Previously an early founder signal (`{upgrade_from}`). Now listed by a16z Speedrun."
            if upgrade_from
            else None
        )
        alert = official_alert(item, upgrade_from=upgrade_from, upgrade_note=upgrade_note)
        if not bootstrapped:
            if enqueue:
                db.reserve_item(alert.dedup_key, item, "baseline", "speedrun_bootstrap")
            continue
        if not enqueue:
            if not db.has_seen_item(alert.dedup_key):
                alerts.append(alert)
            continue
        if db.reserve_alert(alert):
            alerts.append(alert)
    if enqueue:
        db.put_state(SPEEDRUN_CATALOG_BOOTSTRAP_KEY, "1")
    return alerts


def ingest_yc_directory(
    db: Database,
    result: CollectionResult,
    *,
    enqueue: bool = True,
    max_age_days: int = 7,
    now: datetime | None = None,
) -> list[Alert]:
    bootstrapped = db.is_yc_catalog_bootstrapped()
    successful = result.health.status == HealthStatus.OK
    if not successful:
        return []

    alerts: list[Alert] = []
    cutoff = (now or datetime.now(UTC)) - timedelta(days=max_age_days)
    for item in result.items:
        payload = item.raw if isinstance(item.raw, dict) else {}
        # Bounty: poll newly added companies / new batch listings, then skip
        # already-seen slugs. Recency of launched_at blocks restorations of old
        # companies (BelozFi W22). The daily yc-oss `added` list is only a
        # one-generation catalog diff — missing it must not hide a recent listing.
        eligible = bool(item.published_at and item.published_at >= cutoff)
        upgrade_from = db.find_early_alert_key(item.company_name)
        upgrade_note = (
            f"Previously an early founder signal (`{upgrade_from}`). Now listed in the official directory."
            if upgrade_from
            else None
        )
        if not enqueue:
            if bootstrapped and not db.has_yc_company(item.item_id) and eligible:
                alerts.append(
                    official_alert(item, upgrade_from=upgrade_from, upgrade_note=upgrade_note)
                )
            continue
        aliases = company_aliases(item.company_name, payload)
        primary = normalize_company(item.company_name) or item.item_id
        is_new = db.upsert_yc_company(
            item.item_id,
            primary,
            item.company_name or item.item_id,
            website_host(item.company_url),
            extract_founder_handles(payload),
            payload,
            aliases=aliases,
        )
        if not bootstrapped or not is_new or not eligible:
            continue
        alert = official_alert(item, upgrade_from=upgrade_from, upgrade_note=upgrade_note)
        if db.reserve_alert(alert):
            alerts.append(alert)
    if enqueue:
        db.mark_yc_catalog_bootstrapped()
    return alerts


class MonitorPipeline:
    def __init__(self, settings: Settings, social_classifier: SocialJudge | None = None) -> None:
        # `base_settings` never changes, so runtime overrides are always
        # recomputed from the .env defaults rather than layering on top of the
        # previous cycle's already-overridden copy.
        self.base_settings = settings
        self.settings = settings
        self.db = Database(settings.database_path)
        self.official_adapters, self.social_adapters = build_adapters(settings)
        self.notifier = SlackNotifier(settings, self.db)
        self.social_classifier = social_classifier or GPTSocialClassifier(
            settings.openai_api_key,
            settings.openai_model,
            settings.openai_timeout_seconds,
            settings.openai_max_retries,
            settings.openai_max_concurrency,
            settings.openai_min_confidence,
            settings.openai_max_calls_per_cycle,
            settings.openai_immediate_min_confidence,
            daily_budget_check=lambda: self.db.consume_daily_budget(
                "openai_calls", self.settings.openai_max_calls_per_day
            ),
            company_resolver=TwitterCompanyResolver(settings.twitterapi_io_api_key),
        )

    async def run(self, dry_run: bool = False) -> dict[str, object]:
        # Runtime overrides from /yc config take effect at the next cycle
        # without a restart.
        previous = self.settings
        self.settings = apply_runtime_settings(self.db, self.base_settings)
        if self.settings != previous:
            # Rebuild only the adapters, which are cheap config carriers. The
            # GPT classifier is refreshed in place below so an injected test
            # double is never replaced by a live API client.
            self.official_adapters, self.social_adapters = build_adapters(self.settings)
            self._sync_classifier_limits()
        run_id = f"run_{uuid.uuid4().hex}"
        self.db.start_run(run_id)
        begin_cycle = getattr(self.social_classifier, "begin_cycle", None)
        if callable(begin_cycle):
            begin_cycle()
        health: list[AdapterHealth] = []
        alerts: list[Alert] = []
        gpt_stats = GPTCycleStats(max_calls=self.settings.openai_max_calls_per_cycle).as_dict()
        try:
            async with httpx.AsyncClient(headers={"User-Agent": "YCLaunchMonitor/1.0"}) as client:
                for adapter in self.official_adapters:
                    result = await self._safe_collect(adapter, client)
                    health.append(result.health)
                    alerts.extend(
                        ingest_official_result(
                            self.db,
                            result,
                            enqueue=not dry_run,
                            yc_max_age_days=self.settings.yc_official_alert_max_age_days,
                        )
                    )
                names, hosts, handles = self.db.official_identities()
                social_items: list[CanonicalItem] = []
                for adapter in self.social_adapters:
                    result = await self._safe_collect(adapter, client)
                    health.append(result.health)
                    social_items.extend(
                        item
                        for item in result.items
                        if not self.db.has_seen_item(
                            f"{item.source.value}:{item.item_id}"
                        )
                    )
                classifications = await self._classify_social_items(
                    social_items, names, hosts, handles
                )
                gpt_stats = self._gpt_stats(classifications)
                self.db.put_state(LAST_GPT_STATS_KEY, json.dumps(gpt_stats))
                review_items: list[CanonicalItem] = []
                for item, classification in zip(
                    social_items, classifications, strict=True
                ):
                    published = item.published_at.isoformat() if item.published_at else None
                    if published and not dry_run:
                        self.db.put_state(
                            f"watermark:{item.source.value}", published
                        )
                    if not classification.persist:
                        continue
                    post_key = f"{item.source.value}:{item.item_id}"
                    if classification.alert:
                        alert = classification.alert
                        if dry_run:
                            alerts.append(alert)
                            continue
                        if not self.db.reserve_item(
                            post_key, item, "evidence", classification.reason
                        ):
                            continue
                        if self.db.reserve_alert(alert):
                            alerts.append(alert)
                    elif classification.reason.startswith("gpt_review:"):
                        review_items.append(item)
                        if not dry_run:
                            self.db.reserve_item(
                                post_key, item, "review", classification.reason
                            )
                    elif not dry_run:
                        self.db.reserve_item(
                            post_key, item, "rejected", classification.reason
                        )
                usage = {
                    "twitter_posts": sum(
                        1 for item in social_items if item.source == Source.TWITTER
                    ),
                    "linkedin_posts": sum(
                        1 for item in social_items if item.source == Source.LINKEDIN
                    ),
                    "review_count": len(review_items),
                    "gpt": gpt_stats,
                }
                if not dry_run:
                    self.db.put_state(LAST_USAGE_KEY, json.dumps(usage))
                    if review_items:
                        await self._post_ops(
                            "Review digest: "
                            + ", ".join(
                                (item.company_name or item.item_id)
                                for item in review_items[:8]
                            )
                        )
            delivered = 0
            if not dry_run:
                delivered = await self.deliver_outbox()
                await self._notify_source_health(health)
            self.db.finish_run(run_id, "completed", len(alerts), health)
            return {
                "run_id": run_id,
                "status": "completed",
                "alert_count": len(alerts),
                "alerts": [alert.dedup_key for alert in alerts],
                "delivered_count": delivered,
                "gpt": gpt_stats,
                "health": [
                    {"source": h.source.value, "status": h.status.value, "detail": h.detail}
                    for h in health
                ],
            }
        except Exception as exc:
            logger.exception("Monitor run failed")
            self.db.finish_run(run_id, "failed", len(alerts), health, type(exc).__name__)
            raise

    def _sync_classifier_limits(self) -> None:
        """Propagate runtime-tunable GPT knobs onto the live classifier.

        The classifier is mutated in place rather than rebuilt so an injected
        `SocialJudge` double (or a live AsyncOpenAI client) survives a config
        change; non-GPT doubles are left untouched.
        """
        classifier = self.social_classifier
        if not isinstance(classifier, GPTSocialClassifier):
            return
        classifier.min_confidence = self.settings.openai_min_confidence
        classifier.immediate_min_confidence = self.settings.openai_immediate_min_confidence
        classifier.max_calls_per_cycle = self.settings.openai_max_calls_per_cycle
        current = classifier.stats
        if isinstance(current, GPTCycleStats):
            current.max_calls = self.settings.openai_max_calls_per_cycle

    async def deliver_outbox(self) -> int:
        delivered = 0
        for alert in self.db.list_undelivered_alerts():
            thread_ts = self.db.slack_thread_for_early_key(alert.upgrade_from)
            try:
                posted = await self.notifier.send(alert, thread_ts=thread_ts)
            except (SlackClientError, OSError, TimeoutError, httpx.HTTPError) as exc:
                logger.warning(
                    "Slack delivery failed for %s: %s",
                    alert.dedup_key,
                    type(exc).__name__,
                )
                self.db.mark_outbox_failed(alert.dedup_key, type(exc).__name__)
                continue
            if posted:
                self.db.mark_outbox_sent(
                    alert.dedup_key,
                    posted.get("channel"),
                    posted.get("ts"),
                )
                delivered += 1
        return delivered

    async def _notify_source_health(self, health: list[AdapterHealth]) -> None:
        for item in health:
            source = item.source.value
            if item.status == HealthStatus.FAILED:
                failures = self.db.record_source_failure(source, item.detail)
                if failures == 2:
                    await self._post_ops(
                        f"Monitor degraded: `{source}` failed 2 consecutive cycles. {item.detail}"
                    )
            elif item.status == HealthStatus.OK:
                previous = self.db.clear_source_failure(source)
                if previous >= 2:
                    await self._post_ops(f"`{source}` monitor recovered.")

    async def _post_ops(self, text: str) -> None:
        channel = self.settings.slack_ops_channel_id or self.settings.slack_channel_id
        token = self.notifier._bot_token()
        if not token or not channel:
            return
        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient(token=token)
        try:
            await client.chat_postMessage(channel=channel, text=text)
        except (SlackClientError, OSError, TimeoutError):
            logger.warning("Failed to post ops notice")

    def _gpt_stats(self, classifications: list[Classification]) -> dict[str, int]:
        stats = getattr(self.social_classifier, "stats", None)
        if isinstance(stats, GPTCycleStats):
            return stats.as_dict()
        return stats_from_classifications(
            classifications, self.settings.openai_max_calls_per_cycle
        ).as_dict()

    async def _classify_social_items(
        self,
        items: list[CanonicalItem],
        names: set[str],
        hosts: set[str],
        handles: set[str],
    ) -> list[Classification]:
        return list(
            await asyncio.gather(
                *(
                    self.social_classifier.classify(item, names, hosts, handles)
                    for item in items
                )
            )
        )

    async def _safe_collect(
        self, adapter: SourceAdapter, client: httpx.AsyncClient
    ) -> CollectionResult:
        try:
            return await adapter.collect(client)
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            detail = str(exc).strip() or f"{type(exc).__name__}: collection failed"
            if len(detail) > 240:
                detail = detail[:237] + "..."
            logger.warning("%s adapter failed: %s", adapter.source.value, detail)
            return CollectionResult(
                [],
                AdapterHealth(adapter.source, HealthStatus.FAILED, detail),
            )
