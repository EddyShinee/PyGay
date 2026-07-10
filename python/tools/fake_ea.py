"""Simulates the MT5 EA side of the protocol for local testing, without
needing a real MetaTrader terminal.

Connects to the Python server as a TCP client (exactly like the real EA),
streams a random-walk tick for one symbol, maintains fake open positions,
and answers open_order/close_position/close_all/modify_position/get_positions
the same way SocketBridgeEA.mq5 is expected to.

Run: python3 tools/fake_ea.py   (with python/main.py already running)
"""
import asyncio
import itertools
import json
import logging
import random
import time

HOST, PORT = "127.0.0.1", 9090
SYMBOL = "EURUSD"
POINT = 0.00001
CONTRACT_SIZE = 100000

logging.basicConfig(level=logging.INFO, format="%(asctime)s fake_ea %(message)s")
log = logging.getLogger("fake_ea")


class FakeEA:
    def __init__(self) -> None:
        self.positions: dict[int, dict] = {}
        self.next_ticket = itertools.count(1000)
        self.bid = 1.08500
        self.ask = 1.08520
        self.balance = 10000.0
        self.magic = 123456

    def step_price(self) -> None:
        drift = random.uniform(-0.00006, 0.00006)
        self.bid = round(self.bid + drift, 5)
        self.ask = round(self.bid + 0.00002, 5)

    def tick_message(self) -> dict:
        return {"type": "tick", "symbol": SYMBOL, "bid": self.bid, "ask": self.ask,
                "point": POINT, "time": int(time.time())}

    def open_order(self, side: str, volume: float, sl: float, tp: float) -> int:
        ticket = next(self.next_ticket)
        price = self.ask if side == "BUY" else self.bid
        self.positions[ticket] = {
            "ticket": ticket, "symbol": SYMBOL, "side": side, "volume": volume,
            "price_open": price, "sl": sl or 0, "tp": tp or 0,
            "profit": 0.0, "swap": 0.0, "time_open": int(time.time()), "magic": self.magic,
        }
        log.info("opened #%s %s %.2f lot @ %.5f", ticket, side, volume, price)
        return ticket

    def update_profit(self) -> None:
        for p in self.positions.values():
            price = self.bid if p["side"] == "BUY" else self.ask
            sign = 1 if p["side"] == "BUY" else -1
            p["profit"] = round(sign * (price - p["price_open"]) * p["volume"] * CONTRACT_SIZE, 2)

    def snapshot_messages(self) -> list[dict]:
        msgs = [{"type": "positions_begin", "count": len(self.positions)}]
        msgs += [{"type": "position", **p} for p in self.positions.values()]
        msgs.append({"type": "positions_end"})
        return msgs

    def close_ticket(self, ticket: int) -> dict | None:
        """Remove a position, realize its profit into balance, and return a
        deal_closed message for it (or None if the ticket doesn't exist)."""
        p = self.positions.pop(ticket, None)
        if p is None:
            return None
        price = self.bid if p["side"] == "BUY" else self.ask
        self.balance += p["profit"] + p["swap"]
        return {
            "type": "deal_closed", "ticket": ticket, "symbol": p["symbol"], "side": p["side"],
            "volume": p["volume"], "price_open": p["price_open"], "price_close": price,
            "profit": p["profit"], "swap": p["swap"], "commission": 0.0,
            "time_open": p["time_open"], "time_close": int(time.time()),
        }

    def account_message(self) -> dict:
        floating = sum(p["profit"] for p in self.positions.values())
        equity = self.balance + floating
        margin = sum(p["volume"] for p in self.positions.values()) * 1000.0
        margin_free = equity - margin
        margin_level = (equity / margin * 100) if margin > 0 else 0.0
        return {
            "type": "account", "balance": round(self.balance, 2), "equity": round(equity, 2),
            "margin": round(margin, 2), "margin_free": round(margin_free, 2),
            "margin_level": round(margin_level, 2), "currency": "USD", "leverage": 100,
            "magic": self.magic,
        }


async def main() -> None:
    reader, writer = await asyncio.open_connection(HOST, PORT)
    ea = FakeEA()
    log.info("connected to %s:%s", HOST, PORT)

    async def send(msg: dict) -> None:
        writer.write((json.dumps(msg) + "\n").encode())
        await writer.drain()

    async def send_snapshot() -> None:
        ea.update_profit()
        for m in ea.snapshot_messages():
            await send(m)

    async def handle_message(msg: dict) -> None:
        msg_type = msg.get("type")
        req_id = msg.get("id")

        if msg_type == "open_order":
            ticket = ea.open_order(msg["side"], msg["volume"], msg.get("sl", 0), msg.get("tp", 0))
            await send({"type": "order_result", "id": req_id, "ok": True, "ticket": ticket})
            await send_snapshot()

        elif msg_type == "close_position":
            ticket = msg["ticket"]
            ea.update_profit()
            deal = ea.close_ticket(ticket)
            if deal:
                await send({"type": "order_result", "id": req_id, "ok": True, "ticket": ticket})
                await send(deal)
            else:
                await send({"type": "order_result", "id": req_id, "ok": False, "error": "ticket not found"})
            await send_snapshot()

        elif msg_type == "close_all":
            filt = msg.get("filter", "all")
            ea.update_profit()
            to_close = [
                t for t, p in ea.positions.items()
                if filt == "all" or (filt == "profit" and p["profit"] > 0) or (filt == "loss" and p["profit"] < 0)
            ]
            for t in to_close:
                deal = ea.close_ticket(t)
                if deal:
                    await send(deal)
            await send({"type": "order_result", "id": req_id, "ok": True, "ticket": 0})
            await send_snapshot()

        elif msg_type == "modify_position":
            ticket = msg["ticket"]
            if ticket in ea.positions:
                ea.positions[ticket]["sl"] = msg.get("sl", 0)
                ea.positions[ticket]["tp"] = msg.get("tp", 0)
                await send({"type": "order_result", "id": req_id, "ok": True, "ticket": ticket})
            else:
                await send({"type": "order_result", "id": req_id, "ok": False, "error": "ticket not found"})
            await send_snapshot()

        elif msg_type == "get_positions":
            await send_snapshot()

        elif msg_type == "set_magic":
            ea.magic = int(msg.get("magic", ea.magic))
            log.info("magic number set to %s", ea.magic)
            await send({"type": "order_result", "id": req_id, "ok": True, "ticket": 0})
            await send(ea.account_message())

        elif msg_type == "signal":
            action = msg.get("action")
            if action in ("BUY", "SELL"):
                ea.open_order(action, msg.get("volume", 0.01), msg.get("sl", 0), msg.get("tp", 0))
                await send_snapshot()

    async def handle_incoming() -> None:
        buffer = b""
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                log.info("server closed connection")
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line.strip():
                    await handle_message(json.loads(line))

    async def tick_loop() -> None:
        while True:
            ea.step_price()
            await send(ea.tick_message())
            await asyncio.sleep(0.5)

    async def snapshot_loop() -> None:
        while True:
            await send_snapshot()
            await send(ea.account_message())
            await asyncio.sleep(1)

    await asyncio.gather(handle_incoming(), tick_loop(), snapshot_loop())


if __name__ == "__main__":
    asyncio.run(main())
