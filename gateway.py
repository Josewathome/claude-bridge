import asyncio
import contextlib
import hmac
import json
import os
import threading
import time
import uuid

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import claude_client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "gateway_state.json")
LOG_FILE = os.path.join(BASE_DIR, "logs", "gateway.jsonl")
GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY", "")
# Request log: logs/gateway.jsonl is the active log and never grows beyond LOG_MAX_BYTES. When the next line would not
# fit, the file is renamed (atomic, no copying) into logs/archive/ and a fresh active log starts. Archived files are
# deleted only once they are older than LOG_RETENTION_DAYS (0 = keep forever).
LOG_MAX_BYTES = int(float(os.environ.get("GATEWAY_LOG_MAX_MB", "6")) * 1024 * 1024)
LOG_RETENTION_DAYS = max(0.0, float(os.environ.get("GATEWAY_LOG_RETENTION_DAYS", "180")))
LOG_ARCHIVE_DIR = os.path.join(os.path.dirname(LOG_FILE), "archive")
LOG_CLEANUP_INTERVAL_SECONDS = 24 * 3600

if not GATEWAY_API_KEY:
    raise RuntimeError(
        "GATEWAY_API_KEY is not set. This gateway refuses to start without a caller "
        "auth key - see README.md. Set it in claude-bridge/.env and load it before "
        "starting uvicorn."
    )

app = FastAPI(title="claude-bridge (PoC)")

session_locks = claude_client.SessionLockRegistry()

# At most this many claude processes run at once, across all callers (memory, and the shared plan's rate limits).
# Each request is its own CLI process with its own new (or explicitly resumed) session, so concurrent requests never
# see each other's conversation; two requests for the SAME session_id still run one at a time (per-session lock).
# Waiters are served first come, first served. 1 = strictly one request at a time.
MAX_CONCURRENT = max(1, int(os.environ.get("BRIDGE_MAX_CONCURRENT", "2")))
_concurrency = asyncio.Semaphore(MAX_CONCURRENT)


_log_lock = threading.Lock()
_cleanup_running = threading.Lock()
_last_cleanup = 0.0


def _archive_active_log():
    """Move the active log into the archive folder. A rename on the same filesystem is atomic and instant, so no log
    line can be lost or half-copied. On any failure the active log is simply left in place and keeps being appended to."""
    try:
        os.makedirs(LOG_ARCHIVE_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        dest = os.path.join(LOG_ARCHIVE_DIR, f"gateway-{stamp}.jsonl")
        n = 0
        while os.path.exists(dest):
            n += 1
            dest = os.path.join(LOG_ARCHIVE_DIR, f"gateway-{stamp}-{n}.jsonl")
        os.rename(LOG_FILE, dest)
    except OSError:
        pass


def _cleanup_archive():
    """Delete archived logs whose last write is older than the retention period."""
    try:
        if LOG_RETENTION_DAYS > 0:
            cutoff = time.time() - LOG_RETENTION_DAYS * 86400
            for name in os.listdir(LOG_ARCHIVE_DIR):
                path = os.path.join(LOG_ARCHIVE_DIR, name)
                if name.startswith("gateway-") and name.endswith(".jsonl") and os.path.getmtime(path) < cutoff:
                    os.remove(path)
    except OSError:
        pass
    finally:
        _cleanup_running.release()


def _maybe_start_cleanup(force=False):
    """Run the retention cleanup in a background thread at most once per day (and once at startup), so it never
    delays a request."""
    global _last_cleanup
    now = time.monotonic()
    if not force and now - _last_cleanup < LOG_CLEANUP_INTERVAL_SECONDS:
        return
    if _cleanup_running.acquire(blocking=False):
        _last_cleanup = now
        threading.Thread(target=_cleanup_archive, daemon=True).start()


def log_event(**fields):
    fields.setdefault("timestamp", time.time())
    line = json.dumps(fields) + "\n"
    with _log_lock:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        try:
            size = os.path.getsize(LOG_FILE)
        except OSError:
            size = 0
        if size > 0 and size + len(line.encode("utf-8")) > LOG_MAX_BYTES:
            _archive_active_log()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    _maybe_start_cleanup()


_maybe_start_cleanup(force=True)


def read_state():
    if not os.path.isfile(STATE_FILE):
        return {"enabled": True}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"enabled": True}


def write_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def require_api_key(x_gateway_api_key):
    if not x_gateway_api_key or not hmac.compare_digest(x_gateway_api_key, GATEWAY_API_KEY):
        raise HTTPException(status_code=401, detail="missing or invalid X-Gateway-Api-Key")


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    timeout_seconds: int | None = None
    mcp_config: dict | None = None
    allowed_tools: list[str] | None = None
    json_schema: dict | None = None
    system_prompt: str | None = None
    # Optional model pin, passed to the CLI as --model. Absent = the CLI's own default (unchanged behaviour for every
    # caller that does not send it). Only plain model ids are accepted.
    model: str | None = None
    # Optional CLI --effort level (low, medium, high, xhigh, max). Absent = the CLI/model default (unchanged for callers that do not send it).
    effort: str | None = None
    # Deliver the message to the CLI on stdin instead of as a command-line argument (a single argument is limited to ~128 KiB by Linux).
    # None = automatic: stdin when the message is larger than 60,000 bytes, argument otherwise (unchanged for callers with small messages).
    via_stdin: bool | None = None


