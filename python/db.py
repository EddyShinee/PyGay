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


_CREATE_DEALS_SQL = """
    CREATE TABLE deals (
        account_id TEXT NOT NULL DEFAULT '',
        ticket INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        volume REAL NOT NULL,
        price_open REAL NOT NULL,
        price_close REAL NOT NULL,
        profit REAL NOT NULL,
        swap REAL NOT NULL,
        commission REAL NOT NULL,
        time_open INTEGER NOT NULL,
        time_close INTEGER NOT NULL,
        PRIMARY KEY (account_id, ticket)
    )
"""


_CREATE_ACCOUNT_SETTINGS_SQL = """
    CREATE TABLE IF NOT EXISTS account_settings (
        account_id TEXT PRIMARY KEY,
        telegram_bot_token TEXT,
        telegram_chat_id TEXT
    )
"""


def init_db(db_path: Path = DB_PATH) -> None:
    conn = get_connection(db_path)
    conn.execute(_CREATE_ACCOUNT_SETTINGS_SQL)
    _ensure_column(conn, "account_settings", "telegram_trade_symbol", "TEXT")
    _ensure_column(conn, "account_settings", "telegram_trade_lot", "REAL")

    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='deals'"
    ).fetchone()

    if existing is None:
        conn.execute(_CREATE_DEALS_SQL)
    else:
        # A pre-multi-account trades.db has `ticket` as the sole primary
        # key, which silently collides across accounts (ticket/position
        # ids are only unique *within* one MT5 account) - a plain
        # `ALTER TABLE ... ADD COLUMN` can add account_id but SQLite can't
        # alter an existing PRIMARY KEY, so rebuild the table properly.
        info = conn.execute("PRAGMA table_info(deals)").fetchall()
        pk_cols = [row[1] for row in sorted((r for r in info if r[5] > 0), key=lambda r: r[5])]
        if pk_cols != ["account_id", "ticket"]:
            had_account_id = "account_id" in [row[1] for row in info]
            conn.execute("ALTER TABLE deals RENAME TO deals_old")
            conn.execute(_CREATE_DEALS_SQL)
            account_expr = "account_id" if had_account_id else "''"
            conn.execute(f"""
                INSERT INTO deals (account_id, ticket, symbol, side, volume, price_open,
                                    price_close, profit, swap, commission, time_open, time_close)
                SELECT {account_expr}, ticket, symbol, side, volume, price_open, price_close,
                       profit, swap, commission, time_open, time_close
                FROM deals_old
            """)
            conn.execute("DROP TABLE deals_old")
    conn.commit()
    conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, coldef: str) -> None:
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")


def upsert_deal(account_id: str, deal: dict, db_path: Path = DB_PATH) -> None:
    """Insert a closed deal, or replace it if (account_id, ticket) was
    already recorded (e.g. a later partial close updates the totals, or
    the EA resent it after a reconnect)."""
    conn = get_connection(db_path)
    conn.execute("""
        INSERT INTO deals (account_id, ticket, symbol, side, volume, price_open, price_close,
                            profit, swap, commission, time_open, time_close)
        VALUES (:account_id, :ticket, :symbol, :side, :volume, :price_open, :price_close,
                :profit, :swap, :commission, :time_open, :time_close)
        ON CONFLICT(account_id, ticket) DO UPDATE SET
            symbol=excluded.symbol, side=excluded.side, volume=excluded.volume,
            price_open=excluded.price_open, price_close=excluded.price_close,
            profit=excluded.profit, swap=excluded.swap, commission=excluded.commission,
            time_open=excluded.time_open, time_close=excluded.time_close
    """, {**deal, "account_id": account_id})
    conn.commit()
    conn.close()


