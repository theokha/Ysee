# Setup

## Slack personal app

1. Create an app from `slack_app_manifest.yaml` in the target workspace.
2. Replace its redirect URL with `PUBLIC_BASE_URL/slack/oauth_redirect`.
3. Copy client ID, client secret, and signing secret into `.env`.
4. Deploy the service and visit `PUBLIC_BASE_URL/slack/install`, or install manually and set `SLACK_BOT_TOKEN`.
5. Set `SLACK_CHANNEL_ID`; invite the bot if posting to a private channel.

This is a single-workspace app, not a Slack Marketplace listing. OAuth tokens are stored in SQLite. If `SLACK_BOT_TOKEN` is unset, alerts use the latest installed token from `slack_install`. For the simplest personal deployment, putting the installed `xoxb-` token in the environment is still sufficient. `python -m yc_monitor test-alert` posts a clearly DEMO-labeled message to `SLACK_CHANNEL_ID` without treating it as a real detection.

Slash command `/yc` (or `/yc status`) replies with ephemeral monitor status. It requires `SLACK_SIGNING_SECRET`, the `commands` bot scope, and a public Request URL of `PUBLIC_BASE_URL/slack/commands`. Reinstall the app after adding the command. Slack cards include original-post quotes for early signals, directory/website buttons, and an upgrade note when a previously early company is later confirmed by YC or a16z Speedrun.

## X

Create a TwitterAPI.io key and set `TWITTERAPI_IO_API_KEY`. Queries use the Latest advanced-search endpoint with bounded pages (`TWITTER_MAX_PAGES`, default 3) and append `since_time:<unix timestamp>` for a rolling window. `TWITTER_LOOKBACK_DAYS` defaults to 7 and accepts 1–30 days. The window intentionally overlaps every eight-hour cycle; persistent tweet IDs prevent duplicate alerts.

The query pack is several focused groups (direct acceptance, Y Combinator wording, founder-launch phrasing, backing, Speedrun, and current batch codes) rather than two broad OR queries. Set `TWITTER_CURRENT_BATCHES` to a comma-separated list of `Syy`/`Wyy`/`Fyy` codes (default `F26,W27,S27` as of 2026-09-01). Operators are limited to quoted phrases, `OR`, `lang:en`, and `since_time`. Live tweet `createdAt` values look like `Mon Aug 31 21:54:24 +0000 2026`; ISO-8601 is also accepted. Keep `TWITTER_MAX_PAGES` low to control spend.

## LinkedIn through HarvestAPI / Apify

Set `APIFY_API_TOKEN`. The monitor runs HarvestAPI's LinkedIn post-search actor `buIWk2uOUzTmcLsuB` (`harvestapi/linkedin-post-search`) and pins build `ASBzmjLXGQlvadkLr` (`0.0.110`). A mismatched actor build is recorded as `apify_build_drift` instead of a generic `ValueError`. One actor run covers the three YC/Speedrun queries, date-sorted, limited to the past 24 hours.

`LINKEDIN_TOTAL_POSTS` is the **cycle-wide** upper bound (default 50), not a per-query cap. HarvestAPI's `maxPosts` is per search query, so the monitor allocates that budget across the three queries (for 50 posts: `maxPosts=16` × 3 = 48) and still truncates the normalized result set to `LINKEDIN_TOTAL_POSTS`. Do not pass the cycle total as raw `maxPosts` or spend will be about 3× the configured budget. The overlapping 24-hour window is safe because SQLite deduplicates stable LinkedIn post ids.

The monitor uses its own defensive normalizer for the actor's `id`/`entityId`, `linkedinUrl`, `content`, `author`, and `postedAt` output. It refuses unexpected actor builds until the schema is reviewed. Override `LINKEDIN_ACTOR_ID` or `LINKEDIN_ACTOR_BUILD_ID` only after validating the replacement.

This actor searches LinkedIn posts; it does not enumerate every newly created organization page. Do not claim complete company-page creation monitoring from this actor alone. Confirm the actor's pricing and LinkedIn-related terms before production use.

