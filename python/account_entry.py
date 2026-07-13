"""EntryManager configuration stored in Supabase Postgres.

Uses table `account_entry_config` + RPCs (see supabase_schema.sql /
supabase_update.sql v6). Same httpx/RPC shape as account_risk.py.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


class EntryConfigError(Exception):
    pass


class EntryUnavailable(Exception):
    pass


def _supabase_url() -> str:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not url:
        raise EntryConfigError("Thiếu SUPABASE_URL. Tạo file python/.env")
    return url


def _supabase_key() -> str:
    key = os.environ.get("SUPABASE_KEY", "")
    if not key:
        raise EntryConfigError("Thiếu SUPABASE_KEY. Tạo file python/.env")
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
    return resp.status_code == 404 or "account_entry_config" in _parse_error(resp)


_SCHEMA_HINT = (
    "Chưa tạo bảng account_entry_config trên Supabase. "
    "Chạy block v6 trong python/supabase_update.sql (hoặc supabase_schema.sql) ở SQL Editor"
)


def get_entry_config(account_id: str) -> Optional[dict]:
    resp = _rpc("get_account_entry", {"p_account_id": account_id})
    if _schema_missing(resp):
        raise EntryConfigError(_SCHEMA_HINT)
    if resp.status_code >= 400:
        raise EntryUnavailable(_parse_error(resp))
    row = resp.json()
    if not row:
        return None
    config = row.get("config")
    if not isinstance(config, dict):
        config = {}
    updated = row.get("updated_at")
    return {
        "enabled": bool(row.get("enabled")),
        "config": config,
        "updated_at": int(updated) if updated is not None else 0,
    }


def set_entry_config(account_id: str, enabled: bool, config: dict[str, Any]) -> dict:
    resp = _rpc(
        "upsert_account_entry",
        {
            "p_account_id": account_id,
            "p_enabled": bool(enabled),
            "p_config": config,
        },
    )
    if _schema_missing(resp):
        raise EntryConfigError(_SCHEMA_HINT)
    if resp.status_code >= 400:
        msg = _parse_error(resp)
        if "INVALID_ACCOUNT_ID" in msg:
            raise EntryConfigError("account_id phải là số MT5 (5–12 chữ số)")
        raise EntryUnavailable(msg)
    return resp.json() or {}


def clear_entry_config(account_id: str) -> bool:
    resp = _rpc("delete_account_entry", {"p_account_id": account_id})
    if _schema_missing(resp):
        raise EntryConfigError(_SCHEMA_HINT)
    if resp.status_code >= 400:
        raise EntryUnavailable(_parse_error(resp))
    return bool(resp.json())
