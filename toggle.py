"""Flip the gateway kill switch without needing curl/the API key by hand.

Usage: ./toggle.py on|off|status
Talks to the running gateway over its admin endpoints using the key in .env.
"""
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


def call(path, method, api_key):
    url = f"http://{env.get('GATEWAY_HOST', '127.0.0.1')}:{env.get('GATEWAY_PORT', '8787')}{path}"
    req = urllib.request.Request(url, method=method, headers={"X-Gateway-Api-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "body": e.read().decode('utf-8', errors='replace')}
    except urllib.error.URLError as e:
        return {"error": f"could not reach gateway: {e}"}


if __name__ == "__main__":
    env = load_env()
    api_key = env.get("GATEWAY_API_KEY", "")
    action = sys.argv[1] if len(sys.argv) > 1 else "status"

    if action == "on":
        print(call("/admin/enable", "POST", api_key))
    elif action == "off":
        print(call("/admin/disable", "POST", api_key))
    else:
        print(call("/admin/status", "GET", api_key))
