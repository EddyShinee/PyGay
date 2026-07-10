"""Web dashboard user accounts - separate from MT5 trading accounts
entirely (see session_manager.py for those). Plain sqlite3, no ORM, same
style as db.py. Passwords hashed with PBKDF2-HMAC-SHA256 (stdlib only,
no extra dependency for something this well-trodden).
"""
import hashlib
import hmac
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "users.db"

PBKDF2_ITERATIONS = 200_000
MIN_PASSWORD_LENGTH = 8


class UsernameTaken(Exception):
    pass


class InvalidPassword(Exception):
    pass


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    conn = get_connection(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS).hex()


def create_user(username: str, password: str, db_path: Path = DB_PATH) -> int:
    username = username.strip()
    if len(password) < MIN_PASSWORD_LENGTH:
        raise InvalidPassword(f"Mật khẩu phải có ít nhất {MIN_PASSWORD_LENGTH} ký tự")

    salt = os.urandom(16)
    password_hash = _hash_password(password, salt)

    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
            (username, password_hash, salt.hex(), int(time.time())),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise UsernameTaken(f"Tên đăng nhập '{username}' đã tồn tại")
    finally:
        conn.close()


def verify_user(username: str, password: str, db_path: Path = DB_PATH) -> Optional[int]:
    conn = get_connection(db_path)
    row = conn.execute("SELECT id, password_hash, salt FROM users WHERE username = ?", (username.strip(),)).fetchone()
    conn.close()
    if row is None:
        return None
    expected = _hash_password(password, bytes.fromhex(row["salt"]))
    if hmac.compare_digest(expected, row["password_hash"]):
        return row["id"]
    return None


def get_username(user_id: int, db_path: Path = DB_PATH) -> Optional[str]:
    conn = get_connection(db_path)
    row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return row["username"] if row else None
