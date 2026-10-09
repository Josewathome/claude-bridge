"""Summarize claude-bridge's gateway.jsonl: requests, cost, tokens, models used.

Usage: ./usage_summary.py [--since YYYY-MM-DD]
"""
import argparse
import glob
import json
import os
import re
import time

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "gateway.jsonl")


def iter_log_lines():
    """Lines from gateway.jsonl and its rotated copies (gateway.jsonl.1, .2, ...), oldest first."""
    rotated = [p for p in glob.glob(LOG_FILE + ".*") if re.fullmatch(r"\d+", p[len(LOG_FILE) + 1:])]
    rotated.sort(key=lambda p: int(p.rsplit(".", 1)[1]), reverse=True)
    for path in rotated + [LOG_FILE]:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                yield from f


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", help="only count requests on/after this UTC date, e.g. 2026-08-26")
    args = parser.parse_args()

    since_ts = None
    if args.since:
        since_ts = time.mktime(time.strptime(args.since, "%Y-%m-%d")) - time.timezone

    total_requests = 0
    failed_requests = 0
    total_cost = 0.0
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_creation = 0
    total_cache_read = 0
    models_seen = set()
    first_ts = None
    last_ts = None

    for line in iter_log_lines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue

        ts = d.get("timestamp")
        if since_ts and ts and ts < since_ts:
            continue

        event = d.get("event")
        if event == "request_completed":
            total_requests += 1
            total_cost += d.get("cost_usd") or 0
            total_input_tokens += d.get("input_tokens") or 0
            total_output_tokens += d.get("output_tokens") or 0
            total_cache_creation += d.get("cache_creation_input_tokens") or 0
            total_cache_read += d.get("cache_read_input_tokens") or 0
            for m in d.get("models_used") or []:
                models_seen.add(m)
            if ts:
                first_ts = ts if first_ts is None else min(first_ts, ts)
                last_ts = ts if last_ts is None else max(last_ts, ts)
        elif event == "request_failed":
            failed_requests += 1

    def fmt_ts(ts):
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts)) if ts else "n/a"

    print(f"Period:              {fmt_ts(first_ts)}  ->  {fmt_ts(last_ts)}")
    print(f"Completed requests:  {total_requests}")
    print(f"Failed requests:     {failed_requests}")
    print(f"Total cost:          ${total_cost:.4f}")
    print(f"Input tokens:        {total_input_tokens:,}")
    print(f"Output tokens:       {total_output_tokens:,}")
    print(f"Cache creation:      {total_cache_creation:,}")
    print(f"Cache read:          {total_cache_read:,}")
    print(f"Models used:         {', '.join(sorted(models_seen)) or 'none'}")
    if total_requests:
        print(f"Avg cost/request:    ${total_cost / total_requests:.4f}")


if __name__ == "__main__":
    main()
