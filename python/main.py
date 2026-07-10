import asyncio
import logging

import uvicorn

from socket_server import SocketServer
from session_manager import SessionManager
import db
import handlers
import web

SOCKET_HOST, SOCKET_PORT = "127.0.0.1", 9090
WEB_HOST, WEB_PORT = "127.0.0.1", 8000


async def run() -> None:
    db.init_db()

    server = SocketServer(host=SOCKET_HOST, port=SOCKET_PORT)
    sessions = SessionManager(server)
    handlers.register(server, sessions)

    app = web.create_app(sessions)
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