def insights(account_id: str, bucket: str, limit_periods: int = 30, db_path: Path = DB_PATH) -> list[dict]:
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
            WHERE account_id = ?
            GROUP BY period, side
        )
        ORDER BY period DESC
        LIMIT ?
    """, (account_id, limit_periods * 2)).fetchall()  # *2: a BUY row and a SELL row per period
    conn.close()
    return [dict(r) for r in rows]


def summary(account_id: str, db_path: Path = DB_PATH) -> dict:
    conn = get_connection(db_path)
    row = conn.execute("""
        SELECT
            COUNT(*) AS trades,
            COALESCE(SUM(profit + swap + commission), 0) AS total_profit,
            COALESCE(SUM(CASE WHEN profit + swap + commission > 0 THEN 1 ELSE 0 END), 0) AS wins,
            COALESCE(SUM(CASE WHEN profit + swap + commission > 0 THEN profit + swap + commission ELSE 0 END), 0) AS gross_profit,
            COALESCE(SUM(CASE WHEN profit + swap + commission < 0 THEN -(profit + swap + commission) ELSE 0 END), 0) AS gross_loss
        FROM deals
        WHERE account_id = ?
    """, (account_id,)).fetchone()
    conn.close()

    d = dict(row)
    trades = d["trades"]
    d["win_rate"] = round(d["wins"] / trades * 100, 1) if trades else 0.0
    d["profit_factor"] = round(d["gross_profit"] / d["gross_loss"], 2) if d["gross_loss"] else None
    return d


def get_telegram_config(account_id: str, db_path: Path = DB_PATH) -> Optional[dict]:
    """Returns bot/chat and optional trade defaults for Telegram commands."""
    conn = get_connection(db_path)
    row = conn.execute(
        """SELECT telegram_bot_token, telegram_chat_id,
                  telegram_trade_symbol, telegram_trade_lot
           FROM account_settings WHERE account_id = ?""",
        (account_id,),
    ).fetchone()
    conn.close()
    if row is None or not row["telegram_bot_token"] or not row["telegram_chat_id"]:
        return None
    lot = row["telegram_trade_lot"]
    return {
        "bot_token": row["telegram_bot_token"],
        "chat_id": str(row["telegram_chat_id"]),
        "trade_symbol": (row["telegram_trade_symbol"] or "").strip().upper() or None,
        "trade_lot": float(lot) if lot is not None else 0.01,
    }


def list_telegram_configs(db_path: Path = DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT account_id, telegram_bot_token, telegram_chat_id,
                  telegram_trade_symbol, telegram_trade_lot
           FROM account_settings
           WHERE telegram_bot_token IS NOT NULL AND telegram_bot_token != ''
             AND telegram_chat_id IS NOT NULL AND telegram_chat_id != ''"""
    ).fetchall()
    conn.close()
    out: list[dict] = []
    for row in rows:
        lot = row["telegram_trade_lot"]
        out.append({
            "account_id": row["account_id"],
            "bot_token": row["telegram_bot_token"],
            "chat_id": str(row["telegram_chat_id"]),
            "trade_symbol": (row["telegram_trade_symbol"] or "").strip().upper() or None,
            "trade_lot": float(lot) if lot is not None else 0.01,
        })
    return out


def set_telegram_config(
    account_id: str,
    bot_token: str,
    chat_id: str,
    trade_symbol: Optional[str] = None,
    trade_lot: Optional[float] = None,
    db_path: Path = DB_PATH,
) -> None:
    symbol = (trade_symbol or "").strip().upper() or None
    conn = get_connection(db_path)
    conn.execute(
        """
        INSERT INTO account_settings (
            account_id, telegram_bot_token, telegram_chat_id,
            telegram_trade_symbol, telegram_trade_lot
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(account_id) DO UPDATE SET
            telegram_bot_token = excluded.telegram_bot_token,
            telegram_chat_id = excluded.telegram_chat_id,
            telegram_trade_symbol = excluded.telegram_trade_symbol,
            telegram_trade_lot = excluded.telegram_trade_lot
        """,
        (account_id, bot_token, chat_id, symbol, trade_lot if trade_lot is not None else 0.01),
    )
    conn.commit()
    conn.close()


def clear_telegram_config(account_id: str, db_path: Path = DB_PATH) -> bool:
    conn = get_connection(db_path)
    cur = conn.execute(
        """
        UPDATE account_settings
        SET telegram_bot_token = NULL,
            telegram_chat_id = NULL,
            telegram_trade_symbol = NULL,
            telegram_trade_lot = NULL
        WHERE account_id = ?
          AND telegram_bot_token IS NOT NULL
          AND telegram_bot_token != ''
        """,
        (account_id,),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed
