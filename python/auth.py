"""Web dashboard user accounts stored in Supabase Postgres.

Uses table `dashboard_users` + RPCs (see supabase_dashboard_users.sql).
Passwords are PBKDF2-HMAC-SHA256 hashed in Python — no Supabase Auth email
flow, so no email rate-limit on register.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

MIN_PASSWORD_LENGTH = 8
PBKDF2_ITERATIONS = 200_000
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,32}$")


class UsernameTaken(Exception):
    pass


class InvalidPassword(Exception):
    pass


class AuthConfigError(Exception):
    pass


class AuthUnavailable(Exception):
    """Transient Supabase failures (network, schema missing, etc.)."""
    pass


@dataclass(frozen=True)
class AuthUser:
    id: str
    username: str


def _supabase_url() -> str:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not url:
        raise AuthConfigError("Thiếu SUPABASE_URL trong .env")
    return url


def _supabase_key() -> str:
    key = os.environ.get("SUPABASE_KEY", "")
    if not key:
        raise AuthConfigError("Thiếu SUPABASE_KEY trong .env")
    return key


def _normalize_username(username: str) -> str:
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise InvalidPassword(
            "Tên đăng nhập chỉ gồm chữ, số, gạch dưới (3–32 ký tự)"
        )
    return username


def _headers() -> dict[str, str]:
    key = _supabase_key()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt, PBKDF2_ITERATIONS
    ).hex()


def _rpc(name: str, params: dict) -> httpx.Response:
    url = f"{_supabase_url()}/rest/v1/rpc/{name}"
    with httpx.Client(timeout=20.0) as client:
        return client.post(url, headers=_headers(), json=params)


def _parse_error(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except Exception:
        return resp.text or f"HTTP {resp.status_code}"
    if isinstance(payload, dict):
        return (
            payload.get("message")
            or payload.get("hint")
            or payload.get("error")
            or str(payload)
        )
    return str(payload)


def init_db() -> None:
    """Validate config + that dashboard_users RPCs exist."""
    _supabase_url()
    _supabase_key()
    resp = _rpc("get_dashboard_user_auth", {"p_username": "__ping__"})
    if resp.status_code == 404:
        raise AuthConfigError(
            "Chưa tạo bảng dashboard_users trên Supabase. "
            "Mở SQL Editor và chạy file python/supabase_dashboard_users.sql"
        )
    if resp.status_code >= 500:
        raise AuthUnavailable(_parse_error(resp))


def create_user(username: str, password: str) -> AuthUser:
    username = _normalize_username(username)
    if len(password) < MIN_PASSWORD_LENGTH:
        raise InvalidPassword(f"Mật khẩu phải có ít nhất {MIN_PASSWORD_LENGTH} ký tự")

    salt = os.urandom(16)
    password_hash = _hash_password(password, salt)
    resp = _rpc(
        "register_dashboard_user",
        {
            "p_username": username,
            "p_password_hash": password_hash,
            "p_salt": salt.hex(),
        },
    )

    if resp.status_code >= 400:
        msg = _parse_error(resp)
        if resp.status_code == 404:
            raise AuthConfigError(
                "Chưa tạo bảng dashboard_users trên Supabase. "
                "Chạy file python/supabase_dashboard_users.sql trong SQL Editor"
            )
        if "USERNAME_TAKEN" in msg:
            raise UsernameTaken(f"Tên đăng nhập '{username}' đã tồn tại")
        if "INVALID_USERNAME" in msg:
            raise InvalidPassword("Tên đăng nhập không hợp lệ")
        raise AuthUnavailable(msg)

    payload = resp.json()
    if not payload or not payload.get("id"):
        raise AuthUnavailable("Supabase không trả về user sau khi đăng ký")
    return AuthUser(id=str(payload["id"]), username=str(payload["username"]))


def verify_user(username: str, password: str) -> Optional[AuthUser]:
    try:
        username = _normalize_username(username)
    except InvalidPassword:
        return None

    resp = _rpc("get_dashboard_user_auth", {"p_username": username})
    if resp.status_code == 404:
        raise AuthConfigError(
            "Chưa tạo bảng dashboard_users trên Supabase. "
            "Chạy file python/supabase_dashboard_users.sql trong SQL Editor"
        )
    if resp.status_code >= 400:
        raise AuthUnavailable(_parse_error(resp))

    row = resp.json()
    if not row:
        return None

    expected = _hash_password(password, bytes.fromhex(row["salt"]))
    if not hmac.compare_digest(expected, row["password_hash"]):
        return None
    return AuthUser(id=str(row["id"]), username=str(row["username"]))


def get_username(user_id: str, session_username: Optional[str] = None) -> Optional[str]:
    return session_username
