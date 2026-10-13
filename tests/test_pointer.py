import json
import socket
import struct
import time
from collections.abc import Iterator
from pathlib import Path
from threading import Thread

import pytest

from framewisp.errors import SessionError
from framewisp.pointer import cursor_position


@pytest.fixture
def compositor(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Path]:
    payload: object = request.param
    failures: list[BaseException] = []
    with socket.socket(socket.AF_UNIX) as server:
        server.settimeout(2)
        server.bind(str(tmp_path / "sway-ipc.test.sock"))
        server.listen()

        def serve() -> None:
            try:
                with server.accept()[0] as connection:
                    connection.settimeout(2)
                    header = bytearray()
                    while len(header) < 14:
                        chunk = connection.recv(14 - len(header))
                        assert chunk
                        header.extend(chunk)
                    assert bytes(header) == b"i3-ipc" + struct.pack("=II", 0, 101)
                    if payload is None:
                        assert connection.recv(1) == b""
                    else:
                        data = json.dumps(payload).encode()
                        reply = b"i3-ipc" + struct.pack("=II", len(data), 101) + data
                        # Exercise partial headers and partial JSON bodies.
                        for part in (reply[:5], reply[5:16], reply[16:]):
                            if part:
                                connection.sendall(part)
                                time.sleep(0.005)
            except BaseException as error:
                failures.append(error)

        thread = Thread(target=serve)
        thread.start()
        try:
            yield tmp_path
        finally:
            thread.join(timeout=3)
            assert not thread.is_alive()
            assert not failures


@pytest.mark.parametrize(
    "compositor,expected",
    [
        ([{"name": "seat0", "cursor": {"x": 0, "y": 0}}], (0, 0)),
        ([{"name": "seat0", "cursor": {"x": 123.6, "y": 201.2}}], (124, 201)),
        ([{"name": "seat0", "cursor": {"x": -1, "y": 50}}], None),
    ],
    indirect=["compositor"],
)
def test_live_cursor_coordinates(
    compositor: Path, expected: tuple[int, int] | None
) -> None:
    assert cursor_position(compositor, lambda: False) == expected


@pytest.mark.parametrize(
    "compositor",
    [
        [],
        [{"name": "seat0"}],
        [{"name": "other", "cursor": {"x": 1, "y": 1}}],
        [{"name": "seat0", "cursor": {"x": True, "y": 1}}],
        [{"name": "seat0", "cursor": {"x": float("nan"), "y": 1}}],
        [{"name": "seat0", "cursor": {"x": "1", "y": 1}}],
    ],
    indirect=True,
)
def test_invalid_cursor_reply_is_actionable(compositor: Path) -> None:
    with pytest.raises(SessionError, match="--move-duration 0"):
        cursor_position(compositor, lambda: False)


@pytest.mark.parametrize("compositor", [None], indirect=True)
def test_cancel_cursor_query_closes_connection(compositor: Path) -> None:
    started = time.monotonic()
    with pytest.raises(InterruptedError, match="cancelled"):
        cursor_position(compositor, lambda: time.monotonic() - started > 0.05)
    assert time.monotonic() - started < 0.5
