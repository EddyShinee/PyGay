"""RiskManager configuration stored in Supabase Postgres.

Uses table `account_risk_config` + RPCs (see supabase_schema.sql /
supabase_update.sql v5). Same httpx/RPC shape as account_links.py / auth.py -
one config row per MT5 account_id, holding an arbitrary JSON blob so new risk
rules never require a schema migration.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


class RiskConfigError(Exception):
    """Misconfiguration or missing schema - user action required."""
    pass


class RiskUnavailable(Exception):
    """Transient Supabase failures (network, 5xx, etc.)."""
    pass


def _supabase_url() -> str:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not url:
        raise RiskConfigError("Thiếu SUPABASE_URL. Tạo file python/.env")
    return url


def _supabase_key() -> str:
    key = os.environ.get("SUPABASE_KEY", "")
    if not key:
        raise RiskConfigError("Thiếu SUPABASE_KEY. Tạo file python/.env")
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
    return resp.status_code == 404 or "account_risk_config" in _parse_error(resp)


_SCHEMA_HINT = (
    "Chưa tạo bảng account_risk_config trên Supabase. "
    "Chạy block v5 trong python/supabase_update.sql (hoặc supabase_schema.sql) ở SQL Editor"
)


def get_risk_config(account_id: str) -> Optional[dict]:
    """Return {'enabled': bool, 'config': dict, 'updated_at': int} or None."""
    resp = _rpc("get_account_risk", {"p_account_id": account_id})
    if _schema_missing(resp):
        raise RiskConfigError(_SCHEMA_HINT)
    if resp.status_code >= 400:
        raise RiskUnavailable(_parse_error(resp))
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


def set_risk_config(account_id: str, enabled: bool, config: dict[str, Any]) -> dict:
    resp = _rpc(
        "upsert_account_risk",
        {
            "p_account_id": account_id,
            "p_enabled": bool(enabled),
            "p_config": config,
        },
    )
    if _schema_missing(resp):
        raise RiskConfigError(_SCHEMA_HINT)
    if resp.status_code >= 400:
        msg = _parse_error(resp)
        if "INVALID_ACCOUNT_ID" in msg:
            raise RiskConfigError("account_id phải là số MT5 (5–12 chữ số)")
        raise RiskUnavailable(msg)
    return resp.json() or {}


def clear_risk_config(account_id: str) -> bool:
    resp = _rpc("delete_account_risk", {"p_account_id": account_id})
    if _schema_missing(resp):
        raise RiskConfigError(_SCHEMA_HINT)
    if resp.status_code >= 400:
        raise RiskUnavailable(_parse_error(resp))
    return bool(resp.json())
