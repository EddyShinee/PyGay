import asyncio
import logging
import secrets
from pathlib import Path

import uvicorn
from starlette.middleware.sessions import SessionMiddleware

from socket_server import SocketServer
from session_manager import SessionManager
import auth
import db
import handlers
import web

SOCKET_HOST, SOCKET_PORT = "127.0.0.1", 9090
WEB_HOST, WEB_PORT = "127.0.0.1", 8000
SESSION_SECRET_PATH = Path(__file__).parent / ".session_secret"
SESSION_MAX_AGE_S = 60 * 60 * 24 * 30  # 30 days


def _load_or_create_session_secret() -> str:
    """Persisted so restarting main.py doesn't log everyone out."""
    if SESSION_SECRET_PATH.exists():
        return SESSION_SECRET_PATH.read_text().strip()
    secret = secrets.token_hex(32)
    SESSION_SECRET_PATH.write_text(secret)
    return secret


async def run() -> None:
    db.init_db()
    auth.init_db()

    server = SocketServer(host=SOCKET_HOST, port=SOCKET_PORT)
    sessions = SessionManager(server)
    handlers.register(server, sessions)

    app = web.create_app(sessions)
    app.add_middleware(
        SessionMiddleware,
        secret_key=_load_or_create_session_secret(),
        same_site="lax",
        max_age=SESSION_MAX_AGE_S,
    )
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
