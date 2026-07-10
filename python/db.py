"""SQLite storage for closed deals - the one piece of state that must
survive a Python restart (open positions/account don't need to; the EA
re-sends those). Kept as plain sqlite3/SQL, no ORM - small surface, easy
to read and change.
"""
import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "trades.db"

BUCKET_FORMATS = {
    "minute": "%Y-%m-%d %H:%M",
    "hour": "%Y-%m-%d %H:00",
    "day": "%Y-%m-%d",
    "month": "%Y-%m",
    "year": "%Y",
}


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    conn = get_connection(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deals (
            ticket INTEGER PRIMARY KEY,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            volume REAL NOT NULL,
            price_open REAL NOT NULL,
            price_close REAL NOT NULL,
            profit REAL NOT NULL,
            swap REAL NOT NULL,
            commission REAL NOT NULL,
            time_open INTEGER NOT NULL,
            time_close INTEGER NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def upsert_deal(deal: dict, db_path: Path = DB_PATH) -> None:
    """Insert a closed deal, or replace it if the ticket (=position id)
    was already recorded (e.g. a later partial close updates the totals,
    or the EA resent it after a reconnect)."""
    conn = get_connection(db_path)
    conn.execute("""
        INSERT INTO deals (ticket, symbol, side, volume, price_open, price_close,
                            profit, swap, commission, time_open, time_close)
        VALUES (:ticket, :symbol, :side, :volume, :price_open, :price_close,
                :profit, :swap, :commission, :time_open, :time_close)
        ON CONFLICT(ticket) DO UPDATE SET
            symbol=excluded.symbol, side=excluded.side, volume=excluded.volume,
            price_open=excluded.price_open, price_close=excluded.price_close,
            profit=excluded.profit, swap=excluded.swap, commission=excluded.commission,
            time_open=excluded.time_open, time_close=excluded.time_close
    """, deal)
    conn.commit()
    conn.close()


def insights(bucket: str, limit_periods: int = 30, db_path: Path = DB_PATH) -> list[dict]:
    """One row per (period, side): trade count, volume, net profit, wins.
    `bucket` is one of BUCKET_FORMATS' keys (minute/hour/day/month/year)."""
    fmt = BUCKET_FORMATS.get(bucket, BUCKET_FORMATS["day"])
    conn = get_connection(db_path)
    rows = conn.execute(f"""
        SELECT period, side, trades, volume, profit, wins FROM (
            SELECT
                strftime('{fmt}', time_close, 'unixepoch') AS period,
                side,
                COUNT(*) AS trades,
                SUM(volume) AS volume,
                SUM(profit + swap + commission) AS profit,
                SUM(CASE WHEN profit + swap + commission > 0 THEN 1 ELSE 0 END) AS wins
            FROM deals
            GROUP BY period, side
        )
        ORDER BY period DESC
        LIMIT ?
    """, (limit_periods * 2,)).fetchall()  # *2: a BUY row and a SELL row per period
    conn.close()
    return [dict(r) for r in rows]


def summary(db_path: Path = DB_PATH) -> dict:
    conn = get_connection(db_path)
    row = conn.execute("""
        SELECT
            COUNT(*) AS trades,
            COALESCE(SUM(profit + swap + commission), 0) AS total_profit,
            COALESCE(SUM(CASE WHEN profit + swap + commission > 0 THEN 1 ELSE 0 END), 0) AS wins,
            COALESCE(SUM(CASE WHEN profit + swap + commission > 0 THEN profit + swap + commission ELSE 0 END), 0) AS gross_profit,
            COALESCE(SUM(CASE WHEN profit + swap + commission < 0 THEN -(profit + swap + commission) ELSE 0 END), 0) AS gross_loss
        FROM deals
    """).fetchone()
    conn.close()

    d = dict(row)
    trades = d["trades"]
    d["win_rate"] = round(d["wins"] / trades * 100, 1) if trades else 0.0
    d["profit_factor"] = round(d["gross_profit"] / d["gross_loss"], 2) if d["gross_loss"] else None
    return d
