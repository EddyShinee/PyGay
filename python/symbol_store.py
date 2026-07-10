"""List of tradable symbols reported by the broker (via the EA), fed to the
dashboard's symbol dropdowns. Same pub/sub shape as AccountStore - fetched
once per EA connection (see SocketBridgeEA.mq5), not on every tick.
"""
import asyncio


class SymbolStore:
    def __init__(self) -> None:
        self._symbols: list[str] = []
        self._subscribers: set[asyncio.Queue] = set()

    async def update(self, symbols: list[str]) -> None:
        self._symbols = symbols
        await self._notify()

    def snapshot(self) -> list[str]:
        return list(self._symbols)

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
