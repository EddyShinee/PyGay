"""Wire format: one JSON object per line (newline-delimited JSON).

Kept in its own module so the framing/encoding rules have a single
place to change if the protocol ever needs to evolve (e.g. length-
prefixed frames instead of newlines).
"""
import json
from typing import Any


def encode(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def decode_lines(buffer: bytes) -> tuple[list[dict[str, Any]], bytes]:
    """Split buffer on newlines. Returns (parsed messages, leftover bytes)."""
    *lines, rest = buffer.split(b"\n")
    messages = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        messages.append(json.loads(line))
    return messages, rest
