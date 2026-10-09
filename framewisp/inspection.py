"""Bounded, read-only AT-SPI observations on a headless session's private bus."""

# pyright: reportMissingModuleSource=false

import json
import math
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread, Timer
from typing import cast

from gi.repository import Gio, GLib

from framewisp.display_bounds import Rect, convert_bounds, match_window, read_windows
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
    timeout: float = 5.0

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
    longest_call_ms: float = 0
    app_unresponsive: bool = False
    error_context: str = ""

    def __init__(
        self, address: str, timeout: float, cancelled: Callable[[], bool] | None = None
    ):
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout
        self.cancel = Gio.Cancellable()
        self.timer = Timer(timeout, self.cancel.cancel)
        self.timer.daemon = True
        self.timer.start()
        self.finished = Event()
        self.watcher: Thread | None = None
        if cancelled is not None:

            def watch() -> None:
                while not self.finished.wait(0.02):
                    if cancelled():
                        self.cancel.cancel()
                        return

            self.watcher = Thread(target=watch, daemon=True)
            self.watcher.start()
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
            self.finish()
            raise

    def finish(self) -> None:
        self.timer.cancel()
        self.finished.set()
        if self.watcher is not None:
            self.watcher.join()

    def close(self) -> None:
        self.finish()
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
        started = time.monotonic()
        try:
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
        except GLib.Error:
            duration = time.monotonic() - started
            self.error_context = (
                f"{ref[0]} {ref[1]} {interface}.{method} "
                f"({duration * 1000:.3f} ms): "
            )
            # A failed app call consuming most of the whole query is evidence
            # of an unresponsive app, not proof that the process has hung.
            self.app_unresponsive = (
                ref[0] not in (ROOT[0], "org.freedesktop.DBus")
                and duration >= self.timeout * 0.8
                and time.monotonic() >= self.deadline
            )
            raise
        finally:
            self.longest_call_ms = max(
                self.longest_call_ms, (time.monotonic() - started) * 1000
            )

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


def inspect_session(
    session: Path, query: Query, *, cancelled: Callable[[], bool] | None = None
) -> dict[str, object]:
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
    return inspect_bus(
        expected, query, cancelled=cancelled, runtime=Path(state["runtime_directory"])
    )


def component_bounds(
    bus: Bus,
    ref: tuple[str, str],
    cache: dict[tuple[str, str], Rect | None],
) -> Rect | None:
    if ref not in cache:
        interfaces = cast(list[str], bus.call(ref, ACCESSIBLE, "GetInterfaces"))
        rect = (
            Rect(
                *cast(
                    tuple[int, int, int, int],
                    bus.call(
                        ref, "org.a11y.atspi.Component", "GetExtents", "(u)", (1,)
                    ),
                )
            )
            if "org.a11y.atspi.Component" in interfaces
            else None
        )
        cache[ref] = (
            rect if rect is not None and rect.width > 0 and rect.height > 0 else None
        )
    return cache[ref]


