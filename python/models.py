"""Plain data shapes shared across modules."""
from dataclasses import dataclass, asdict


@dataclass
class Position:
    ticket: int
    symbol: str
    side: str          # "BUY" | "SELL"
    volume: float
    price_open: float
    sl: float
    tp: float
    profit: float
    swap: float
    time_open: int
    magic: int = 0
    comment: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_message(message: dict) -> "Position":
        return Position(
            ticket=int(message["ticket"]),
            symbol=message["symbol"],
            side=message["side"],
            volume=float(message["volume"]),
            price_open=float(message["price_open"]),
            sl=float(message.get("sl", 0.0)),
            tp=float(message.get("tp", 0.0)),
            profit=float(message.get("profit", 0.0)),
            swap=float(message.get("swap", 0.0)),
            time_open=int(message.get("time_open", 0)),
            magic=int(message.get("magic", 0)),
            comment=str(message.get("comment", "") or ""),
        )
