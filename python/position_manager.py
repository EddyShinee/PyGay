"""Per-account position manager - the "middle" layer between entry and exit.

Groups the account's managed positions into baskets keyed by (symbol, side)
and runs a set of independent, opt-in strategies over each basket:

  - basket : close the whole basket on total profit/loss (money or points)
  - dca    : Martingale averaging - add same-side when losing, scale lot up
  - grid   : add same-side every N points of adverse move (regardless of P/L)
  - pyramid: add same-side when in profit (ride the trend)
  - hedge  : open the opposite side once when drawdown is deep enough

Only positions whose magic matches the account's managed magic are touched, so
manual trades (magic 0 / other EAs) are left alone. Config lives in Supabase
(account_manage.py). Evaluation is tick-driven with a per-symbol throttle plus
a background supervisor fallback, mirroring EntryManager.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Optional

import account_manage
import telegram_notify
from grid_jobs import scaled_lot, sl_tp_from_points

if TYPE_CHECKING:
    from session_manager import AccountSession, SessionManager

logger = logging.getLogger("position_manager")

SUPERVISOR_INTERVAL_S = 1.0
EVAL_THROTTLE_S = 1.0
MIN_LOT = 0.01


def _round_lot(lot: float) -> float:
    return max(MIN_LOT, round(lot, 2))


@dataclass
class ManageConfig:
    enabled: bool = False
    # 0 = manage whichever magic the EA/account currently uses; >0 = only this
    # magic. Positions with a different magic (e.g. manual = 0) are never touched.
    manage_magic: int = 0
    symbols: list = field(default_factory=list)  # empty = all symbols with managed positions
    sltp_unit: str = "points"  # points | pips (for *_points distances)

    # Global safety rails
    max_positions_per_basket: int = 10
    max_total_lot: float = 1.0
    max_lot_per_order: float = 1.0
    add_cooldown_seconds: float = 5.0

    # --- basket close ---
    basket_enabled: bool = False
    basket_tp_money: Optional[float] = None
    basket_sl_money: Optional[float] = None
    basket_tp_points: Optional[float] = None
    basket_sl_points: Optional[float] = None

    # --- DCA / Martingale (add when losing) ---
    dca_enabled: bool = False
    dca_step_points: float = 200.0
    dca_lot_mode: str = "multiply"  # multiply | add | none
    dca_lot_value: float = 2.0
    dca_max_steps: int = 5

    # --- Grid (add every step regardless of P/L) ---
    grid_enabled: bool = False
    grid_step_points: float = 300.0
    grid_lot_mode: str = "none"  # none | add | multiply
    grid_lot_value: float = 0.0
    grid_max_levels: int = 5

    # --- Pyramiding (add when in profit) ---
    pyr_enabled: bool = False
    pyr_step_points: float = 200.0
    pyr_lot_mode: str = "none"
    pyr_lot_value: float = 0.0
    pyr_max_steps: int = 3
    pyr_trail_points: float = 0.0  # 0 = don't trail basket SL

    # --- Hedge (open opposite when drawdown deep) ---
    hedge_enabled: bool = False
    hedge_dd_money: Optional[float] = None
    hedge_dd_points: Optional[float] = None
    hedge_lot_ratio: float = 1.0  # each opposite order lot = ratio * basket volume
    hedge_max_orders: int = 1     # how many hedge orders allowed per basket
    hedge_step_points: float = 0.0  # 0 = open all at once; >0 = add one more each extra step

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ManageConfig":
        if not data:
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def symbol_list(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in (self.symbols or []):
            for part in str(item).replace(";", ",").replace(" ", ",").split(","):
                s = part.strip().upper()
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)
        return out


@dataclass
class _BasketState:
    seeded: bool = False
    last_add_ts: float = 0.0
    ref_price: float = 0.0     # price the next add step is measured from
    dca_steps: int = 0
    grid_levels: int = 0
    pyr_steps: int = 0
    hedge_count: int = 0
    hedge_ref: float = 0.0  # adverse-move reference for laddered hedges


def _pnl(p: dict) -> float:
    return float(p.get("profit", 0.0)) + float(p.get("swap", 0.0))


class PositionManager:
    def __init__(self, session: "AccountSession"):
        self._session = session
        self.config = ManageConfig()
        self.enabled = False
        self._loaded = False
        self._acting = False
        self._last_trigger: Optional[str] = None
        self._baskets: dict[str, _BasketState] = {}
        self._eval_ts: dict[str, float] = {}

    @property
    def account_id(self) -> str:
        return self._session.account_id

    def _unit_factor(self) -> float:
        return 10.0 if (self.config.sltp_unit or "points").lower() == "pips" else 1.0

    async def reload_config(self) -> None:
        try:
            row = await asyncio.to_thread(account_manage.get_manage_config, self.account_id)
        except Exception:
            logger.exception("[%s] load manage config failed - keeping current", self.account_id)
            return
        if row is None:
            self.config = ManageConfig()
            self.enabled = False
        else:
            self.config = ManageConfig.from_dict(row.get("config"))
            self.enabled = bool(row.get("enabled"))
        self._loaded = True

    def _effective_magic(self) -> int:
        if self.config.manage_magic and self.config.manage_magic > 0:
            return int(self.config.manage_magic)
        account = self._session.account_store.snapshot()
        try:
            return int(account.get("magic") or 0)
        except (TypeError, ValueError):
            return 0

    def _managed_positions(self, symbol: str) -> list[dict]:
        magic = self._effective_magic()
        out = []
        for p in self._session.store.snapshot():
            if (p.get("symbol") or "").upper() != symbol.upper():
                continue
            if magic > 0 and int(p.get("magic") or 0) != magic:
                continue
            out.append(p)
        return out

    def status(self) -> dict:
        cfg = self.config
        magic = self._effective_magic()
        baskets = []
        symbols = cfg.symbol_list()
        # Report every symbol/side that currently has managed positions.
        seen_syms: set[str] = set(symbols)
        for p in self._session.store.snapshot():
            if magic > 0 and int(p.get("magic") or 0) != magic:
                continue
            seen_syms.add((p.get("symbol") or "").upper())
        for sym in sorted(s for s in seen_syms if s):
            for side in ("BUY", "SELL"):
                pos = [p for p in self._managed_positions(sym) if p.get("side") == side]
                if not pos:
                    continue
                baskets.append({
                    "symbol": sym,
                    "side": side,
                    "count": len(pos),
                    "volume": round(sum(float(p["volume"]) for p in pos), 2),
                    "pnl": round(sum(_pnl(p) for p in pos), 2),
                })
        return {
            "enabled": self.enabled,
            "acting": self._acting,
            "last_trigger": self._last_trigger,
            "magic": magic,
            "baskets": baskets,
        }

    # -- basket metrics --------------------------------------------------------
    def _basket_metrics(self, positions: list[dict], side: str, bid: float, ask: float, point: float):
        total_vol = sum(float(p["volume"]) for p in positions)
        if total_vol <= 0:
            return None
        avg_price = sum(float(p["price_open"]) * float(p["volume"]) for p in positions) / total_vol
        pnl_money = sum(_pnl(p) for p in positions)
        # Most recent entry (highest time_open) - the reference for step adds.
        last_pos = max(positions, key=lambda p: int(p.get("time_open") or 0))
        cur = bid if side == "BUY" else ask  # price we'd close at
        if point > 0:
            pnl_points = ((cur - avg_price) if side == "BUY" else (avg_price - cur)) / point
        else:
            pnl_points = 0.0
        return {
            "total_vol": total_vol,
            "avg_price": avg_price,
            "pnl_money": pnl_money,
            "pnl_points": pnl_points,
            "last_price": float(last_pos["price_open"]),
            "last_lot": float(last_pos["volume"]),
            "count": len(positions),
        }

    async def evaluate(self, symbol: str, bid: float, ask: float, point: float) -> None:
        if not self.enabled or not self._session.connected or self._acting or point <= 0:
            return
        symbol = symbol.upper()
        allowed = self.config.symbol_list()
        if allowed and symbol not in allowed:
            return
        now = time.monotonic()
        if now - self._eval_ts.get(symbol, 0.0) < EVAL_THROTTLE_S:
            return
        self._eval_ts[symbol] = now

        for side in ("BUY", "SELL"):
            positions = [p for p in self._managed_positions(symbol) if p.get("side") == side]
            key = f"{symbol}:{side}"
            if not positions:
                self._baskets.pop(key, None)
                continue
            m = self._basket_metrics(positions, side, bid, ask, point)
            if m is None:
                continue
            st = self._baskets.get(key)
            if st is None:
                st = _BasketState(seeded=True, ref_price=m["last_price"])
                self._baskets[key] = st

            acted = await self._manage_basket(symbol, side, st, m, bid, ask, point)
            if acted:
                return  # one action per evaluation cycle keeps things calm

    async def _manage_basket(self, symbol, side, st, m, bid, ask, point) -> bool:
        cfg = self.config
        cur = bid if side == "BUY" else ask

        # 1) Basket close (money or points target) - highest priority.
        if cfg.basket_enabled:
            reason = None
            if cfg.basket_tp_money is not None and m["pnl_money"] >= cfg.basket_tp_money:
                reason = f"Chốt rổ +{m['pnl_money']:.2f}$ (TP {cfg.basket_tp_money}$)"
            elif cfg.basket_sl_money is not None and m["pnl_money"] <= -abs(cfg.basket_sl_money):
                reason = f"Cắt rổ {m['pnl_money']:.2f}$ (SL {cfg.basket_sl_money}$)"
            elif cfg.basket_tp_points is not None and m["pnl_points"] >= cfg.basket_tp_points:
                reason = f"Chốt rổ +{m['pnl_points']:.0f}pts (TP {cfg.basket_tp_points})"
            elif cfg.basket_sl_points is not None and m["pnl_points"] <= -abs(cfg.basket_sl_points):
                reason = f"Cắt rổ {m['pnl_points']:.0f}pts (SL {cfg.basket_sl_points})"
            if reason:
                await self._close_basket(symbol, side, reason, m["pnl_money"])
                return True

        # Adds share one cooldown so multiple enabled strategies don't stack.
        now = time.monotonic()
        if now - st.last_add_ts < cfg.add_cooldown_seconds:
            return False
        if m["count"] >= cfg.max_positions_per_basket:
            return False

        unit = self._unit_factor()
        move = (cur - st.ref_price) if side == "BUY" else (st.ref_price - cur)
        move_points = move / point  # >0 = favorable, <0 = adverse

        # 2) Hedge (open opposite when drawdown deep - may open several).
        if cfg.hedge_enabled and st.hedge_count < max(1, cfg.hedge_max_orders):
            hit = False
            if cfg.hedge_dd_money is not None and m["pnl_money"] <= -abs(cfg.hedge_dd_money):
                hit = True
            if cfg.hedge_dd_points is not None and -m["pnl_points"] >= abs(cfg.hedge_dd_points):
                hit = True
            if hit:
                remaining = max(1, cfg.hedge_max_orders) - st.hedge_count
                # step_points > 0 → ladder one at a time on further adverse move
                if st.hedge_count > 0 and cfg.hedge_step_points > 0:
                    step = cfg.hedge_step_points * unit * point
                    if (-move) < step:
                        remaining = 0  # not deep enough for the next rung yet
                    else:
                        remaining = 1
                elif cfg.hedge_step_points > 0:
                    remaining = 1  # first rung only; the rest ladder in later
                if remaining > 0:
                    opp = "SELL" if side == "BUY" else "BUY"
                    lot = _round_lot(min(cfg.max_lot_per_order, m["total_vol"] * cfg.hedge_lot_ratio))
                    reason = f"Hedge {opp} {lot} lot ×{remaining} (rổ {side} lỗ {m['pnl_money']:.2f}$)"
                    opened = await self._add_order(symbol, opp, lot, reason, count=remaining)
                    if opened > 0:
                        st.hedge_count += opened
                        st.hedge_ref = cur
                        st.ref_price = cur
                        st.last_add_ts = now
                        return True

        # 3) DCA / Martingale (add same side when losing, on adverse move).
        if cfg.dca_enabled and st.dca_steps < cfg.dca_max_steps and m["pnl_points"] < 0:
            need = cfg.dca_step_points * unit * point
            if (-move) >= need:
                lot = _round_lot(scaled_lot(m["last_lot"], cfg.dca_lot_mode, cfg.dca_lot_value))
                lot = min(lot, cfg.max_lot_per_order)
                if m["total_vol"] + lot <= cfg.max_total_lot:
                    reason = f"DCA #{st.dca_steps + 1} {side} {lot} lot (lỗ, giá đi ngược {-move_points:.0f}pts)"
                    if await self._add_order(symbol, side, lot, reason):
                        st.dca_steps += 1
                        st.ref_price = cur
                        st.last_add_ts = now
                        return True

        # 4) Grid (add same side every step of adverse move, ignore P/L).
        if cfg.grid_enabled and st.grid_levels < cfg.grid_max_levels:
            need = cfg.grid_step_points * unit * point
            if (-move) >= need:
                lot = _round_lot(scaled_lot(m["last_lot"], cfg.grid_lot_mode, cfg.grid_lot_value))
                lot = min(lot, cfg.max_lot_per_order)
                if m["total_vol"] + lot <= cfg.max_total_lot:
                    reason = f"Grid #{st.grid_levels + 1} {side} {lot} lot (bước {cfg.grid_step_points})"
                    if await self._add_order(symbol, side, lot, reason):
                        st.grid_levels += 1
                        st.ref_price = cur
                        st.last_add_ts = now
                        return True

        # 5) Pyramiding (add same side when in profit, on favorable move).
        if cfg.pyr_enabled and st.pyr_steps < cfg.pyr_max_steps and m["pnl_points"] > 0:
            need = cfg.pyr_step_points * unit * point
            if move >= need:
                lot = _round_lot(scaled_lot(m["last_lot"], cfg.pyr_lot_mode, cfg.pyr_lot_value))
                lot = min(lot, cfg.max_lot_per_order)
                if m["total_vol"] + lot <= cfg.max_total_lot:
                    reason = f"Pyramid #{st.pyr_steps + 1} {side} {lot} lot (lời, giá thuận {move_points:.0f}pts)"
                    if await self._add_order(symbol, side, lot, reason):
                        st.pyr_steps += 1
                        st.ref_price = cur
                        st.last_add_ts = now
                        if cfg.pyr_trail_points > 0:
                            await self._trail_basket(symbol, side, cur, point)
                        return True

        return False

    async def _add_order(self, symbol: str, side: str, lot: float, reason: str, count: int = 1) -> int:
        """Open `count` orders of the same side; returns how many succeeded."""
        if lot < MIN_LOT or count < 1:
            return 0
        self._acting = True
        self._last_trigger = f"{symbol} {side}: {reason}"
        opened = 0
        try:
            for _ in range(count):
                result = await self._session.gateway.open_order(symbol, side, lot, 0.0, 0.0)
                if result.get("ok"):
                    opened += 1
                else:
                    logger.warning("[%s] manage add failed: %s", self.account_id, result.get("error"))
                    break
            if opened:
                logger.info("[%s] manage add %s %s %.2f ×%d: %s",
                            self.account_id, side, symbol, lot, opened, reason)
                await telegram_notify.notify(
                    self.account_id,
                    telegram_notify.format_manage_action(self.account_id, "Thêm lệnh", reason),
                )
            return opened
        finally:
            self._acting = False

    async def _close_basket(self, symbol: str, side: str, reason: str, pnl: float) -> None:
        self._acting = True
        self._last_trigger = f"{symbol} {side}: {reason}"
        try:
            positions = [p for p in self._managed_positions(symbol) if p.get("side") == side]
            closed = 0
            for p in positions:
                res = await self._session.gateway.close_position(int(p["ticket"]))
                if res.get("ok"):
                    closed += 1
                else:
                    logger.warning("[%s] manage close #%s failed: %s", self.account_id, p["ticket"], res.get("error"))
            self._baskets.pop(f"{symbol}:{side}", None)
            logger.info("[%s] manage close basket %s %s (%d lệnh): %s", self.account_id, symbol, side, closed, reason)
            await telegram_notify.notify(
                self.account_id,
                telegram_notify.format_manage_action(
                    self.account_id, "Đóng rổ", f"{reason} · {closed} lệnh · P/L {pnl:.2f}$"
                ),
            )
        finally:
            self._acting = False

    async def _trail_basket(self, symbol: str, side: str, cur: float, point: float) -> None:
        """Move every basket ticket's SL to lock in profit (best-effort)."""
        offset = self.config.pyr_trail_points * self._unit_factor() * point
        sl = cur - offset if side == "BUY" else cur + offset
        for p in [p for p in self._managed_positions(symbol) if p.get("side") == side]:
            try:
                await self._session.gateway.modify_position(int(p["ticket"]), sl, float(p.get("tp") or 0.0))
            except Exception:
                logger.debug("[%s] trail modify #%s failed", self.account_id, p.get("ticket"))


async def run_manage_supervisor(sessions: "SessionManager") -> None:
    """Fallback loop so basket management keeps running even if ticks are quiet."""
    logger.info("position manager supervisor started")
    while True:
        try:
            for session in list(sessions.sessions.values()):
                pm = session.position_manager
                if not pm._loaded:
                    await pm.reload_config()
                if not session.connected or not pm.enabled:
                    continue
                magic = pm._effective_magic()
                symbols: set[str] = set(pm.config.symbol_list())
                for p in session.store.snapshot():
                    if magic > 0 and int(p.get("magic") or 0) != magic:
                        continue
                    symbols.add((p.get("symbol") or "").upper())
                for sym in symbols:
                    if not sym:
                        continue
                    price = session.price_cache.get(sym)
                    if price is None:
                        continue
                    await pm.evaluate(
                        sym,
                        float(price["bid"]),
                        float(price["ask"]),
                        float(price.get("point") or 0),
                    )
        except Exception:
            logger.exception("manage supervisor tick failed")
        await asyncio.sleep(SUPERVISOR_INTERVAL_S)
