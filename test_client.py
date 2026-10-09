import json
import os
import sys
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_env():
    env = {}
    with open(os.path.join(BASE_DIR, ".env"), "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k] = v
    return env


ENV = load_env()
BASE_URL = f"http://{ENV.get('GATEWAY_HOST', '127.0.0.1')}:{ENV.get('GATEWAY_PORT', '8787')}"
API_KEY = ENV.get("GATEWAY_API_KEY", "")

PASS = "PASS"
FAIL = "FAIL"
results = []


def record(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" - {detail}" if detail else ""))


def call(path, method="GET", body=None, api_key=API_KEY, timeout=60):
    url = BASE_URL + path
    headers = {"Content-Type": "application/json"}
    if api_key is not None:
        headers["X-Gateway-Api-Key"] = api_key
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def main():
    status, body = call("/health")
    record("health check reachable", status == 200, json.dumps(body))
    record("health reports claude binary found", body.get("claude_binary_found") is True)
    record("health reports credentials present", body.get("credentials_present") is True)

    status, body = call("/chat", "POST", {"message": "reply with exactly: PING"}, api_key="wrong-key")
    record("rejects bad api key", status == 401, f"status={status}")

    status, body = call("/chat", "POST", {"message": "reply with exactly: PING"}, api_key=None)
    record("rejects missing api key", status == 401, f"status={status}")

    status, body = call("/chat", "POST", {"message": "Remember this number: 918273. Reply with exactly: OK"})
    record("first message succeeds", status == 200 and body.get("success"), json.dumps(body))
    session_id = body.get("session_id")
    record("session_id returned", bool(session_id), session_id)

    status, body = call(
        "/chat", "POST", {"message": "What number did I ask you to remember? Reply with just the number.", "session_id": session_id}
    )
    record("second message succeeds", status == 200 and body.get("success"), json.dumps(body))
    record("second message recalls context", body.get("response", "").strip() == "918273", body.get("response"))

    status, body = call(f"/session/{session_id}")
    record("session exists before delete", status == 200 and body.get("exists") is True)

    status, body = call(f"/session/{session_id}", "DELETE")
    record("session delete succeeds", status == 200 and body.get("deleted") is True)

    status, body = call(f"/session/{session_id}")
    record("session gone after delete", status == 200 and body.get("exists") is False)

    status, body = call(
        "/chat",
        "POST",
        {"message": "What number did I ask you to remember earlier? If you don't know, say UNKNOWN.", "session_id": session_id},
    )
    record(
        "resuming a deleted session does not recall old context",
        body.get("response", "").strip().upper() != "918273",
        body.get("response"),
    )

    status, body = call("/admin/disable", "POST")
    record("admin disable succeeds", status == 200 and body.get("enabled") is False)

    status, body = call("/chat", "POST", {"message": "reply with exactly: SHOULD_NOT_RUN"})
    record("chat rejected while disabled", status == 503, f"status={status}")

    status, body = call("/admin/enable", "POST")
    record("admin enable succeeds", status == 200 and body.get("enabled") is True)

    status, body = call("/chat", "POST", {"message": "reply with exactly: BACK_ON"})
    record("chat works again after re-enable", status == 200 and body.get("success"), json.dumps(body))

    print()
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} passed")
    if passed != len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
