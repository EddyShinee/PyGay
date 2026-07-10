"""Cron entry point: calls POST /api/{account_id}/history/fetch for a
configured list of (account_id, symbol, timeframe, count) and appends new
bars to CSV under python/history/.

python/main.py must already be running (with that account's EA connected)
- this script just triggers the fetch over HTTP, it does not talk to MT5
itself.

Add to crontab to run hourly:

    0 * * * * cd /path/to/PyGay/python && .venv/bin/python3 tools/fetch_history_cron.py >> /tmp/fetch_history.log 2>&1

Edit JOBS below to add/remove what gets collected (one entry per
account+symbol+timeframe you want to track). Uses only the stdlib
(urllib) so it runs with any python3, no venv/install required for cron.
"""
import json
import sys
import urllib.request

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT_S = 35

# (account_id, symbol, timeframe, count) - count should comfortably cover
# more than the gap between cron runs so a missed run doesn't leave a hole
# (e.g. 1000 M1 bars = ~16h of history, way more than the 1h cron interval).
JOBS = [
    ("1001", "EURUSD", "M1", 1000),
    ("1001", "EURUSD", "H1", 500),
    # ("1002", "GBPUSD", "M1", 1000),
]


def fetch(account_id: str, symbol: str, timeframe: str, count: int) -> None:
    body = json.dumps({"symbol": symbol, "timeframe": timeframe, "count": count}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/api/{account_id}/history/fetch",
        data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        result = json.loads(resp.read())
        print(f"[{account_id}] {symbol} {timeframe}: fetched={result['fetched']} saved={result['saved']}")


def main() -> None:
    had_error = False
    for account_id, symbol, timeframe, count in JOBS:
        try:
            fetch(account_id, symbol, timeframe, count)
        except Exception as exc:
            had_error = True
            print(f"[{account_id}] {symbol} {timeframe}: FAILED - {exc}", file=sys.stderr)
    sys.exit(1 if had_error else 0)


if __name__ == "__main__":
    main()
