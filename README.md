# Claude Bridge

Claude Bridge is a small HTTP gateway (FastAPI) that exposes a locally installed, already-logged-in
[Claude Code](https://docs.claude.com/en/docs/claude-code) CLI as a simple JSON API. Other programs on your
network POST a message to `/chat` and get Claude's answer back, along with cost/token usage, the tools it called,
and a `session_id` they can send later to continue the same conversation.

> **Status: proof of concept.** The service identifies itself as "claude-bridge (PoC)". It is built for a single
> trusted operator on a private network, not for multi-tenant or internet-facing use. Read
> [Security considerations](#security-considerations) before exposing it beyond localhost.

## Why we built it

We wanted automation scripts and services to be able to use Claude **without calling the Anthropic API and without
building a whole agent/automation stack from scratch**. Claude Code already provides what we needed:

- an authenticated login (the CLI's own OAuth credentials) instead of API-key plumbing in every client;
- persistent conversations that can be resumed by ID (`--resume`), so a client can keep context across calls;
- MCP server support, structured (JSON-schema) output, model and effort selection, and system prompts;
- a transcript of everything the model did on disk, which we read back to report which tools were actually called.

Claude Bridge is the thin layer that turns that into something any script can call: one authenticated HTTP endpoint,
per-session serialization, a kill switch, and usage logging. It deliberately contains no model logic of its own; it
shells out to the `claude` binary.

## How it works

```
  your scripts / services
          |  HTTP + X-Gateway-Api-Key
          v
  +-------------------------------+
  | gateway.py  (FastAPI/uvicorn) |   auth, kill switch, per-session lock,
  |                               |   request logging -> logs/gateway.jsonl
  +---------------+---------------+
                  |  claude_client.run_claude()
                  v
  +-------------------------------+
  | claude CLI  (~/.local/bin)    |   claude -p --output-format json
  |  cwd = cli_workdir/           |   [--resume <id>] [--model ...] ...
  +---------------+---------------+
                  |  uses your existing Claude login (~/.claude/.credentials.json)
                  v
              Anthropic
```

Components:

| File | Role |
|---|---|
| `gateway.py` | FastAPI app. Endpoints, API-key check, kill switch, per-request logging, response shaping. |
| `claude_client.py` | Builds and runs the `claude` subprocess, enforces timeouts, validates `model`/`effort`, reads session transcripts from `~/.claude/projects/` to extract tool calls and reasoning text, deletes sessions, holds the per-session lock registry. |
| `run.sh` | Loads `.env` and starts uvicorn with `GATEWAY_HOST`/`GATEWAY_PORT`. |
| `toggle.py` | CLI to turn the kill switch on/off/status using the key from `.env`. |
| `usage_summary.py` | Summarizes `logs/gateway.jsonl` (requests, failures, cost, tokens, models). |
| `test_client.py` | End-to-end smoke test against a running gateway (**makes real Claude calls**, see below). |
| `deploy/claude-bridge.service.example` | Template systemd user unit. |
| `.env.example` | Configuration template. |
| `cli_workdir/` | Working directory the `claude` subprocess runs in (must exist; kept in Git via `.gitkeep`). |

### Execution flow of `POST /chat`

1. The `X-Gateway-Api-Key` header is compared (constant-time) with `GATEWAY_API_KEY`; mismatch gives `401`.
2. If the kill switch is off (`gateway_state.json` has `"enabled": false`), the request gets `503`.
3. A lock is taken for the request's `session_id` so two requests for the same session never run concurrently.
   Requests without a `session_id` share a single lock (see [Limitations](#limitations)).
4. `claude_client.run_claude()` launches:
   `claude --tools "" --strict-mcp-config -p --output-format json` plus, depending on the request,
   `--safe-mode` (only when no `mcp_config` is given), `--resume <session_id>`, `--mcp-config <json>
   --dangerously-skip-permissions`, `--allowedTools`, `--json-schema`, `--system-prompt`, `--model`, `--effort`.
   The message is passed as the final argument, or on stdin when it is larger than 60,000 bytes (or when
   `via_stdin` is true).
5. Built-in Claude Code tools are disabled (`--tools ""`) and ambient MCP servers are ignored
   (`--strict-mcp-config`); only MCP servers the caller passes in `mcp_config` are available.
6. The process is killed if it exceeds `timeout_seconds` (default 120 s) and the caller gets `504`.
   Non-zero exit, non-JSON output, or an `is_error` result give `502`.
7. On success, the gateway reads the session's transcript file to list the tools actually invoked and the text the
   model wrote during this turn, appends a `request_completed` line to `logs/gateway.jsonl`, and returns the result.

## Features

All of these are implemented in the code:

- **`POST /chat`** with optional `session_id` (resume), `timeout_seconds`, `mcp_config`, `allowed_tools`,
  `json_schema`, `system_prompt`, `model`, `effort`, `via_stdin`.
- **Session continuity**: pass back the returned `session_id` to continue a conversation. `session_resumed` in the
  response tells you whether the CLI really continued that session.
- **Session management**: `GET /session/{id}` (exists?) and `DELETE /session/{id}` (removes the CLI's transcript file).
- **Ground-truth telemetry**: `tools_called` and `reasoning_text` are read from the CLI's own transcript for the
  current turn, not inferred. Both are best-effort and may be `null`.
- **Usage reporting**: cost (USD), duration, turns, input/output/cache tokens, and per-model usage in every response
  and in the log.
- **Kill switch**: `/admin/enable`, `/admin/disable`, `/admin/status`, and `toggle.py`. State survives restarts.
- **Per-session serialization** via asyncio locks.
- **Input validation** on `model` (must look like `claude-...`) and `effort` (`low|medium|high|xhigh|max`).
- **Fail-closed auth**: the process refuses to start if `GATEWAY_API_KEY` is empty.
- **Health endpoint** reporting whether the `claude` binary and stored credentials are present.
- **Log rotation**: `logs/gateway.jsonl` rotates by size and keeps a fixed number of backups, so disk use is bounded
  (default about 60 MB).
- **Usage summary script** over the JSONL log, including its rotated files.

## Technology stack

- Python 3, [FastAPI](https://fastapi.tiangolo.com/) 0.115.0, [uvicorn](https://www.uvicorn.org/) 0.30.6,
  [pydantic](https://docs.pydantic.dev/) 2.9.2 (pinned in `requirements.txt`); everything else is the standard library.
- Bash (`run.sh`).
- The Claude Code CLI as the execution engine.
- Optional: systemd (user unit) for running as a service.

## Requirements

- **OS**: Linux. Developed and run on Debian Linux (kernel 6.12). `run.sh` is Bash and the CLI is expected at a
  POSIX path; Windows is not supported. macOS is untested.
- **Python**: 3.10 or newer (the code uses `str | None` annotations). Tested on Python 3.13.5.
- **Claude Code CLI**: installed at `~/.local/bin/claude` (the path is hard-coded as `CLAUDE_BIN` in
  `claude_client.py`) and **logged in**, so that `~/.claude/.credentials.json` exists. Tested with version 2.1.295.
  The CLI flags used must be available in your version: `--tools`, `--strict-mcp-config`, `--safe-mode`,
  `--resume`, `--mcp-config`, `--dangerously-skip-permissions`, `--allowedTools`, `--json-schema`,
  `--system-prompt`, `--model`, `--effort`, `--output-format json`.
- A Claude plan/login that allows using the CLI non-interactively. Check that your intended use is permitted by
  the terms of your plan.
- Network access from the machine to Anthropic.

## Installation and setup

```bash
# 1. Install Claude Code and log in (follow Anthropic's instructions), then confirm:
~/.local/bin/claude --version

# 2. Get the code
git clone <your-repo-url> claude-bridge
cd claude-bridge

# 3. Create the virtualenv and install dependencies
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 4. Create your local config
cp .env.example .env
chmod 600 .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # paste the output as GATEWAY_API_KEY in .env
```

## Configuration

Configuration is read from environment variables, normally supplied through `.env` (see `.env.example`).

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `GATEWAY_API_KEY` | **yes** | none | Shared secret clients send in the `X-Gateway-Api-Key` header. The gateway will not start if it is empty. |
| `GATEWAY_HOST` | no | `127.0.0.1` | Interface uvicorn binds to (also used by `toggle.py` / `test_client.py` to find the gateway). |
| `GATEWAY_PORT` | no | `8787` | TCP port. |
| `BRIDGE_DEFAULT_MODEL` | no | `claude-sonnet-5` | Model used when a request has no `model`. |
| `GATEWAY_LOG_MAX_MB` | no | `10` | Size at which `logs/gateway.jsonl` is rotated. |
| `GATEWAY_LOG_BACKUPS` | no | `5` | Rotated files kept (`gateway.jsonl.1` ... `.N`, minimum 1); older ones are deleted. |

Format notes: no quotes and no inline comments in `.env`. `run.sh` sources the file, but `toggle.py` and
`test_client.py` parse it by hand and would keep quote characters in the value.

Fixed in code (edit `claude_client.py` to change): `CLAUDE_BIN` (`~/.local/bin/claude`), default request timeout
(120 s), stdin threshold (60,000 bytes), CLI working directory (`cli_workdir/`).

## Running

Foreground:

```bash
./run.sh
```

`run.sh` exits with an error if `venv/` is missing, loads `.env`, and runs
`uvicorn gateway:app --host $GATEWAY_HOST --port $GATEWAY_PORT`.

As a systemd user service: copy `deploy/claude-bridge.service.example` to `~/.config/systemd/user/claude-bridge.service`,
replace the `/path/to/claude-bridge` paths (and `--host`/`--port` if needed), then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now claude-bridge
systemctl --user status claude-bridge
```

### Using the API

```bash
set -a; source .env; set +a      # loads GATEWAY_API_KEY, GATEWAY_HOST, GATEWAY_PORT
BASE="http://${GATEWAY_HOST:-127.0.0.1}:${GATEWAY_PORT:-8787}"

# Health (no key needed)
curl -s "$BASE/health"

# Start a conversation
curl -s "$BASE/chat" \
  -H "X-Gateway-Api-Key: $GATEWAY_API_KEY" -H "Content-Type: application/json" \
  -d '{"message": "Remember the number 42. Reply with OK."}'

# Continue it using the session_id from the response
curl -s "$BASE/chat" \
  -H "X-Gateway-Api-Key: $GATEWAY_API_KEY" -H "Content-Type: application/json" \
  -d '{"message": "What number did I ask you to remember?", "session_id": "<session_id>"}'
```

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /health` | none | `status`, `gateway_enabled`, `claude_binary_found`, `credentials_present` |
| `POST /chat` | key | Run a prompt (see fields below) |
| `GET /session/{id}` | key | `{"exists": true/false}` |
| `DELETE /session/{id}` | key | Delete the session transcript |
| `GET /admin/status` | key | Kill-switch state |
| `POST /admin/enable` / `POST /admin/disable` | key | Turn the kill switch on / off |

`POST /chat` body:

| Field | Type | Notes |
|---|---|---|
| `message` | string, required | The prompt. |
| `session_id` | string | Resume this session. Omit to start a new one. |
| `timeout_seconds` | int | Default 120. On expiry the process is killed and the response is `504`. |
| `model` | string | e.g. `claude-sonnet-5`. Must match `claude-...`. Default: `BRIDGE_DEFAULT_MODEL`. |
| `effort` | string | `low`, `medium`, `high`, `xhigh`, `max`. |
| `system_prompt` | string | Replaces the default system prompt. |
| `json_schema` | object | Passed to `--json-schema` for structured output. |
| `mcp_config` | object | MCP server configuration passed to `--mcp-config`. **Powerful; see Security.** |
| `allowed_tools` | list of strings | Passed to `--allowedTools`. |
| `via_stdin` | bool | Force stdin delivery. Default: automatic above 60,000 bytes. |

Successful responses contain `success`, `response`, `session_id`, `cost_usd`, `duration_ms`, `num_turns`,
`tools_called`, `reasoning_text`, `request_id`, `input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens`, `model_usage`, `session_resumed`. Failures return `{"success": false, "error", "error_kind",
"request_id"}` with `error_kind` one of `cli_missing`, `bad_model`, `bad_effort`, `timeout`, `process_error`,
`malformed_output`, `claude_error`.

### Operations

```bash
./toggle.py status     # show kill switch state (also the default with no argument)
./toggle.py off        # reject all /chat calls with 503
./toggle.py on         # accept them again
./usage_summary.py                       # totals from logs/gateway.jsonl and its rotated files
./usage_summary.py --since 2026-08-26    # only on/after a UTC date
```

## Verification and troubleshooting

Quick check after starting:

```bash
curl -s "http://127.0.0.1:8787/health"
# expect: {"status":"alive","gateway_enabled":true,"claude_binary_found":true,"credentials_present":true}
```

Full smoke test (`./test_client.py`, exits non-zero on failure). **Run it only against a gateway you are happy to
test:** it makes several real Claude requests (which use your plan's quota), creates and deletes a session, and
briefly disables and re-enables the kill switch, so it interrupts any other client using that gateway.

| Symptom | Cause / fix |
|---|---|
| `RuntimeError: GATEWAY_API_KEY is not set` on start | Fill in `GATEWAY_API_KEY` in `.env`, and start via `run.sh` (or systemd with `EnvironmentFile`) so it is loaded. |
| `run.sh`: "venv missing" | `python3 -m venv venv && ./venv/bin/pip install -r requirements.txt` |
| `claude_binary_found: false` / `cli_missing` | The CLI is not at `~/.local/bin/claude`. Install it there, symlink it there, or change `CLAUDE_BIN`. |
| `credentials_present: false` | Run `claude` once interactively and log in as the same OS user that runs the gateway. |
| `401` | Missing/wrong `X-Gateway-Api-Key`. |
| `503 gateway is disabled` | Kill switch is off: `./toggle.py on`. |
| `504` / `error_kind: timeout` | Raise `timeout_seconds` in the request. |
| `502` with `process_error` | The CLI failed; the error text includes the first 500 characters of its stderr. Try the same flags by hand. A rejected flag usually means an older/newer CLI version. |
| `address already in use` | Another process (possibly an already-running gateway) holds the port; change `GATEWAY_PORT` or stop it. |
| `toggle.py` / `test_client.py`: "could not reach gateway" | Gateway not running, or `GATEWAY_HOST`/`GATEWAY_PORT` in `.env` do not match where it is listening. |

## Security considerations

- **The API key is effectively shell access to the gateway user's account.** A caller holding it can send an
  arbitrary `mcp_config`; when one is supplied the CLI runs with `--dangerously-skip-permissions`, and an MCP server
  entry can launch any local command. Treat `GATEWAY_API_KEY` like an SSH key: long and random, unique, shared only
  with trusted callers, rotated if exposed. Run the gateway as a dedicated, unprivileged user if you can.
- **Never commit or share** `.env`, `~/.claude/.credentials.json`, anything under `logs/`, or `gateway_state.json`.
  `.gitignore` excludes the first, third and fourth; the credentials file lives in your home directory, outside this
  repository. Keep `.env` at mode `600`.
- **Network exposure**: traffic is plain HTTP with a static key. Default to `GATEWAY_HOST=127.0.0.1`. If other
  hosts or containers must connect, bind to the specific private interface address (not `0.0.0.0` on a machine with
  a public interface), restrict the port with a firewall, and use an SSH tunnel or a TLS reverse proxy for anything
  crossing an untrusted network. Do not publish your server's IP addresses or hostnames in issues, logs, or docs.
- **`/health` requires no key** and reveals whether the CLI binary and credentials are present.
- **Logs** (`logs/gateway.jsonl` and its rotated copies) do not store prompts or responses, but do record session IDs, costs, token counts,
  tool names, and error text (which can quote CLI output). Uvicorn logs include client IP addresses. Redact before
  sharing.
- **Session transcripts** written by the CLI under `~/.claude/projects/` do contain full conversations. `DELETE
  /session/{id}` removes one; the `session_id` is used in a file-glob lookup, so only give the key to callers you trust.
- If you ever commit a secret by mistake, deleting it in a later commit is not enough: it remains in history. Rotate
  the secret immediately and rewrite history (for example with `git filter-repo`) before pushing.

## Limitations

- Log rotation drops the oldest data: usage totals only cover what is still on disk (raise `GATEWAY_LOG_BACKUPS`
  or archive `gateway.jsonl.N` files yourself if you need longer history).
- Proof of concept: no TLS, no per-client keys, no rate limiting, no request queue limits, single process.
- All requests that omit `session_id` share one lock, so they run **one at a time**; concurrency exists only across
  different session IDs.
- Linux-only as written; the CLI path is hard-coded to `~/.local/bin/claude`.
- Depends on an external, evolving CLI: behaviour of flags such as `--safe-mode` and `--resume` (and the transcript
  format used for `tools_called` / `reasoning_text`) can change between Claude Code versions. Tested with 2.1.295.
- Uses your logged-in Claude plan, so usage counts against its limits and the gateway stops working if the login
  expires. `reasoning_text` only contains text the model emits; extended-thinking content was not populated in our
  setup.
- Built-in Claude Code tools are disabled by design; tool use is only possible through MCP servers you pass in.
- Sessions are only resumable on the machine/user account where the transcript exists.
- `test_client.py` and `usage_summary.py` are simple scripts: no CLI flags beyond those documented, and no tests
  that run without a live gateway and CLI.

## License

Claude Bridge is released under the [PolyForm Noncommercial License 1.0.0](LICENSE)
(`PolyForm-Noncommercial-1.0.0`). In short: you may use, modify and share it for noncommercial purposes
(personal use, research, education, hobby projects, and certain noncommercial organizations, as defined in the
license). **Commercial use requires separate permission**: contact the repository owner through their GitHub
profile or open an issue to request a commercial license.

This is a source-available license, not an OSI-approved open-source license. The software is provided as is, without
warranty; see the license text for the full terms.
