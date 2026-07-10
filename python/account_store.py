"""Latest account snapshot (balance/equity/margin/...), fed by the EA's
"account" message and pushed to WebSocket subscribers - same pub/sub shape
as PositionStore, just for a single flat object instead of a list.
"""
import asyncio


class AccountStore:
    def __init__(self) -> None:
        self._account: dict = {}
        self._subscribers: set[asyncio.Queue] = set()

    async def update(self, account: dict) -> None:
        self._account = account
        await self._notify()

    def snapshot(self) -> dict:
        return dict(self._account)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    async def _notify(self) -> None:
        snap = self.snapshot()
        for queue in list(self._subscribers):
            await queue.put(snap)
