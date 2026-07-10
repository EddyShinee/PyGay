"""Appends fetched OHLC bars to a per-account/symbol/timeframe CSV file,
skipping bars already saved (by comparing against the last saved bar's
time) so an hourly cron job can safely re-fetch overlapping ranges without
creating duplicate rows.

Namespaced by account_id (not just symbol/timeframe) because different
accounts can be on different brokers with different price feeds for what
is nominally "the same" symbol - keeping them in separate files avoids
ever mixing two feeds into one series.
"""
import re
from pathlib import Path

HISTORY_DIR = Path(__file__).parent / "history"
FIELDNAMES = ["time", "open", "high", "low", "close", "tick_volume", "spread"]
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def csv_path(account_id: str, symbol: str, timeframe: str) -> Path:
    HISTORY_DIR.mkdir(exist_ok=True)
    safe_account = _UNSAFE_CHARS.sub("_", account_id)
    return HISTORY_DIR / f"{safe_account}_{symbol}_{timeframe}.csv"


def last_saved_time(path: Path) -> int:
    """Time of the last row, read from the tail of the file so this stays
    fast even once the CSV has grown to hundreds of thousands of rows."""
    if not path.exists():
        return 0
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        block = min(size, 4096)
        f.seek(-block, 2)
        tail = f.read().decode(errors="ignore")
    lines = [line for line in tail.splitlines() if line.strip()]
    if not lines:
        return 0
    last_line = lines[-1]
    if last_line.startswith("time,"):  # tail happened to only catch the header
        return 0
    return int(last_line.split(",")[0])


def append_bars(account_id: str, symbol: str, timeframe: str, bars: list[dict]) -> int:
    """Append only bars newer than what's already saved. Returns how many
    new rows were written."""
    path = csv_path(account_id, symbol, timeframe)
    last = last_saved_time(path)
    new_bars = sorted((b for b in bars if int(b["time"]) > last), key=lambda b: b["time"])
    if not new_bars:
        return 0

    write_header = not path.exists()
    with path.open("a") as f:
        if write_header:
            f.write(",".join(FIELDNAMES) + "\n")
        for b in new_bars:
            f.write(",".join(str(b[field]) for field in FIELDNAMES) + "\n")
    return len(new_bars)
