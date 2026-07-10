import asyncio
import logging

import uvicorn

from socket_server import SocketServer
from position_store import PositionStore
from trade_gateway import TradeGateway
from grid_jobs import GridJobManager
from price_cache import PriceCache
from account_store import AccountStore
import db
import handlers
import web

SOCKET_HOST, SOCKET_PORT = "127.0.0.1", 9090
WEB_HOST, WEB_PORT = "127.0.0.1", 8000


async def run() -> None:
    db.init_db()

    server = SocketServer(host=SOCKET_HOST, port=SOCKET_PORT)
    store = PositionStore()
    gateway = TradeGateway(server)
    grid_manager = GridJobManager(gateway)
    price_cache = PriceCache()
    account_store = AccountStore()
    handlers.register(server, store, gateway, grid_manager, price_cache, account_store)

    app = web.create_app(store, gateway, grid_manager, price_cache, account_store)
    web_config = uvicorn.Config(app, host=WEB_HOST, port=WEB_PORT, log_level="info")
    web_server = uvicorn.Server(web_config)

    await asyncio.gather(server.start(), web_server.serve())


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
