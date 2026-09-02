from __future__ import annotations

import argparse
import asyncio
import json

import uvicorn

from yc_monitor.config import get_settings
from yc_monitor.pipeline import MonitorPipeline
from yc_monitor.slack_app import send_test_alert


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="YC Launch Monitor")
    sub = value.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Run HTTP server and persistent 8-hour scheduler")
    run = sub.add_parser("run-once", help="Run one monitor cycle")
    run.add_argument("--dry-run", action="store_true", help="Do not post alerts to Slack")
    sub.add_parser("status", help="Show persistent monitor status")
    leads = sub.add_parser("leads", help="Show recent detections without secrets")
    leads.add_argument("--limit", type=int, default=20)
    sub.add_parser(
        "test-alert",
        help="Send a DEMO-labeled Block Kit alert to the configured Slack channel",
    )
    return value


def main() -> None:
    args = parser().parse_args()
    settings = get_settings()
    if args.command == "serve":
        uvicorn.run("yc_monitor.pond_server:app", host="0.0.0.0", port=settings.port)
        return
    pipeline = MonitorPipeline(settings)
    if args.command == "status":
        print(json.dumps(pipeline.db.status(), indent=2))
    elif args.command == "leads":
        print(json.dumps(pipeline.db.recent_leads(args.limit), indent=2, default=str))
    elif args.command == "run-once":
        print(json.dumps(asyncio.run(pipeline.run(args.dry_run)), indent=2))
    elif args.command == "test-alert":
        print(json.dumps(asyncio.run(send_test_alert(settings, pipeline.db)), indent=2))


if __name__ == "__main__":
    main()
