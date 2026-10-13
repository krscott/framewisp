"""Bounded IPC queries to a session's private compositor."""

import json
import socket
import struct
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast


def request(
    runtime: Path,
    kind: int,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None = None,
) -> object:
    """Never consult the caller's SWAYSOCK or launch a desktop command."""
    paths = list(runtime.glob("sway-ipc.*.sock"))
    if len(paths) != 1 or not paths[0].is_socket():
        raise ValueError("Private compositor socket is unavailable or ambiguous")

    def remaining() -> float:
        if cancelled and cancelled():
            raise InterruptedError("Input cancelled during compositor query")
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Compositor query timed out")
        return min(seconds, 0.02)

    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(remaining())
        connection.connect(str(paths[0]))
        connection.settimeout(remaining())
        connection.sendall(b"i3-ipc" + struct.pack("=II", 0, kind))

        def receive(length: int) -> bytes:
            result = bytearray()
            while len(result) < length:
                connection.settimeout(remaining())
                try:
                    chunk = connection.recv(length - len(result))
                except TimeoutError:
                    continue
                if not chunk:
                    raise ValueError("Incomplete Sway reply")
                result.extend(chunk)
            return bytes(result)

        header = receive(14)
        length, reply_kind = struct.unpack("=II", header[6:])
        if header[:6] != b"i3-ipc" or reply_kind != kind or length > 1024 * 1024:
            raise ValueError("Invalid or oversized Sway reply")
        return cast(object, json.loads(receive(length)))