def inspect_bus(
    address: str,
    query: Query,
    *,
    cancelled: Callable[[], bool] | None = None,
    runtime: Path | None = None,
) -> dict[str, object]:
    started = time.monotonic()
    deadline = started + query.timeout
    before, mapping_error = (
        read_windows(runtime, deadline, cancelled)
        if runtime is not None
        else ([], "private-compositor-unavailable")
    )
    # Keep enclosing windows: an embedded dialog has no compositor toplevel.
    roots: dict[tuple[str, str], tuple[tuple[tuple[str, str], str], ...]] = {}
    parents: dict[tuple[str, str], tuple[str, str]] = {}
    pids: dict[str, int] = {}
    geometries: dict[tuple[str, str], Rect | None] = {}
    conversions: list[tuple[dict[str, object], Rect, Rect, tuple[str, ...], int]] = []
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
        bus = (
            Bus(address, max(0.001, deadline - time.monotonic()))
            if cancelled is None
            else Bus(address, max(0.001, deadline - time.monotonic()), cancelled)
        )
        # Stall classification uses the full query budget, including compositor reads.
        bus.timeout = query.timeout
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
                parents[ref] = parent
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
                full_name = cast(str, bus.prop(ref, ACCESSIBLE, "Name"))
                name = clipped(full_name)
                if role.casefold() in {"frame", "dialog", "window"}:
                    roots[ref] = ((ref, full_name),) + roots.get(parent, ())
                elif parent in roots:
                    roots[ref] = roots[parent]
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
                    "display_bounds": None,
                    "display_bounds_reason": "bounds-unavailable",
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
                        bounds = Rect(x, y, width, height)
                        node["bounds"] = bounds.as_bounds("window")
                        node["display_bounds_reason"] = mapping_error
                        if mapping_error is None:
                            root = roots.get(ref)
                            if root is None:
                                node["display_bounds_reason"] = (
                                    "accessible-window-not-found"
                                )
                            else:
                                node["display_bounds_reason"] = (
                                    "window-metadata-unavailable"
                                )
                                try:
                                    if ref[0] not in pids:
                                        pids[ref[0]] = cast(
                                            int,
                                            bus.call(
                                                (
                                                    "org.freedesktop.DBus",
                                                    "/org/freedesktop/DBus",
                                                ),
                                                "org.freedesktop.DBus",
                                                "GetConnectionUnixProcessID",
                                                "(s)",
                                                (ref[0],),
                                            ),
                                        )
                                    geometries[ref] = bounds

                                    target = ref
                                    child = bounds
                                    consistent = True
                                    while target != root[0][0]:
                                        target = parents[target]
                                        rect = component_bounds(bus, target, geometries)
                                        if rect is not None:
                                            consistent = consistent and rect.contains(
                                                child
                                            )
                                            child = rect
                                    if not consistent:
                                        node["display_bounds_reason"] = (
                                            "bounds-outside-parent"
                                        )
                                        continue
                                    titles = tuple(title for _, title in root)
                                    selected = match_window(
                                        pids[ref[0]], titles, before
                                    )
                                    if isinstance(selected, str):
                                        node["display_bounds_reason"] = selected
                                        continue
                                    toplevel = component_bounds(
                                        bus, root[selected.index][0], geometries
                                    )
                                except GLib.Error:
                                    if (
                                        bus.cancel.is_cancelled()
                                        or time.monotonic() >= bus.deadline
                                    ):
                                        raise
                                    # Conversion metadata is optional; readable control fields remain complete.
                                    continue
                                if toplevel is not None:
                                    conversions.append(
                                        (node, bounds, toplevel, titles, pids[ref[0]])
                                    )
            except GLib.Error as error:
                message = f"{bus.error_context}{error}"[:TEXT_LIMIT]
                if bus.cancel.is_cancelled() or time.monotonic() >= bus.deadline:
                    # Preserve the terminal failure even after earlier object errors.
                    if len(errors) == 5:
                        errors.pop()
                    errors.append(message)
                    status = "timeout"
                    reasons.add("timeout")
                    break
                reasons.add("unavailable-object")
                if len(errors) < 5:
                    errors.append(message)
        if applications == 0:
            status = "unsupported"
            reasons.add("no-accessible-applications")
    except GLib.Error as error:
        status = (
            "timeout" if time.monotonic() - started >= query.timeout else "unavailable"
        )
        reasons.add(status)
        context = bus.error_context if bus is not None else ""
        errors.append(f"{context}{error}"[:TEXT_LIMIT])
    finally:
        if bus is not None:
            bus.close()
    if runtime is not None and conversions:
        after, mapping_error = read_windows(runtime, deadline, cancelled)
        for node, bounds, toplevel, titles, pid in conversions:
            if mapping_error is not None:
                node["display_bounds_reason"] = mapping_error
            else:
                node["display_bounds"], node["display_bounds_reason"] = convert_bounds(
                    bounds,
                    toplevel,
                    pid,
                    titles[0],
                    before,
                    after,
                    ancestor_titles=titles[1:],
                )
    if status == "timeout" and bus is not None and bus.app_unresponsive:
        reasons.add("app-unresponsive")
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
        "hints": retry_hints(query, reasons),
        "longest_call_ms": round(bus.longest_call_ms, 3) if bus is not None else 0,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
    }


def retry_hints(query: Query, reasons: set[str]) -> list[str]:
    hints: list[str] = []
    for reason, current, maximum in (
        ("max-depth", query.max_depth, 32),
        ("max-nodes", query.max_nodes, 4096),
        ("limit", query.limit, 100),
    ):
        if reason in reasons:
            if current < maximum:
                hints.append(f"Raise --{reason} (maximum {maximum}).")
            else:
                hints.append(
                    f"--{reason} is already at its maximum ({maximum}); use a screenshot if the partial result is insufficient."
                )
    if "limit" in reasons:
        hints.append("Narrow --role, --name, or --text to return fewer matches.")
    if "app-unresponsive" in reasons:
        hints.append(
            "An app call consumed at least 80% of the query budget. Wait and retry, or take a screenshot; raising limits may not help."
        )
    elif "timeout" in reasons:
        if query.timeout < 10:
            hints.append("Raise --timeout (maximum 10 seconds).")
        else:
            hints.append(
                "--timeout is already at its maximum (10 seconds); wait and retry, or take a screenshot."
            )
        hints.append(
            "See longest_call_ms for the slowest call; a timeout alone does not prove the app is responsive."
        )
    if "unavailable-object" in reasons or "stale-object" in reasons:
        hints.append(
            "The accessible tree changed or an object could not be read. Retry once after the UI settles; use a screenshot if it persists."
        )
    if "text-limit" in reasons:
        hints.append(
            "Accessible text is capped at 1024 characters; use a screenshot for omitted content."
        )
    if "action-limit" in reasons:
        hints.append("Action names are capped at 16 per object.")
    return hints
