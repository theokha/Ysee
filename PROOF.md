# Submission proof checklist

Do not claim fixture output is a live discovery.

- [x] `pytest` (49), Ruff, and strict mypy pass locally on 2026-09-02.
- [ ] Docker build passes (local Docker daemon was unavailable on 2026-09-02).
- [x] All four adapters show `ok` against verified live sources. Speedrun is correctly identified and labeled as the official **a16z** Speedrun directory, despite the bounty’s “YC Speedrun” wording.
- [x] Isolated and production-state `run-once --dry-run` completed with no Slack/outbox/snapshot mutation.
- [x] Slack bot authentication succeeded and a clearly DEMO-labeled Block Kit message was accepted by Slack.
- [ ] Capture a real source-derived alert arriving through the Slack API (requires explicit approval before external posting).
- [x] Repeated isolated cycle produced zero duplicate alerts; seen posts now skip GPT before classification.
- [x] Local `GET /healthz` and `GET /manifest` returned protocol 1.0 with `run_monitor_cycle` and `get_status`.
- [ ] Capture authenticated `POST /runs` after configuring `POND_ACCESS_KEY`.
- [ ] Add Slack screenshots or a short screen recording under `docs/proof/`.
- [x] Record the verified a16z Speedrun source and HarvestAPI LinkedIn provider here.

## Source notes (2026-09-01)

- TwitterAPI.io live `createdAt` sample: `Mon Aug 31 21:54:24 +0000 2026`.
- YC catalog: `https://yc-oss.github.io/api/companies/all.json`. Freshness probe: `https://yc-oss.github.io/api/changes/latest.json` (`generated_at` observed `2026-08-31T02:27:34.876Z`). Catalog rows have no founder Twitter fields.
- Speedrun: the bounty wording is inaccurate—Speedrun is operated by a16z, not YC. Official directory: `https://speedrun.a16z.com/companies`; public API: `https://speedrun-api.a16z.com/api/companies/companies/`.

## Verified live integrations (2026-09-02)

- YC Directory: 6,201 companies; latest-changes probe succeeded and reported two additions.
- a16z Speedrun: official public API returned 258 companies with `SR003`–`SR007` cohort metadata; pagination and first-run baseline behavior verified.
- X: six focused TwitterAPI.io query groups with a seven-day lookback; live timestamp parsing verified.
- LinkedIn: HarvestAPI actor `buIWk2uOUzTmcLsuB`, pinned build `tBATtQstpZt632roT`, returned normalized live posts under a cycle-wide budget.
- GPT: live structured classification accepted first-party founder launches and rejected event/news examples; production cap and defer behavior verified.
- Slack: bot authentication succeeded; DEMO test message sent to the configured channel without creating source evidence or outbox state.
- Dedup: second isolated cycle produced zero duplicate alerts. Dry runs leave YC snapshot, social seen rows, and outbox unchanged.
- Legacy state: pre-bootstrap 6,196 pending YC rows without outbox entries were backed up and safely repaired.

## Remaining external evidence

Add dated Slack screenshots/screen recording and deployed Pond responses here before submission. Redact tokens, workspace-private channel details, and provider credentials. HTTPS is live at `https://43.153.196.115.sslip.io` (`/healthz`, `/manifest`). Pond registration: set the agent origin to that URL and use the VPS `POND_ACCESS_KEY`. Slack `/yc` Request URL: `https://43.153.196.115.sslip.io/slack/commands` (also needs `SLACK_SIGNING_SECRET`).

## Live evidence (2026-09-02/03)

- Pond `POST /runs` (`get_status`) over HTTPS, authenticated: `completed`, 6,201 YC companies, outbox `sent: 7`, next run scheduled.
- Live cycle health, all sources `ok`:
  - `yc_directory`: 6,201 companies; latest-changes probe ok
  - `yc_speedrun`: 258 companies (official a16z API)
  - `yc_launches`: 20 posts (Launch YC JSON feed; baselined)
  - `twitter`: 6 query groups, 7-day lookback, 199 posts
  - `linkedin`: HarvestAPI actor, cycle budget 50, 37 posts (recovered after Apify 403 fix)
- Watermarks persisted: `watermark:twitter`, `watermark:linkedin`
- Usage persisted per cycle: twitter/linkedin post counts, review count, GPT counters
- GPT caps live: cycle `calls: 25` = `max_calls: 25`, 24 deferred (not rejected)
- Slack delivery: outbox `sent: 7` (no duplicates on repeat cycles)
