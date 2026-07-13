"""PositionManager configuration stored in Supabase Postgres.

Uses table `account_manage_config` + RPCs (see supabase_update.sql v7). Same
httpx/RPC shape as account_entry.py / account_risk.py - one config row per MT5
account_id holding an arbitrary JSON blob.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


class ManageConfigError(Exception):
    pass


class ManageUnavailable(Exception):
    pass


def _supabase_url() -> str:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not url:
        raise ManageConfigError("Thiếu SUPABASE_URL. Tạo file python/.env")
    return url


def _supabase_key() -> str:
    key = os.environ.get("SUPABASE_KEY", "")
    if not key:
        raise ManageConfigError("Thiếu SUPABASE_KEY. Tạo file python/.env")
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
    return resp.status_code == 404 or "account_manage_config" in _parse_error(resp)


_SCHEMA_HINT = (
    "Chưa tạo bảng account_manage_config trên Supabase. "
    "Chạy block v7 trong python/supabase_update.sql ở SQL Editor"
)


def get_manage_config(account_id: str) -> Optional[dict]:
    resp = _rpc("get_account_manage", {"p_account_id": account_id})
    if _schema_missing(resp):
        raise ManageConfigError(_SCHEMA_HINT)
    if resp.status_code >= 400:
        raise ManageUnavailable(_parse_error(resp))
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


def set_manage_config(account_id: str, enabled: bool, config: dict[str, Any]) -> dict:
    resp = _rpc(
        "upsert_account_manage",
        {
            "p_account_id": account_id,
            "p_enabled": bool(enabled),
            "p_config": config,
        },
    )
    if _schema_missing(resp):
        raise ManageConfigError(_SCHEMA_HINT)
    if resp.status_code >= 400:
        msg = _parse_error(resp)
        if "INVALID_ACCOUNT_ID" in msg:
            raise ManageConfigError("account_id phải là số MT5 (5–12 chữ số)")
        raise ManageUnavailable(msg)
    return resp.json() or {}


def clear_manage_config(account_id: str) -> bool:
    resp = _rpc("delete_account_manage", {"p_account_id": account_id})
    if _schema_missing(resp):
        raise ManageConfigError(_SCHEMA_HINT)
    if resp.status_code >= 400:
        raise ManageUnavailable(_parse_error(resp))
    return bool(resp.json())
