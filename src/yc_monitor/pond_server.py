from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from apscheduler.events import (  # type: ignore[import-untyped]
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_SUBMITTED,
)
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from yc_monitor import __version__
from yc_monitor.config import Settings, get_settings
from yc_monitor.db import SCHEDULER_NEXT_RUN_KEY
from yc_monitor.pipeline import MonitorPipeline
from yc_monitor.scheduler import build_scheduler, job_next_run_iso, schedule_first_run
from yc_monitor.slack_app import ACK_TEXTS, handle_slash_command, verify_slack_signature
from yc_monitor.slack_format import format_leads_blocks

logger = logging.getLogger(__name__)

# Slack gives a slash command 3 seconds to respond, but a scan/review/retry can
# run for minutes. handle_slash_command answers these with a quick ack (see
# slack_app.ACK_TEXTS); matching that ack here tells the route to schedule the
# real work on the event loop instead of blocking the response.
BACKGROUND_ACK_TO_ACTION = {text: action for action, text in ACK_TEXTS.items()}

# Held references: a task nobody points at can be garbage-collected mid-run.
_background_tasks: set[asyncio.Task[None]] = set()


def _summarize_action(action: str, result: dict[str, Any]) -> str:
    if action in {"scan_requested", "dry_scan_requested"}:
        return (
            f"Dry scan complete. {result.get('alert_count', 0)} candidate(s), nothing posted."
            if action == "dry_scan_requested"
            else (
                f"Scan complete. {result.get('alert_count', 0)} new alert(s), "
                f"{result.get('delivered_count', 0)} delivered."
            )
        )
    if action == "review_requested":
        promoted_keys = result.get("promoted")
        promoted_names = result.get("promoted_names")
        summary = (
            f"Re-reviewed {result.get('reviewed', 0)} queued post(s): "
            f"{len(promoted_keys) if isinstance(promoted_keys, list) else 0} promoted "
            f"(and sent), {result.get('cleared', 0)} cleared, "
            f"{result.get('deferred', 0)} still deferred."
        )
        if isinstance(promoted_names, list) and promoted_names:
            summary += " New alerts: " + ", ".join(str(name) for name in promoted_names[:8])
        return summary
    return f"Retried outbox. Delivered {result.get('delivered', 0)} message(s)."


async def _run_background_scan(
    action: str, payload: dict[str, str], pipeline: MonitorPipeline
) -> None:
    """Run a slow slash-command action off the request path, then DM the result.

    Both halves are guarded: a failure in the work still reaches the user, and a
    failure in the follow-up post never surfaces as an unobserved task error.
    """
    user_id = payload.get("user_id", "")
    channel_id = payload.get("channel_id", "")
    try:
        if action in {"scan_requested", "dry_scan_requested"}:
            result: dict[str, Any] = await pipeline.run(
                dry_run=action == "dry_scan_requested"
            )
        elif action == "review_requested":
            result = await pipeline.rejudge_review_queue(25)
        elif action == "retry_requested":
            result = {"delivered": await pipeline.deliver_outbox()}
        else:
            logger.warning("Unknown background action %r; nothing to do", action)
            return
        summary = _summarize_action(action, result)
    except Exception as exc:
        logger.exception("Background slash-command action %s failed", action)
        summary = f"That background job failed ({type(exc).__name__}). Nothing was posted."
    try:
        await pipeline.notifier.post_ephemeral(user_id, channel_id, summary)
    except Exception:
        logger.exception("Failed to deliver background result for %s", action)


class RunRequest(BaseModel):
    model_config = {"extra": "ignore"}

    run_id: str = Field(min_length=1)
    agent_id: str | None = None
    conversation_id: str | None = None
    action_id: str | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    input: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)


