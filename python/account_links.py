"""Link web dashboard users to MT5 account_id values via Supabase RPCs.

See supabase_schema.sql / supabase_update.sql.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

ACCOUNT_ID_RE = re.compile(r"^[0-9]{5,12}$")
LinkVia = Literal["manual", "discovered", "admin"]


class AccountLinkError(Exception):
    pass


class AccountAlreadyLinked(Exception):
    pass


class InvalidAccountId(Exception):
    pass


class LinkConfigError(Exception):
    pass


class LinkUnavailable(Exception):
    pass


@dataclass(frozen=True)
class LinkedAccount:
    account_id: str
    linked_via: str
    socket_host: str = "127.0.0.1"
    socket_port: int = 9090
    created_at: Optional[str] = None


@dataclass(frozen=True)
class AccountOwner:
    user_id: str
    account_id: str
    linked_via: str


def _supabase_url() -> str:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not url:
        raise LinkConfigError("Thiếu SUPABASE_URL. Tạo file python/.env")
    return url


def _supabase_key() -> str:
    key = os.environ.get("SUPABASE_KEY", "")
    if not key:
        raise LinkConfigError("Thiếu SUPABASE_KEY. Tạo file python/.env")
    return key


def _headers() -> dict[str, str]:
    key = _supabase_key()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


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


def _schema_missing(resp: httpx.Response) -> bool:
    return resp.status_code == 404 or "dashboard_user_accounts" in _parse_error(resp)


def _check_rpc_overload(resp: httpx.Response, fn_name: str = "link_user_account") -> None:
    if resp.status_code == 300:
        raise LinkConfigError(
            f"RPC {fn_name} bị trùng overload trên Supabase (HTTP 300). "
            "Chạy trong SQL Editor: "
            f"drop function if exists public.{fn_name}(uuid, text, text);"
        )


def normalize_account_id(account_id: str) -> str:
    account_id = account_id.strip()
    if not ACCOUNT_ID_RE.match(account_id):
        raise InvalidAccountId("account_id phải là số MT5 (5–12 chữ số)")
    return account_id


def normalize_socket_host(host: str) -> str:
    host = (host or "").strip()
    return host or "127.0.0.1"


def normalize_socket_port(port: int) -> int:
    if port < 1 or port > 65535:
        raise InvalidAccountId("Port phải từ 1 đến 65535")
    return port


def list_linked_accounts(user_id: str) -> list[LinkedAccount]:
    resp = _rpc("list_user_accounts", {"p_user_id": user_id})
    if _schema_missing(resp):
        raise LinkConfigError(
            "Chưa tạo bảng dashboard_user_accounts. "
            "Chạy python/supabase_schema.sql trong SQL Editor"
        )
    if resp.status_code >= 400:
        raise LinkUnavailable(_parse_error(resp))
    rows = resp.json() or []
    return [
        LinkedAccount(
            account_id=str(row["account_id"]),
            linked_via=str(row["linked_via"]),
            socket_host=str(row.get("socket_host") or "127.0.0.1"),
            socket_port=int(row.get("socket_port") or 9090),
            created_at=row.get("created_at"),
        )
        for row in rows
    ]


def list_claimed_account_ids() -> set[str]:
    resp = _rpc("list_claimed_account_ids", {})
    if _schema_missing(resp):
        raise LinkConfigError(
            "Chưa tạo bảng dashboard_user_accounts. "
            "Chạy python/supabase_schema.sql trong SQL Editor"
        )
    if resp.status_code >= 400:
        raise LinkUnavailable(_parse_error(resp))
    return {str(aid) for aid in (resp.json() or [])}


def get_owner(account_id: str) -> Optional[AccountOwner]:
    account_id = normalize_account_id(account_id)
    resp = _rpc("get_account_owner", {"p_account_id": account_id})
    if _schema_missing(resp):
        raise LinkConfigError(
            "Chưa tạo bảng dashboard_user_accounts. "
            "Chạy python/supabase_schema.sql trong SQL Editor"
        )
    if resp.status_code >= 400:
        raise LinkUnavailable(_parse_error(resp))
    row = resp.json()
    if not row:
        return None
    return AccountOwner(
        user_id=str(row["user_id"]),
        account_id=str(row["account_id"]),
        linked_via=str(row["linked_via"]),
    )


def link_account(
    user_id: str,
    account_id: str,
    via: LinkVia,
    socket_host: str = "127.0.0.1",
    socket_port: int = 9090,
) -> LinkedAccount:
    account_id = normalize_account_id(account_id)
    host = normalize_socket_host(socket_host)
    port = normalize_socket_port(socket_port)
    resp = _rpc(
        "link_user_account",
        {
            "p_user_id": user_id,
            "p_account_id": account_id,
            "p_via": via,
            "p_socket_host": host,
            "p_socket_port": port,
        },
    )
    if _schema_missing(resp):
        raise LinkConfigError(
            "Chưa tạo bảng dashboard_user_accounts. "
            "Chạy python/supabase_schema.sql trong SQL Editor"
        )
    _check_rpc_overload(resp)
    if resp.status_code >= 400:
        msg = _parse_error(resp)
        if "ACCOUNT_ALREADY_LINKED" in msg:
            raise AccountAlreadyLinked(
                f"Tài khoản MT5 #{account_id} đã được gắn với user khác"
            )
        if "INVALID_ACCOUNT_ID" in msg:
            raise InvalidAccountId("account_id phải là số MT5 (5–12 chữ số)")
        raise LinkUnavailable(msg)
    payload = resp.json()
    return LinkedAccount(
        account_id=str(payload["account_id"]),
        linked_via=str(payload["linked_via"]),
        socket_host=str(payload.get("socket_host") or host),
        socket_port=int(payload.get("socket_port") or port),
    )


class AccountNotLinked(Exception):
    pass


def update_account_socket(
    user_id: str,
    account_id: str,
    socket_host: str,
    socket_port: int,
) -> LinkedAccount:
    account_id = normalize_account_id(account_id)
    host = normalize_socket_host(socket_host)
    port = normalize_socket_port(socket_port)
    resp = _rpc(
        "update_account_socket",
        {
            "p_user_id": user_id,
            "p_account_id": account_id,
            "p_socket_host": host,
            "p_socket_port": port,
        },
    )
    if _schema_missing(resp):
        raise LinkConfigError(
            "Chưa có cột socket_host/socket_port. "
            "Chạy block v3 trong python/supabase_update.sql"
        )
    if resp.status_code >= 400:
        msg = _parse_error(resp)
        if "ACCOUNT_NOT_LINKED" in msg:
            raise AccountNotLinked(f"Chưa gắn tài khoản MT5 #{account_id}")
        if "INVALID_SOCKET_PORT" in msg:
            raise InvalidAccountId("Port phải từ 1 đến 65535")
        raise LinkUnavailable(msg)
    payload = resp.json()
    return LinkedAccount(
        account_id=str(payload["account_id"]),
        linked_via="",
        socket_host=str(payload.get("socket_host") or host),
        socket_port=int(payload.get("socket_port") or port),
    )


def unlink_account(user_id: str, account_id: str) -> bool:
    account_id = normalize_account_id(account_id)
    resp = _rpc(
        "unlink_user_account",
        {"p_user_id": user_id, "p_account_id": account_id},
    )
    if _schema_missing(resp):
        raise LinkConfigError(
            "Chưa tạo bảng dashboard_user_accounts. "
            "Chạy python/supabase_schema.sql trong SQL Editor"
        )
    if resp.status_code >= 400:
        raise LinkUnavailable(_parse_error(resp))
    return bool(resp.json())
