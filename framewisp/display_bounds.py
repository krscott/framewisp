"""Conservative conversion of accessible window bounds using private Sway IPC."""

import json
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def contains(self, other: "Rect") -> bool:
        return (
            self.x <= other.x
            and self.y <= other.y
            and other.x + other.width <= self.x + self.width
            and other.y + other.height <= self.y + self.height
        )

    def as_bounds(self, coordinate_space: str) -> dict[str, int | str]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "coordinate_space": coordinate_space,
        }


@dataclass(frozen=True)
class Window:
    id: int
    pid: int
    title: str | None
    content: Rect
    visible: bool
    transform_supported: bool
    output: Rect


def rectangle(value: object) -> Rect:
    if not isinstance(value, dict):
        raise ValueError("Invalid Sway rectangle")
    fields = [
        cast(dict[str, object], value).get(key) for key in ("x", "y", "width", "height")
    ]
    if any(type(field) is not int for field in fields):
        raise ValueError("Invalid Sway rectangle")
    return Rect(*cast(list[int], fields))


def windows_from_tree(tree: object) -> list[Window]:
    windows: list[Window] = []
    pending = [(tree, False, Rect(0, 0, 0, 0))]
    while pending:
        item, supported, output = pending.pop()
        if not isinstance(item, dict):
            raise ValueError("Invalid Sway node")
        node = cast(dict[str, object], item)
        if node.get("type") == "output":
            output = rectangle(node.get("rect"))
            supported = (
                node.get("scale") == 1
                and node.get("transform") == "normal"
                and output.x == output.y == 0
            )
        pid = node.get("pid")
        if type(pid) is int and pid > 0:
            identity = node.get("id")
            title = node.get("name")
            if type(identity) is not int or not isinstance(title, (str, type(None))):
                raise ValueError("Invalid Sway window")
            outer = rectangle(node.get("rect"))
            inner = rectangle(node.get("window_rect"))
            windows.append(
                Window(
                    identity,
                    pid,
                    title,
                    Rect(
                        outer.x + inner.x, outer.y + inner.y, inner.width, inner.height
                    ),
                    node.get("visible") is True,
                    supported,
                    output,
                )
            )
        for key in ("nodes", "floating_nodes"):
            children = node.get(key, [])
            if not isinstance(children, list):
                raise ValueError("Invalid Sway children")
            pending.extend(
                (child, supported, output) for child in cast(list[object], children)
            )
    return windows


def read_windows(
    runtime: Path, deadline: float, cancelled: Callable[[], bool] | None
) -> tuple[list[Window], str | None]:
    """Never consult SWAYSOCK or launch a command against the caller's desktop."""
    deadline = min(deadline, time.monotonic() + 0.25)
    try:
        paths = list(runtime.glob("sway-ipc.*.sock"))
        if len(paths) != 1 or not paths[0].is_socket():
            return [], "private-compositor-unavailable"
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            connection.connect(str(paths[0]))
            connection.sendall(b"i3-ipc" + struct.pack("=II", 0, 4))

            def receive(length: int) -> bytes:
                result = bytearray()
                while len(result) < length:
                    if time.monotonic() >= deadline or (cancelled and cancelled()):
                        raise TimeoutError("Compositor query timed out")
                    connection.settimeout(min(0.02, deadline - time.monotonic()))
                    try:
                        chunk = connection.recv(length - len(result))
                    except TimeoutError:
                        continue
                    if not chunk:
                        raise ValueError("Incomplete Sway reply")
                    result.extend(chunk)
                return bytes(result)

            header = receive(14)
            length, kind = struct.unpack("=II", header[6:])
            if header[:6] != b"i3-ipc" or kind != 4 or length > 1024 * 1024:
                raise ValueError("Invalid or oversized Sway reply")
            return windows_from_tree(json.loads(receive(length))), None
    except (OSError, ValueError, RecursionError):
        return [], "private-compositor-unavailable"


def convert_bounds(
    bounds: Rect,
    toplevel: Rect,
    pid: int,
    title: str,
    before: list[Window],
    after: list[Window],
    *,
    ancestors: tuple[tuple[Rect, str], ...] = (),
) -> tuple[dict[str, int | str] | None, str | None]:
    def candidates(windows: list[Window], title: str) -> list[Window]:
        matches = [window for window in windows if window.pid == pid]
        # Untitled accessibles can have a compositor title supplied by GTK.
        # A missing title is usable only when the process has one window.
        if title:
            matches = [window for window in matches if window.title == title]
        return matches

    roots = ((toplevel, title), *ancestors)

    def select(windows: list[Window]) -> tuple[Rect, str, list[Window]]:
        for rect, name in roots:
            matches = candidates(windows, name)
            if matches:
                return rect, name, matches
        return toplevel, title, []

    toplevel, title, matches = select(before)
    if not matches:
        return None, "window-not-found"
    if len(matches) != 1:
        return None, "ambiguous-window"
    window = matches[0]
    if select(after) != (toplevel, title, matches):
        return None, "window-changed"
    if not window.visible:
        return None, "window-not-visible"
    if not window.transform_supported:
        return None, "unsupported-display-transform"
    content = window.content
    if (
        toplevel.x != 0
        or toplevel.y != 0
        or toplevel.width != content.width
        or toplevel.height != content.height
    ):
        return None, "window-geometry-mismatch"
    if (
        bounds.x < 0
        or bounds.y < 0
        or bounds.width <= 0
        or bounds.height <= 0
        or bounds.x + bounds.width > content.width
        or bounds.y + bounds.height > content.height
    ):
        return None, "bounds-outside-window"
    display = Rect(
        content.x + bounds.x, content.y + bounds.y, bounds.width, bounds.height
    )
    if (
        display.x < 0
        or display.y < 0
        or display.x + display.width > window.output.width
        or display.y + display.height > window.output.height
    ):
        return None, "bounds-outside-display"
    return display.as_bounds("display"), None
