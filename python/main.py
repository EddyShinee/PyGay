import asyncio
import logging

from socket_server import SocketServer
import handlers


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    server = SocketServer(host="127.0.0.1", port=9090)
    handlers.register(server)
    asyncio.run(server.start())


if __name__ == "__main__":
    main()
