import asyncio
import re
import glob
import json
import os
import time

CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")
# Used when a request carries no "model". Override with BRIDGE_DEFAULT_MODEL in the service environment.
DEFAULT_MODEL = os.environ.get("BRIDGE_DEFAULT_MODEL", "claude-sonnet-5")
STDIN_THRESHOLD_BYTES = 60_000   # larger messages go to the CLI on stdin (one command-line argument is limited to ~128 KiB)
WORKDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cli_workdir")
DEFAULT_TIMEOUT_SECONDS = 120


class ClaudeCliError(Exception):
    def __init__(self, message, kind):
        super().__init__(message)
        self.kind = kind


def claude_binary_available():
    return os.path.isfile(CLAUDE_BIN) and os.access(CLAUDE_BIN, os.X_OK)


def credentials_present():
    cred_path = os.path.expanduser("~/.claude/.credentials.json")
    if not os.path.isfile(cred_path):
        return False
    try:
        with open(cred_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return "claudeAiOauth" in data or "accessToken" in json.dumps(data)
    except (json.JSONDecodeError, OSError):
        return False


def session_file_path(session_id):
    projects_root = os.path.expanduser("~/.claude/projects")
    matches = glob.glob(os.path.join(projects_root, "*", f"{session_id}.jsonl"))
    if matches:
        return matches[0]
    return os.path.join(projects_root, "__no_such_project__", f"{session_id}.jsonl")


def delete_session_file(session_id):
    path = session_file_path(session_id)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def session_exists(session_id):
    return os.path.isfile(session_file_path(session_id))


def extract_tool_calls(session_id):
    """Every tool the model actually invoked during THIS turn (since the
    most recent genuine user prompt), in call order, read from the real
    session transcript the `claude` CLI writes to disk regardless of
    --output-format - this is ground truth (the CLI's own internal log of
    what it did), not an inference from turn count or the final answer
    text. Confirmed live: --output-format json's summary (num_turns, cost,
    result) never included which tools ran, so a caller reading only that
    summary had no way to know if a tool it expected to matter (e.g. an
    "optional" evidence tool) was ever actually called that turn versus
    just mentioned in a hand-written prompt instruction.

    Deliberately scoped to the current turn, not the whole session: with
    --resume now a real, supported usage pattern (a session can span many
    separate /chat calls), the full transcript accumulates every tool call
    from every PAST turn too. Confirmed live: a second, resumed call that
    made exactly one new tool call had this function return TWO entries
    (the new one plus the first turn's) before this fix - a caller using
    this to answer "did THIS analysis actually investigate anything" would
    have been satisfied by investigation from calls ago, not this one. The
    boundary is the most recent transcript line whose message content is a
    plain string (a genuine new user-authored prompt) rather than a list
    (a tool_result block, which also has type=="user" in this format) -
    only tool_use blocks after that point count.

    'StructuredOutput' (the mechanism used to emit the final JSON-schema
    answer, not a real MCP tool) is excluded. Best-effort: returns None on
    any failure (missing file, unparseable line) rather than raising - this
    is purely additive telemetry and must never block a real chat response."""
    if not session_id:
        return None
    path = session_file_path(session_id)
    if not os.path.isfile(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
    except OSError:
        return None

    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            parsed.append(None)

    # Walk backward for the most recent genuine user turn (plain-string
    # content) - everything from there onward belongs to the current turn.
    turn_start = 0
    for i in range(len(parsed) - 1, -1, -1):
        obj = parsed[i]
        if not obj or obj.get("type") != "user":
            continue
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, str):
            turn_start = i
            break

    tool_calls = []
    for obj in parsed[turn_start:]:
        if not obj:
            continue
        content = (obj.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                if name and name != "StructuredOutput":
                    tool_calls.append(name)

    return tool_calls


def extract_reasoning_text(session_id):
    """All plain-text content the model wrote during THIS turn (since the
    most recent genuine user prompt), concatenated in order - the model's
    actual reasoning, now that the system prompt asks for it before the
    final verdict, rather than the tool-call-only output it was previously
    instructed to produce. Same turn-boundary logic as extract_tool_calls
    (walk back to the most recent transcript line whose user message content
    is a plain string). Returns None on any failure, for the same reason
    extract_tool_calls does: purely additive telemetry, must never block a
    real chat response."""
    if not session_id:
        return None
    path = session_file_path(session_id)
    if not os.path.isfile(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
    except OSError:
        return None

    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            parsed.append(None)

    turn_start = 0
    for i in range(len(parsed) - 1, -1, -1):
        obj = parsed[i]
        if not obj or obj.get("type") != "user":
            continue
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, str):
            turn_start = i
            break

    texts = []
    for obj in parsed[turn_start:]:
        if not obj:
            continue
        content = (obj.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                texts.append(block["text"])
            elif block.get("type") == "thinking" and block.get("thinking"):
                # Not currently populated by this deployment (confirmed live,
                # 2026-09-24: thinking_tokens=0 even at --effort high, and
                # --betas is API-key-only, unavailable under this OAuth-based
                # setup) - captured anyway so this starts working for free if
                # that ever changes, without another investigation.
                texts.append(block["thinking"])

    return "\n".join(texts) if texts else None


class SessionLockRegistry:
    def __init__(self):
        self._locks = {}
        self._registry_lock = asyncio.Lock()

    async def get_lock(self, session_id):
        key = session_id or "__no_session__"
        async with self._registry_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    async def drop(self, session_id):
        async with self._registry_lock:
            self._locks.pop(session_id, None)


async def run_claude(
    message,
    session_id=None,
    timeout=DEFAULT_TIMEOUT_SECONDS,
    mcp_config=None,
    allowed_tools=None,
    json_schema=None,
    system_prompt=None,
    model=None,
    effort=None,
    via_stdin=None,
):
    if not claude_binary_available():
        raise ClaudeCliError(f"claude binary not found or not executable at {CLAUDE_BIN}", "cli_missing")

    # --safe-mode blocks MCP servers unconditionally, even ones explicitly
    # passed via --mcp-config (confirmed empirically - not documented as such)
    # so it's only safe to apply when no mcp_config is given. --strict-mcp-config
    # (kept always) already keeps ambient/global MCP servers out regardless.
    # When mcp_config IS given, callers always also pass their own system_prompt,
    # which replaces the default prompt entirely and empirically prevents the
    # auto-memory contamination --safe-mode was otherwise guarding against.
    args = [CLAUDE_BIN, "--tools", "", "--strict-mcp-config", "-p", "--output-format", "json"]
    if not mcp_config:
        args.append("--safe-mode")
    if session_id:
        args += ["--resume", session_id]
    if mcp_config:
        args += ["--mcp-config", json.dumps(mcp_config), "--dangerously-skip-permissions"]
    if allowed_tools:
        args += ["--allowedTools", ",".join(allowed_tools)]
    if json_schema:
        args += ["--json-schema", json.dumps(json_schema)]
    if system_prompt:
        args += ["--system-prompt", system_prompt]
    model = model or DEFAULT_MODEL      # callers that do not name a model get Sonnet 5, not the CLI's (changeable) default
    if model:
        if not re.fullmatch(r"claude-[a-z0-9.\-]+(\[[0-9a-z]+\])?", model):
            raise ClaudeCliError(f"refusing model id {model!r}", "bad_model")
        args += ["--model", model]
    if effort:
        if effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ClaudeCliError(f"refusing effort {effort!r}", "bad_effort")
        args += ["--effort", effort]
    message_bytes = message.encode("utf-8")
    use_stdin = via_stdin if via_stdin is not None else len(message_bytes) > STDIN_THRESHOLD_BYTES
    if not use_stdin:
        args.append(message)

    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=WORKDIR,
        stdin=asyncio.subprocess.PIPE if use_stdin else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=message_bytes if use_stdin else None), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ClaudeCliError(f"claude process exceeded {timeout}s timeout and was killed", "timeout")

    duration_ms = int((time.monotonic() - start) * 1000)
    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        raise ClaudeCliError(
            f"claude exited with code {proc.returncode}: {stderr_text[:500]}",
            "process_error",
        )

    try:
        parsed = json.loads(stdout_text)
    except json.JSONDecodeError as e:
        raise ClaudeCliError(f"claude returned non-JSON output: {e}. raw={stdout_text[:500]}", "malformed_output")

    if parsed.get("is_error"):
        raise ClaudeCliError(f"claude reported an error: {parsed.get('result')}", "claude_error")

    parsed["_duration_ms"] = duration_ms
    return parsed
