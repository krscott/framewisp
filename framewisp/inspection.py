"""Bounded, read-only AT-SPI observations on a headless session's private bus."""

# pyright: reportMissingModuleSource=false

import json
import math
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Timer
from typing import cast

from gi.repository import Gio, GLib

from framewisp.errors import SessionError

ACCESSIBLE = "org.a11y.atspi.Accessible"
ROOT: tuple[str, str] = ("org.a11y.atspi.Registry", "/org/a11y/atspi/accessible/root")
STATES = (
    "invalid active armed busy checked collapsed defunct editable enabled expandable "
    "expanded focusable focused has-tooltip horizontal iconified modal multi-line "
    "multiselectable opaque pressed resizable selectable selected sensitive showing "
    "single-line stale transient vertical visible manages-descendants indeterminate "
    "required truncated animated invalid-entry supports-autocompletion selectable-text "
    "is-default visited checkable has-popup read-only"
).split()
TEXT_LIMIT = 1024


@dataclass(frozen=True, kw_only=True)
class Query:
    role: str | None = None
    name: str | None = None
    text: str | None = None
    max_depth: int = 8
    limit: int = 20
    max_nodes: int = 256
    timeout: float = 2.0

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("max-depth", self.max_depth, 32),
            ("limit", self.limit, 100),
            ("max-nodes", self.max_nodes, 4096),
        ):
            if not 1 <= value <= maximum:
                raise ValueError(f"--{name} must be between 1 and {maximum}")
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 10:
            raise ValueError("--timeout must be greater than 0 and at most 10 seconds")
        if any(
            value is not None and len(value) > TEXT_LIMIT
            for value in (self.role, self.name, self.text)
        ):
            raise ValueError(
                f"inspection filters must be at most {TEXT_LIMIT} characters"
            )


class Bus:
    def __init__(self, address: str, timeout: float):
        self.deadline = time.monotonic() + timeout
        self.cancel = Gio.Cancellable()
        self.timer = Timer(timeout, self.cancel.cancel)
        self.timer.daemon = True
        self.timer.start()
        try:
            self.connection = Gio.DBusConnection.new_for_address_sync(
                address,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None,
                self.cancel,
            )
            self.connection.set_exit_on_close(False)
        except BaseException:
            self.timer.cancel()
            raise

    def close(self) -> None:
        self.timer.cancel()
        try:
            self.connection.close_sync(None)
        except GLib.Error as error:
            # Session shutdown can close the bus before or during client cleanup.
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CLOSED):
                raise

    def call(
        self,
        ref: tuple[str, str],
        interface: str,
        method: str,
        signature: str = "()",
        arguments: tuple[object, ...] = (),
    ) -> object:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.cancel.cancel()
        result = self.connection.call_sync(
            ref[0],
            ref[1],
            interface,
            method,
            GLib.Variant(signature, arguments),
            None,
            Gio.DBusCallFlags.NO_AUTO_START,
            max(1, math.ceil(remaining * 1000)),
            self.cancel,
        )
        return result.unpack()[0]

    def prop(self, ref: tuple[str, str], interface: str, name: str) -> object:
        return self.call(
            ref, "org.freedesktop.DBus.Properties", "Get", "(ss)", (interface, name)
        )


def wait_for_registry(address: str, stop: Event) -> None:
    bus = Bus(address, 10)
    try:
        while not stop.is_set():
            if bus.call(
                ("org.freedesktop.DBus", "/org/freedesktop/DBus"),
                "org.freedesktop.DBus",
                "NameHasOwner",
                "(s)",
                (ROOT[0],),
            ):
                return
            stop.wait(0.02)
    except GLib.Error as error:
        raise SessionError(
            f"Accessibility registry did not start; see registry.log: {error}"
        ) from error
    finally:
        bus.close()


def inspect_session(session: Path, query: Query) -> dict[str, object]:
    state = json.loads((session / "session.json").read_text())
    if state.get("kind") == "attached":
        raise SessionError(
            "inspect supports headless sessions only; use screenshot for attached desktops"
        )
    if state.get("inspection_protocol") != 1:
        raise SessionError(
            "Session has no inspection support; start a new headless session"
        )
    # Only the exact private socket created by this runner is eligible. Never fall
    # back to the caller's desktop bus or AT_SPI_BUS_ADDRESS environment variable.
    expected = f"unix:path={Path(state['runtime_directory']) / 'accessibility.sock'}"
    if state.get("accessibility_bus") != expected:
        raise SessionError("Invalid private accessibility bus in session metadata")
    return inspect_bus(expected, query)