def manifest() -> dict[str, Any]:
    return {
        "protocol": "marketplace-agent",
        "protocol_version": "1.0",
        "agent_version": __version__,
        "metadata": {
            "name": "YC Launch Monitor",
            "short_description": "Stateful YC/Speedrun monitor with early founder Slack alerts",
            "category": "sales lead generation",
        },
        "actions": [
            {
                "id": "run_monitor_cycle",
                "name": "Run monitor cycle",
                "description": "Collect all sources, classify, deduplicate, and optionally alert Slack",
                "input_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"dry_run": {"type": "boolean"}},
                },
            },
            {
                "id": "get_status",
                "name": "Get monitor status",
                "description": "Return last run, next scheduled run, GPT usage, and persistent seen counts",
                "input_schema": {"type": "object", "additionalProperties": False},
            },
            {
                "id": "list_leads",
                "name": "List recent leads",
                "description": "Return recent alerted or pending companies from persistent state",
                "input_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50}
                    },
                },
            },
        ],
        "capabilities": {"sync": True, "streaming": False, "async_tasks": False},
        "input_modes": ["text/plain"],
        "output_modes": ["application/json", "text/markdown"],
        "limits": {"max_request_bytes": 1048576, "max_run_seconds": 120},
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    pipeline = MonitorPipeline(settings)
    try:
        archived = pipeline.db.archive_known_noise()
        if archived:
            logger.info("Archived %d known-noise alert(s) at startup", archived)
    except Exception:
        logger.exception("Failed to archive known-noise alerts; continuing startup")
    scheduler = build_scheduler(pipeline, settings.poll_interval_hours)

    def _persist_next_run(_event: object | None = None) -> None:
        try:
            iso = job_next_run_iso(scheduler)
            if iso:
                pipeline.db.put_state(SCHEDULER_NEXT_RUN_KEY, iso)
        except Exception:
            logger.exception("Failed to persist scheduler next-run time")

    scheduler.add_listener(
        _persist_next_run,
        EVENT_JOB_SUBMITTED | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            scheduler.start()
            first = schedule_first_run(
                scheduler,
                settings.poll_interval_hours,
                run_immediately=settings.scheduler_run_immediately,
            )
            pipeline.db.put_state(SCHEDULER_NEXT_RUN_KEY, first.isoformat())
            logger.info("Scheduler started; next monitor cycle at %s", first.isoformat())
        except Exception:
            logger.exception("Scheduler failed to start; HTTP server will continue")
        app.state.scheduler = scheduler
        try:
            yield
        finally:
            try:
                if scheduler.running:
                    scheduler.shutdown(wait=False)
            except Exception:
                logger.exception("Scheduler shutdown failed")

    app = FastAPI(title="YC Launch Monitor", version=__version__, lifespan=lifespan)
    app.state.pipeline = pipeline
    app.state.scheduler = scheduler
    oauth_state = secrets.token_urlsafe(24)

    @app.get("/slack/install")
    async def slack_install() -> RedirectResponse:
        if not settings.slack_client_id:
            raise HTTPException(503, "Slack OAuth is not configured")
        scope = "chat:write,chat:write.public,im:write,commands"
        url = (
            "https://slack.com/oauth/v2/authorize"
            f"?client_id={settings.slack_client_id}&scope={scope}&state={oauth_state}"
        )
        return RedirectResponse(url)

    @app.get("/slack/oauth_redirect")
    async def slack_oauth_redirect(
        code: str = Query(min_length=1), state: str = Query(min_length=1)
    ) -> dict[str, str]:
        if not hmac.compare_digest(state, oauth_state):
            raise HTTPException(400, "invalid OAuth state")
        if not settings.slack_client_id or not settings.slack_client_secret:
            raise HTTPException(503, "Slack OAuth is not configured")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://slack.com/api/oauth.v2.access",
                data={
                    "client_id": settings.slack_client_id,
                    "client_secret": settings.slack_client_secret,
                    "code": code,
                    "redirect_uri": f"{settings.public_base_url}/slack/oauth_redirect",
                },
                timeout=20,
            )
        payload = response.json()
        token = payload.get("access_token")
        team = payload.get("team") or {}
        if not payload.get("ok") or not token or not team.get("id"):
            raise HTTPException(400, "Slack OAuth exchange failed")
        pipeline.db.save_slack_install(str(team["id"]), str(token))
        return {"status": "installed", "team_id": str(team["id"])}

    @app.post("/slack/commands")
    async def slack_commands(request: Request) -> JSONResponse:
        if not settings.slack_signing_secret:
            raise HTTPException(503, "Slack slash commands are not configured")
        body = await request.body()
        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")
        if not verify_slack_signature(settings.slack_signing_secret, timestamp, body, signature):
            raise HTTPException(401, "invalid Slack signature")
        from yc_monitor.slack_app import slash_command_payload

        payload = slash_command_payload(body)
        admins = {value.strip() for value in settings.slack_admin_users.split(",") if value.strip()}
        response = handle_slash_command(
            payload.get("command", ""),
            payload.get("text", ""),
            pipeline.db.status(),
            user_id=payload.get("user_id", ""),
            db=pipeline.db,
            admin_users=admins if admins else None,
        )
        text = str(response.get("text") or "")
        action_key = BACKGROUND_ACK_TO_ACTION.get(text)
        if action_key is not None:
            # Slack times a command out after 3s, and these run for minutes.
            # Ack now, then finish the work (and DM the result) on the loop.
            task = asyncio.create_task(_run_background_scan(action_key, payload, pipeline))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        elif text == "leads_requested":
            leads = pipeline.db.recent_leads(25)
            response = {
                "response_type": "ephemeral",
                "text": "Recent leads",
                "blocks": format_leads_blocks(leads),
            }
        return JSONResponse(response)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        speedrun = "configured_a16z_official_api" if settings.yc_speedrun_url else "disabled"
        return {"status": "ok", "version": __version__, "speedrun": speedrun}

    @app.get("/manifest")
    async def get_manifest() -> dict[str, Any]:
        return manifest()

    def _pond_error(status_code: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(status_code=status_code, content={"code": code, "message": message})

    @app.post("/runs")
    async def runs(
        request: RunRequest,
        authorization: str | None = Header(default=None),
        x_agent_protocol_version: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> Any:
        if not settings.pond_access_key:
            return _pond_error(503, "temporarily_unavailable", "Access key is not configured")
        supplied = authorization.removeprefix("Bearer ") if authorization else ""
        if not hmac.compare_digest(supplied, settings.pond_access_key):
            return _pond_error(401, "unauthorized", "Invalid access key")
        if not x_agent_protocol_version:
            return _pond_error(400, "invalid_request", "Missing X-Agent-Protocol-Version header")
        if x_agent_protocol_version != "1.0":
            return _pond_error(400, "unsupported_protocol_version", "Unsupported protocol version")
        key = idempotency_key or request.run_id
        if key != request.run_id:
            return _pond_error(400, "invalid_request", "Idempotency-Key must equal run_id")
        cached = pipeline.db.get_pond_response(key)
        if cached:
            return cached
        action_id = request.action_id or "get_status"
        if action_id == "get_status":
            result: dict[str, Any] = pipeline.db.status()
            live_next = job_next_run_iso(scheduler)
            if live_next:
                result["next_run_at"] = live_next
        elif action_id == "run_monitor_cycle":
            merged = {**request.parameters, **request.input}
            result = await pipeline.run(bool(merged.get("dry_run", False)))
        elif action_id == "list_leads":
            merged = {**request.parameters, **request.input}
            limit = merged.get("limit", 10)
            result = {
                "leads": pipeline.db.recent_leads(int(limit) if isinstance(limit, int) else 10)
            }
        else:
            return _pond_error(400, "unsupported_operation", f"Unknown action_id {action_id}")
        response = {
            "run_id": request.run_id,
            "status": "completed",
            "output": [{"type": "text", "text": _summary(result)}],
            "result": result,
            "usage": {"unit_of_measurement": "result", "quantity": 1},
        }
        pipeline.db.save_pond_response(key, response)
        return response

    @app.get("/tasks/{task_id}")
    async def tasks(
        task_id: str,
        authorization: str | None = Header(default=None),
        x_agent_protocol_version: str | None = Header(default=None),
    ) -> Any:
        # This agent is sync-only (async_tasks: false) and never returns 202, so
        # no real tasks are ever created. Pond's reachability probe hits this path
        # with a synthetic `task_pond_reachability_probe_*` id; answer with a
        # terminal completed task so the route is treated as reachable.
        if not settings.pond_access_key:
            return _pond_error(503, "temporarily_unavailable", "Access key is not configured")
        supplied = authorization.removeprefix("Bearer ") if authorization else ""
        if not hmac.compare_digest(supplied, settings.pond_access_key):
            return _pond_error(401, "unauthorized", "Invalid access key")
        if not x_agent_protocol_version:
            return _pond_error(400, "invalid_request", "Missing X-Agent-Protocol-Version header")
        elif x_agent_protocol_version != "1.0":
            return _pond_error(400, "unsupported_protocol_version", "Unsupported protocol version")
        stored = pipeline.db.get_pond_task(task_id)
        if stored is not None:
            return stored
        if task_id.startswith("task_pond_reachability_probe"):
            return {
                "task_id": task_id,
                "status": "completed",
                "run_id": task_id,
                "result": {"status": "probe acknowledged"},
            }
        return _pond_error(404, "task_not_found", f"No task {task_id}")

    return app


def _summary(result: dict[str, Any]) -> str:
    if "alert_count" in result:
        return f"Monitor cycle completed with {result['alert_count']} new alert(s)."
    if "leads" in result:
        leads = result.get("leads")
        count = len(leads) if isinstance(leads, list) else 0
        return f"Returned {count} recent lead(s)."
    return "Monitor status retrieved successfully."


app = create_app()