@app.get("/health")
def health():
    state = read_state()
    return {
        "status": "alive",
        "gateway_enabled": state.get("enabled", True),
        "claude_binary_found": claude_client.claude_binary_available(),
        "credentials_present": claude_client.credentials_present(),
        "max_concurrent": MAX_CONCURRENT,
    }


@app.get("/admin/status")
def admin_status(x_gateway_api_key: str = Header(default=None)):
    require_api_key(x_gateway_api_key)
    return read_state()


@app.post("/admin/enable")
def admin_enable(x_gateway_api_key: str = Header(default=None)):
    require_api_key(x_gateway_api_key)
    state = read_state()
    state["enabled"] = True
    write_state(state)
    log_event(event="gateway_enabled")
    return {"enabled": True}


@app.post("/admin/disable")
def admin_disable(x_gateway_api_key: str = Header(default=None)):
    require_api_key(x_gateway_api_key)
    state = read_state()
    state["enabled"] = False
    write_state(state)
    log_event(event="gateway_disabled")
    return {"enabled": False}


@app.get("/session/{session_id}")
def get_session(session_id: str, x_gateway_api_key: str = Header(default=None)):
    require_api_key(x_gateway_api_key)
    return {"session_id": session_id, "exists": claude_client.session_exists(session_id)}


@app.delete("/session/{session_id}")
async def delete_session(session_id: str, x_gateway_api_key: str = Header(default=None)):
    require_api_key(x_gateway_api_key)
    deleted = claude_client.delete_session_file(session_id)
    await session_locks.drop(session_id)
    log_event(event="session_deleted", session_id=session_id, existed=deleted)
    return {"session_id": session_id, "deleted": deleted}


@app.post("/chat")
async def chat(req: ChatRequest, x_gateway_api_key: str = Header(default=None)):
    request_id = str(uuid.uuid4())
    require_api_key(x_gateway_api_key)

    state = read_state()
    if not state.get("enabled", True):
        log_event(event="request_rejected_disabled", request_id=request_id)
        raise HTTPException(status_code=503, detail="gateway is disabled (kill switch is off)")

    log_event(event="request_received", request_id=request_id, session_id=req.session_id)

    # Session lock first, then a slot: a request waiting for its own session never holds a slot others could use.
    lock = await session_locks.get_lock(req.session_id) if req.session_id else contextlib.nullcontext()
    received = time.monotonic()
    async with lock, _concurrency:
        queue_ms = int((time.monotonic() - received) * 1000)
        try:
            timeout = req.timeout_seconds or claude_client.DEFAULT_TIMEOUT_SECONDS
            result = await claude_client.run_claude(
                req.message,
                session_id=req.session_id,
                timeout=timeout,
                mcp_config=req.mcp_config,
                allowed_tools=req.allowed_tools,
                json_schema=req.json_schema,
                system_prompt=req.system_prompt,
                model=req.model,
                effort=req.effort,
                via_stdin=req.via_stdin,
            )
        except claude_client.ClaudeCliError as e:
            log_event(
                event="request_failed",
                request_id=request_id,
                session_id=req.session_id,
                error_kind=e.kind,
                error=str(e),
            )
            status = 504 if e.kind == "timeout" else 502
            return JSONResponse(
                status_code=status,
                content={"success": False, "error": str(e), "error_kind": e.kind, "request_id": request_id},
            )

    usage = result.get("usage") or {}
    model_usage = result.get("modelUsage") or {}
    # Ground-truth tool-call list, read from the real session transcript -
    # see claude_client.extract_tool_calls's docstring. Purely additive: a
    # caller that doesn't read this new field sees no change in behavior.
    tools_called = claude_client.extract_tool_calls(result.get("session_id"))
    reasoning_text = claude_client.extract_reasoning_text(result.get("session_id"))

    # Phase 1 instrumentation: ground-truth check on whether --resume
    # actually resumed anything, rather than trusting the caller's own
    # belief about what it asked for. req.session_id is what THIS request
    # sent to the CLI (None on a genuinely fresh call, including a
    # fresh-fallback retry the caller made with session_id=None on purpose);
    # result.get("session_id") is what the CLI actually returned. Equal
    # means the resume genuinely continued that conversation; a caller that
    # requested a session_id but got a different one back would mean the
    # CLI silently started a new session instead of resuming - this field
    # is what would surface that, though it has not been observed to happen.
    requested_session_id = req.session_id
    returned_session_id = result.get("session_id")
    session_resumed = bool(requested_session_id) and requested_session_id == returned_session_id

    log_event(
        event="request_completed",
        request_id=request_id,
        session_id=returned_session_id,
        duration_ms=result.get("_duration_ms"),
        queue_ms=queue_ms,
        cost_usd=result.get("total_cost_usd"),
        num_turns=result.get("num_turns"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
        cache_read_input_tokens=usage.get("cache_read_input_tokens"),
        models_used=list(model_usage.keys()),
        model_usage=model_usage,
        tools_called=tools_called,
        session_resumed=session_resumed,
        reasoning_text_chars=len(reasoning_text) if reasoning_text else 0,
    )

    return {
        "success": True,
        "response": result.get("result"),
        "session_id": returned_session_id,
        "cost_usd": result.get("total_cost_usd"),
        "duration_ms": result.get("_duration_ms"),
        "num_turns": result.get("num_turns"),
        "tools_called": tools_called,
        "reasoning_text": reasoning_text,
        "request_id": request_id,
        # Phase 1 instrumentation - purely additive; every existing field
        # above is unchanged in name, value, or position.
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "model_usage": model_usage,
        "session_resumed": session_resumed,
    }
