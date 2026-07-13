"""Multi-account bookkeeping: one AccountSession per MT5 account_id,
looked up by SessionManager. account_id is bound to a Client the moment
its "hello" message arrives (see handlers.py) - everything else in the
protocol is unchanged, handlers just route to the right session using
client.account_id instead of a single global store/gateway.
"""
import asyncio
import logging
from typing import Optional

from socket_server import Client, SocketServer
from position_store import PositionStore
from account_store import AccountStore
from symbol_store import SymbolStore
from price_cache import PriceCache
from trade_gateway import TradeGateway
from grid_jobs import GridJobManager
from history_gateway import HistoryGateway
from risk_manager import RiskManager
import telegram_notify

logger = logging.getLogger("session_manager")


class AccountSession:
    def __init__(self, account_id: str, server: SocketServer):
        self.account_id = account_id
        self.info: dict = {}          # broker, name, currency (from hello)
        self.connected = False

        self.store = PositionStore()
        self.account_store = AccountStore()
        self.symbol_store = SymbolStore()
        self.price_cache = PriceCache()
        self.gateway = TradeGateway(server, account_id)
        self.grid_manager = GridJobManager(self.gateway)
        self.history_gateway = HistoryGateway(server, account_id)
        self.risk_manager = RiskManager(self)

        # Telegram notification bookkeeping (see handlers.py):
        self.has_synced_once = False   # skip diffing "new/modified order" on the very first snapshot
        self.last_drawdown_tier = 0    # 0/50/60/70/100, edge-triggered so we don't spam every second

    def summary(self) -> dict:
        """Compact info for the accounts overview page."""
        account = self.account_store.snapshot()
        return {
            "account_id": self.account_id,
            "connected": self.connected,
            "broker": self.info.get("broker", ""),
            "name": self.info.get("name", ""),
            "platform": self.info.get("platform", ""),
            "currency": self.info.get("currency", account.get("currency", "")),
            "balance": account.get("balance"),
            "equity": account.get("equity"),
            "floating_profit": self.store.total_profit(),
            "open_count": len(self.store.snapshot()),
        }


class SessionManager:
    def __init__(self, server: SocketServer):
        self.server = server
        self.sessions: dict[str, AccountSession] = {}
        self._subscribers: set[asyncio.Queue] = set()

    def get(self, account_id: Optional[str]) -> Optional[AccountSession]:
        if account_id is None:
            return None
        return self.sessions.get(account_id)

    async def bind(self, client: Client, account_id: str, info: dict) -> None:
        """Called when a "hello" arrives. Evicts any other live connection
        already bound to this account_id (e.g. the EA was recompiled and
        reconnected) so there's only ever one live client per account."""
        for other in self.server.clients():
            if other is not client and other.account_id == account_id:
                logger.warning("closing stale connection for account %s", account_id)
                await other.close()

        client.account_id = account_id
        session = self.sessions.get(account_id)
        if session is None:
            session = AccountSession(account_id, self.server)
            self.sessions[account_id] = session
            logger.info("new account session: %s", account_id)

        session.info = info
        session.connected = True
        await self._notify()

        broker = info.get("broker", "")
        await telegram_notify.notify(
            account_id,
            telegram_notify.format_account_connected(account_id, broker),
        )

    def on_client_disconnect(self, client: Client) -> None:
        session = self.sessions.get(client.account_id) if client.account_id else None
        if session is not None:
            session.connected = False
            asyncio.ensure_future(self._notify())

    def list_accounts(self) -> list[dict]:
        return [s.summary() for s in self.sessions.values()]

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    async def _notify(self) -> None:
        accounts = self.list_accounts()
        for queue in list(self._subscribers):
            await queue.put(accounts)
