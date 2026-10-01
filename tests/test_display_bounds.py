import json
import socket
import struct
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from threading import Thread

import pytest

from framewisp.display_bounds import (
    Rect,
    Window,
    convert_bounds,
    read_windows,
    windows_from_tree,
)


@pytest.fixture
def window() -> Window:
    return Window(
        7, 123, "Dialog", Rect(500, 325, 600, 240), True, True, Rect(0, 0, 1600, 900)
    )


def test_title_bar_offset_and_multiple_windows(window: Window) -> None:
    other = replace(window, id=8, title="Main", content=Rect(0, 0, 1600, 900))
    result, reason = convert_bounds(
        Rect(390, 216, 80, 23),
        Rect(0, 0, 600, 240),
        123,
        "Dialog",
        [other, window],
        [other, window],
    )
    assert reason is None
    assert result == {
        "x": 890,
        "y": 541,
        "width": 80,
        "height": 23,
        "coordinate_space": "display",
    }


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"content": Rect(600, 325, 600, 240)}, "window-changed"),
        ({"title": "Renamed"}, "window-changed"),
        ({"id": 8}, "window-changed"),
        ({"visible": False}, "window-changed"),
    ],
)
def test_changed_window_is_not_a_pointer_target(
    window: Window, change: dict[str, object], reason: str
) -> None:
    result, actual = convert_bounds(Rect(10, 20, 80, 23), Rect(0, 0, 600, 240), 123, "Dialog", [window], [replace(window, **change)])  # type: ignore[arg-type]
    assert result is None
    assert actual == reason


@pytest.mark.parametrize(
    "case,reason",
    [
        ("missing", "window-not-found"),
        ("duplicate", "ambiguous-window"),
        ("hidden", "window-not-visible"),
        ("scaled", "unsupported-display-transform"),
        ("resize", "window-geometry-mismatch"),
        ("outside", "bounds-outside-window"),
    ],
)
def test_unreliable_mapping_has_a_reason(
    window: Window, case: str, reason: str
) -> None:
    windows = [window]
    root = Rect(0, 0, 600, 240)
    bounds = Rect(10, 20, 80, 23)
    if case == "missing":
        windows = []
    elif case == "duplicate":
        windows.append(replace(window, id=8))
    elif case == "hidden":
        windows = [replace(window, visible=False)]
    elif case == "scaled":
        windows = [replace(window, transform_supported=False)]
    elif case == "resize":
        root = Rect(0, 0, 800, 600)
    elif case == "outside":
        bounds = Rect(580, 20, 80, 23)
    result, actual = convert_bounds(bounds, root, 123, "Dialog", windows, windows)
    assert result is None
    assert actual == reason


@pytest.fixture
def tree() -> dict[str, object]:
    return {
        "type": "output",
        "scale": 1.0,
        "transform": "normal",
        "rect": {"x": 0, "y": 0, "width": 1600, "height": 900},
        "floating_nodes": [
            {
                "id": 7,
                "pid": 123,
                "name": "Dialog",
                "visible": True,
                "rect": {"x": 498, "y": 300, "width": 604, "height": 267},
                "window_rect": {"x": 2, "y": 25, "width": 600, "height": 240},
            }
        ],
    }


def test_tree_includes_floating_client_origin(
    tree: dict[str, object], window: Window
) -> None:
    assert windows_from_tree(tree) == [window]


@pytest.fixture
def ipc_socket(tmp_path: Path) -> Iterator[socket.socket]:
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(tmp_path / "sway-ipc.1000.123.sock"))
        listener.listen()
        listener.settimeout(2)
        yield listener


@pytest.mark.parametrize(
    "reply", ["fragmented", "oversized", "invalid", "disconnect", "stalled"]
)
def test_private_ipc_is_bounded_and_validated(
    tmp_path: Path,
    ipc_socket: socket.socket,
    tree: dict[str, object],
    reply: str,
    window: Window,
) -> None:
    def serve() -> None:
        with ipc_socket.accept()[0] as connection:
            assert connection.recv(14) == b"i3-ipc" + struct.pack("=II", 0, 4)
            if reply == "stalled":
                time.sleep(0.3)
                return
            if reply == "disconnect":
                return
            payload = json.dumps(tree).encode() if reply == "fragmented" else b"null"
            header = b"i3-ipc" + struct.pack(
                "=II", 2**24 if reply == "oversized" else len(payload), 4
            )
            for byte in header + (b"" if reply == "oversized" else payload):
                connection.sendall(bytes([byte]))

    server = Thread(target=serve)
    server.start()
    started = time.monotonic()
    try:
        windows, reason = read_windows(tmp_path, started + 1, None)
        assert time.monotonic() - started < 0.6
        if reply == "fragmented":
            assert reason is None
            assert windows == [window]
        else:
            assert windows == []
            assert reason == "private-compositor-unavailable"
    finally:
        server.join(timeout=2)
    assert not server.is_alive()


def test_no_host_compositor_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWAYSOCK", "/run/user/1000/host.sock")
    assert read_windows(tmp_path, time.monotonic() + 1, None) == (
        [],
        "private-compositor-unavailable",
    )


@pytest.mark.parametrize(
    "content",
    [Rect(-300, 100, 600, 240), Rect(1590, 100, 600, 240), Rect(500, 890, 600, 240)],
)
def test_offscreen_control_is_not_a_pointer_target(
    window: Window, content: Rect
) -> None:
    window = replace(window, content=content)
    result, reason = convert_bounds(
        Rect(10, 20, 80, 23), Rect(0, 0, 600, 240), 123, "Dialog", [window], [window]
    )
    assert result is None
    assert reason == "bounds-outside-display"


def test_visible_control_in_partly_offscreen_window(window: Window) -> None:
    window = replace(window, content=Rect(-100, 100, 600, 240))
    result, reason = convert_bounds(
        Rect(390, 216, 80, 23), Rect(0, 0, 600, 240), 123, "Dialog", [window], [window]
    )
    assert reason is None
    assert result is not None and result["x"] == 290