def inspect_bus(address: str, query: Query) -> dict[str, object]:
    started = time.monotonic()
    snapshot = uuid.uuid4().hex
    matches: list[dict[str, object]] = []
    reasons: set[str] = set()
    errors: list[str] = []
    visited: set[tuple[str, str]] = set()
    applications = 0
    status = "ok"
    bus: Bus | None = None

    def clipped(value: str) -> str:
        if len(value) > TEXT_LIMIT:
            reasons.add("text-limit")
        return value[:TEXT_LIMIT]

    try:
        bus = Bus(address, query.timeout)
        applications = cast(int, bus.prop(ROOT, ACCESSIBLE, "ChildCount"))
        # Queue parent/index pairs instead of fetching an unbounded GetChildren reply.
        pending = deque(
            (ROOT, index, 1) for index in range(min(applications, query.max_nodes))
        )
        if applications > query.max_nodes:
            reasons.add("max-nodes")
        while pending:
            if len(visited) >= query.max_nodes:
                reasons.add("max-nodes")
                break
            if len(matches) >= query.limit:
                reasons.add("limit")
                break
            parent, index, depth = pending.popleft()
            try:
                ref = cast(
                    tuple[str, str],
                    bus.call(parent, ACCESSIBLE, "GetChildAtIndex", "(i)", (index,)),
                )
                if ref[1] == "/org/a11y/atspi/null":
                    reasons.add("stale-object")
                    continue
                if ref in visited:
                    continue
                visited.add(ref)
                bits = cast(list[int], bus.call(ref, ACCESSIBLE, "GetState"))
                states = [
                    name
                    for i, name in enumerate(STATES)
                    if i // 32 < len(bits) and bits[i // 32] & (1 << (i % 32))
                ]
                if "defunct" in states or "stale" in states:
                    reasons.add("stale-object")
                    continue
                role = clipped(cast(str, bus.call(ref, ACCESSIBLE, "GetRoleName")))
                name = clipped(cast(str, bus.prop(ref, ACCESSIBLE, "Name")))
                count = cast(int, bus.prop(ref, ACCESSIBLE, "ChildCount"))
                if count > 0:
                    if depth >= query.max_depth:
                        reasons.add("max-depth")
                    else:
                        capacity = max(0, query.max_nodes - len(visited) - len(pending))
                        pending.extend(
                            (ref, i, depth + 1) for i in range(min(count, capacity))
                        )
                        if count > capacity:
                            reasons.add("max-nodes")
                if query.role is not None and query.role.casefold() != role.casefold():
                    continue
                if (
                    query.name is not None
                    and query.name.casefold() not in name.casefold()
                ):
                    continue
                node: dict[str, object] = {
                    "id": f"{snapshot}:{len(matches)}",
                    "role": role,
                    "name": name,
                    "text": None,
                    "states": states,
                    "actions": [],
                    "bounds": None,
                    "value": None,
                    "depth": depth,
                }
                # Append first so optional-interface failures retain readable fields.
                if query.text is None:
                    matches.append(node)
                interfaces = cast(list[str], bus.call(ref, ACCESSIBLE, "GetInterfaces"))
                text: str | None = None
                if "org.a11y.atspi.Text" in interfaces:
                    length = cast(
                        int, bus.prop(ref, "org.a11y.atspi.Text", "CharacterCount")
                    )
                    text = clipped(
                        cast(
                            str,
                            bus.call(
                                ref,
                                "org.a11y.atspi.Text",
                                "GetText",
                                "(ii)",
                                (0, min(length, TEXT_LIMIT)),
                            ),
                        )
                    )
                    if length > TEXT_LIMIT:
                        reasons.add("text-limit")
                if query.text is not None and (
                    text is None or query.text.casefold() not in text.casefold()
                ):
                    continue
                node["text"] = text
                if query.text is not None:
                    matches.append(node)
                if "org.a11y.atspi.Action" in interfaces:
                    n = cast(int, bus.prop(ref, "org.a11y.atspi.Action", "NActions"))
                    node["actions"] = [
                        clipped(
                            cast(
                                str,
                                bus.call(
                                    ref, "org.a11y.atspi.Action", "GetName", "(i)", (i,)
                                ),
                            )
                        )
                        for i in range(min(n, 16))
                    ]
                    if n > 16:
                        reasons.add("action-limit")
                if "org.a11y.atspi.Value" in interfaces:
                    value = cast(
                        float, bus.prop(ref, "org.a11y.atspi.Value", "CurrentValue")
                    )
                    node["value"] = value if math.isfinite(value) else None
                if "org.a11y.atspi.Component" in interfaces:
                    x, y, width, height = cast(
                        tuple[int, int, int, int],
                        bus.call(
                            ref, "org.a11y.atspi.Component", "GetExtents", "(u)", (1,)
                        ),
                    )
                    if x >= 0 and y >= 0 and width > 0 and height > 0:
                        node["bounds"] = {
                            "x": x,
                            "y": y,
                            "width": width,
                            "height": height,
                            "coordinate_space": "window",
                        }
            except GLib.Error as error:
                if bus.cancel.is_cancelled() or time.monotonic() >= bus.deadline:
                    status = "timeout"
                    reasons.add("timeout")
                    break
                reasons.add("unavailable-object")
                if len(errors) < 5:
                    errors.append(str(error)[:TEXT_LIMIT])
        if applications == 0:
            status = "unsupported"
            reasons.add("no-accessible-applications")
    except GLib.Error as error:
        status = (
            "timeout" if time.monotonic() - started >= query.timeout else "unavailable"
        )
        reasons.add(status)
        errors.append(str(error)[:TEXT_LIMIT])
    finally:
        if bus is not None:
            bus.close()
    if status == "ok" and reasons:
        status = "partial"
    return {
        "schema_version": 1,
        "snapshot_id": snapshot,
        "status": status,
        "matches": matches,
        "match_count": len(matches),
        "visited": len(visited),
        "applications": applications,
        "reasons": sorted(reasons),
        "errors": errors,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
    }