## OpenAI social noise classification

Set `OPENAI_API_KEY` to enable GPT classification after X and LinkedIn collection. GPT is a noise filter, not a monitoring source: Python still treats the refreshed YC identity set as authoritative and suppresses already-official companies after GPT extracts company and batch fields. Without a key, the existing deterministic classifier remains active.

The default model is `gpt-5.6-luna`, with confidence threshold `OPENAI_MIN_CONFIDENCE=0.65`, concurrency 4, timeout 20 seconds, two retries, and `OPENAI_MAX_CALLS_PER_CYCLE=25`. That cycle cap is a hard spend ceiling: extra candidates are deferred (`persist=false`) rather than classified. Obvious posts without YC/Y Combinator/Speedrun references never consume a call (deterministic prefilter). Run results and `status` include `gpt` counters: `calls`, `accepted`, `rejected`, `deferred`, `prefiltered`, `capped`, and `max_calls`. Successful noise decisions are remembered; API failures and budget-exhausted items are deferred without a rejected DB record so the overlapping source window can retry them.

## YC Directory freshness

The catalog source of truth is the public yc-oss snapshot `https://yc-oss.github.io/api/companies/all.json` (no auth). After a successful catalog fetch, the adapter optionally GETs `YC_LATEST_CHANGES_URL` for health/freshness only.

Official YC Slack alerts follow the bounty: poll for newly added companies and new batch listings, then skip already-seen slugs. After bootstrap, an unseen company alerts only if `launched_at` is within `YC_OFFICIAL_ALERT_MAX_AGE_DAYS` (default 7). That blocks restorations of old companies (Winter 2022 BelozFi) while still alerting a Fall 2026 listing from a few days ago even if it is no longer in the one-generation `changes/latest.json` `added` list. Nested founder handles are extracted when present.

## Speedrun

The bounty calls this source “YC Speedrun,” but verification found that Speedrun is an Andreessen Horowitz program, not a YC sub-program. YC’s `/speedrun` URL returns 404 and yc-oss has no Speedrun cohort. The official a16z program page links to `https://speedrun.a16z.com/companies`, whose public API is `https://speedrun-api.a16z.com/api/companies/companies/`.

`YC_SPEEDRUN_URL` defaults to that API for backward-compatible configuration. The adapter paginates the complete directory, currently returning company UUID, slug, name, `SR00x` cohort, website, description, and founders. Slack copy explicitly says **a16z Speedrun**. The first successful cycle baselines existing companies without alerting; later unseen companies create incremental alerts. Set `YC_SPEEDRUN_URL=` only to disable this source.

## Pond

Set `POND_ACCESS_KEY`, deploy at public HTTPS, then register that origin in Pond. `GET /manifest` is public. `POST /runs` needs:

```text
Authorization: Bearer <POND_ACCESS_KEY>
X-Agent-Protocol-Version: 1.0
Idempotency-Key: <same value as JSON run_id>
```

Actions are `get_status` and `run_monitor_cycle` (`input.dry_run` optional). `get_status` includes `next_run_at` and the last cycle's `gpt` usage counters. `serve` starts one non-blocking monitor cycle immediately (`SCHEDULER_RUN_IMMEDIATELY=true` by default), then every `POLL_INTERVAL_HOURS`. Overlapping cycles are prevented (`max_instances=1`). A scheduler start failure is logged and does not take down HTTP.

## Persistent deployment

`docker compose up -d` mounts `./data`. Render uses a persistent disk at `/var/data`. Do not deploy SQLite on an ephemeral filesystem.

VPS helpers:

```bash
./scripts/backup-db.sh
./scripts/deploy-vps.sh
```

The VPS currently serves HTTPS via Caddy at `https://43.153.196.115.sslip.io` (Let's Encrypt). Slack slash command Request URL: `https://43.153.196.115.sslip.io/slack/commands`. Pond origin: the same host. Set `SLACK_SIGNING_SECRET` for `/yc`. A custom domain can replace sslip.io later.
