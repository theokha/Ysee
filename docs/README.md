# Diagrams

`yc-monitor-workflow.drawio` is the single source of truth for the architecture diagrams embedded in the top-level [README](../README.md). It has three pages:

| Page | Name | Covers |
| --- | --- | --- |
| 1 | End-to-End Workflow | Triggers, `MonitorPipeline.run`, official + social paths, delivery and close-out |
| 2 | Classification Decision Tree | `GPTSocialClassifier.classify` — prefilter, budget, LLM judgement, every branch to accept/review/reject/defer |
| 3 | Data Stores, Review Loop & Control Surface | SQLite tables, `/yc review` re-judge loop, audit replay, HTTP routes, settings split |

## Editing

Open the file at [app.diagrams.net](https://app.diagrams.net) (File → Open From → Device) or in the draw.io desktop app. Commit the `.drawio` file alongside the re-exported PNGs so the two never drift.

## Re-exporting

GitHub does not render `.drawio` inline, so the README embeds PNGs. After editing, regenerate all three with the desktop app's CLI:

```bash
for i in 1 2 3; do
  /Applications/draw.io.app/Contents/MacOS/draw.io --no-sandbox -x -f png \
    -s 1 -b 16 --page-index "$i" \
    -o "docs/workflow-${i}.png" docs/yc-monitor-workflow.drawio
done
```

Run it from the repository root. On Linux, substitute the `drawio` binary for the macOS app path. Note that `--page-index` is 1-based in draw.io 27.0.2 and later, and 0-based before it — if page 1 comes out as page 2, your build uses the older convention.

## Keeping the diagrams honest

The diagrams carry `file.py:line` references. Those go stale when code moves, so when you change any of the following, re-check the matching page:

- **Page 1** — adapter set or endpoints (`adapters/`), ingest gating and bootstrap logic (`pipeline.py`), outbox delivery (`deliver_outbox`)
- **Page 2** — prefilter gates or the judgement gate chain (`gpt_classify.py`), confidence thresholds (`config.py`), heuristic gates (`classify.py`)
- **Page 3** — schema changes (`db.py`), the re-judge loop (`rejudge_review_queue`), routes (`pond_server.py`), slash commands (`slack_app.py`), tunable keys (`runtime_settings.py`)
