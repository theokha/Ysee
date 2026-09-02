# YC Launch Monitor

A persistent personal Slack bot that monitors the YC Directory, the official a16z Speedrun directory, X, and LinkedIn. It identifies founder self-announcements before a company appears in the official YC snapshot and avoids duplicate alerts through SQLite state.

## What is implemented

- Four source adapters with independent health reporting.
- Eight-hour APScheduler worker that runs one cycle immediately on `serve`, then on the interval, plus `run-once` CLI.
- SQLite deduplication across restarts and social platforms.
- GPT structured-output noise classifier with deterministic fallback and official-company guards.
- Slack Bot API alerts and single-workspace OAuth install routes.
- Pond Protocol 1.0 `/manifest` and `/runs`, plus `/healthz`.
- Docker deployment with persistent storage.

> **Source honesty:** the bounty calls the program “YC Speedrun,” but the verified Speedrun program is operated by Andreessen Horowitz. The bot monitors a16z’s official directory at `speedrun.a16z.com/companies` through its public API and labels alerts **a16z Speedrun**; it never presents those companies as YC. YC Directory uses the public yc-oss catalog plus an optional `changes/latest.json` freshness probe. Official YC Slack alerts fire for unseen slugs with a recent `launched_at` (new listings / new batches), not for restorations of old companies. X timestamps parse TwitterAPI.io's live `createdAt` form and ISO. LinkedIn posts are searched through HarvestAPI's pinned Apify actor `buIWk2uOUzTmcLsuB` using our own result normalization. That actor does not provide a complete census of newly created LinkedIn company pages. See [SETUP.md](SETUP.md).

## Quick start

```bash
cp .env.example .env
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -m yc_monitor run-once --dry-run
python -m yc_monitor serve
```

Open `http://localhost:8080/healthz` and `/manifest`. `serve` schedules one non-blocking monitor cycle immediately, then every `POLL_INTERVAL_HOURS` (default 8), with `max_instances=1` so cycles never overlap. Next-run time is in `status` / Pond `get_status` as `next_run_at`. Scheduler start failures are logged and do not take down HTTP. State lives at `DATABASE_PATH`.

## Architecture

Each adapter emits the same canonical item model. `pipeline.py` refreshes official sources first, builds the official identity set, then classifies social results. SQLite uniqueness constraints reserve an alert into a Slack outbox before delivery, so a Slack outage does not drop the alert. Pending and failed outbox rows retry on later runs; sent rows do not. If `SLACK_BOT_TOKEN` is unset, the notifier uses the latest OAuth-installed bot token from `slack_install`. A failed adapter is recorded but does not stop the other sources.

## Commands

- `python -m yc_monitor serve` — HTTP server and persistent scheduler (immediate first cycle, then interval).
- `python -m yc_monitor run-once --dry-run` — collect and report candidates without Slack delivery or YC/social alert, snapshot, bootstrap, or outbox mutations; run-health/GPT statistics are still recorded.
- `python -m yc_monitor run-once` — collect, enqueue new alerts, and retry pending/failed Slack deliveries.
- `python -m yc_monitor test-alert` — send a DEMO-labeled Block Kit message to the configured channel (not source evidence).
- `python -m yc_monitor status` — inspect persisted run, next scheduled run, GPT usage, dedup, and outbox state.
- `python -m yc_monitor leads --limit 20` — recent detections with source, reason, and snippet.

## Test

```bash
pytest
ruff check src tests
mypy src
```

See [PROOF.md](PROOF.md) before submitting the bounty.
