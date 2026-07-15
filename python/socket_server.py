"""Generic async TCP transport: newline-delimited JSON in, JSON out.

This file should stay generic. Business logic (what to do with a
"tick" message, when to emit a "signal") belongs in handlers.py, not
here. To add a new message type, register a handler with `server.on(...)`
- nothing in this file needs to change.
"""
import asyncio
import logging
from typing import Awaitable, Callable, Optional

from protocol import encode, decode_lines

logger = logging.getLogger("socket_server")

Handler = Callable[["Client", dict], Awaitable[None]]


class Client:
    """One connected MT5 terminal.

    account_id is None until the "hello" message is processed (see
    session_manager.py) - it identifies which MT5 account this connection
    belongs to, so multiple terminals can stay connected at once.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.address = writer.get_extra_info("peername")
        self.account_id: Optional[str] = None

    async def send(self, message: dict) -> None:
        self.writer.write(encode(message))
        await self.writer.drain()

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass


class SocketServer:
    """Minimal TCP server: newline-delimited JSON in, JSON out.

    Add new message types by registering a handler with `on()`:

        @server.on("tick")
        async def on_tick(client, message):
            ...
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9090):
        self.host = host
        self.port = port
        self._handlers: dict[str, Handler] = {}
        # Handlers that must run inline in the read loop instead of the ordered
        # per-client worker. These are the ones that only correlate a reply back
        # to a pending future (order_result, bar, history_end, ...). Running them
        # inline is what breaks the re-entrant deadlock: a business handler
        # (tick/account/positions) can await an EA reply while the read loop
        # stays free to read that very reply and resolve the future. See
        # _handle_client for the routing.
        self._immediate_handlers: dict[str, Handler] = {}
        # List, not set: order must reflect connection order so clients()[-1]
        # reliably means "most recently connected" (see TradeGateway).
        self._clients: list[Client] = []
        self._disconnect_handlers: list[Callable[[Client], None]] = []
        self._server: Optional[asyncio.AbstractServer] = None

    def on(self, msg_type: str):
        """Decorator: register an ordered handler for message["type"] == msg_type.

        Ordered handlers run one-at-a-time per client via a worker task, so the
        order they arrive on the wire is preserved (important for the
        positions_begin/position/positions_end snapshot burst)."""
        def register(fn: Handler) -> Handler:
            self._handlers[msg_type] = fn
            return fn
        return register

    def on_immediate(self, msg_type: str):
        """Decorator: register a handler that runs inline in the read loop.

        Use ONLY for fast, non-blocking handlers that correlate a reply to a
        pending request (they must not await anything slow). This keeps the
        read loop able to service EA replies while an ordered handler is still
        awaiting one - without it, request/response over the same socket
        deadlocks until it times out."""
        def register(fn: Handler) -> Handler:
            self._immediate_handlers[msg_type] = fn
            return fn
        return register

    def on_disconnect(self, fn: Callable[[Client], None]) -> None:
        """Register a callback invoked (synchronously) when a client
        disconnects - used by SessionManager to mark a session offline."""
        self._disconnect_handlers.append(fn)

    async def broadcast(self, message: dict) -> None:
        """Push a message to every connected client (e.g. a trade signal)."""
        for client in list(self._clients):
            try:
                await client.send(message)
            except Exception:
                logger.exception("broadcast failed for %s", client.address)

    def clients(self) -> list[Client]:
        return list(self._clients)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        logger.info("listening on %s:%s", self.host, self.port)
        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = Client(reader, writer)
        self._clients.append(client)
        logger.info("client connected: %s", client.address)
        buffer = b""
        # Ordered business messages go through this queue and are handled one at
        # a time by a dedicated worker, so the read loop below never blocks on a
        # slow handler. Reply messages (immediate handlers) are dispatched right
        # here in the read loop so a worker awaiting an EA reply can never
        # starve itself of that reply.
        queue: asyncio.Queue = asyncio.Queue()
        worker = asyncio.ensure_future(self._client_worker(client, queue))
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                buffer += chunk
                messages, buffer = decode_lines(buffer)
                for message in messages:
                    if message.get("type") in self._immediate_handlers:
                        await self._dispatch(client, message)
                    else:
                        queue.put_nowait(message)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            except Exception:
                # Not the expected cancellation - a real bug in a queued
                # handler would otherwise vanish silently here.
                logger.exception("client worker raised on shutdown: %s", client.address)
            if client in self._clients:
                self._clients.remove(client)
            await client.close()
            logger.info("client disconnected: %s (account_id=%s)", client.address, client.account_id)
            for fn in self._disconnect_handlers:
                try:
                    fn(client)
                except Exception:
                    logger.exception("on_disconnect handler failed")

    async def _client_worker(self, client: Client, queue: "asyncio.Queue") -> None:
        """Process one client's ordered messages sequentially, off the read loop."""
        while True:
            message = await queue.get()
            try:
                await self._dispatch(client, message)
            finally:
                queue.task_done()

    async def _dispatch(self, client: Client, message: dict) -> None:
        msg_type = message.get("type")
        handler = self._handlers.get(msg_type) or self._immediate_handlers.get(msg_type)
        if handler is None:
            logger.warning("no handler for type=%r", msg_type)
            return
        try:
            await handler(client, message)
        except Exception:
            logger.exception("handler for %r failed", msg_type)
