"""Exercise the real SNI and dbusmenu wire interfaces on an isolated session bus."""

# pyright: reportMissingModuleSource=false

import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread

import pytest
from gi.repository import Gio, GLib

from framewisp.attach import AttachedInput
from framewisp.desktop import detach_desktop
from framewisp.portal import DesktopPortal, dispatch_events
from framewisp.tray import (
    ITEM,
    ITEM_PATH,
    MENU,
    MENU_PATH,
    WATCHER,
    SharingIndicator,
)


@pytest.fixture
def buses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Gio.DBusConnection, Gio.DBusConnection]]:
    config = tmp_path / "bus.conf"
    config.write_text(
        "<busconfig><type>session</type><listen>unix:tmpdir=/tmp</listen>"
        '<auth>EXTERNAL</auth><policy context="default">'
        '<allow send_destination="*"/><allow receive_sender="*"/>'
        '<allow own="*"/></policy></busconfig>'
    )
    daemon = subprocess.Popen(
        ["dbus-daemon", f"--config-file={config}", "--nofork", "--print-address"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert daemon.stdout is not None
    address = daemon.stdout.readline().strip()
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)
    connections = [
        Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )
        for _ in range(2)
    ]
    try:
        yield connections[0], connections[1]
    finally:
        for bus in connections:
            if not bus.is_closed():
                bus.close_sync(None)
        daemon.terminate()
        daemon.wait(timeout=5)
        daemon.stdout.close()


def until(predicate: Callable[[], bool], timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        dispatch_events()
        assert time.monotonic() < deadline
        time.sleep(0.001)


def call(
    bus: Gio.DBusConnection,
    destination: str,
    path: str,
    interface: str,
    method: str,
    parameters: GLib.Variant,
) -> GLib.Variant:
    results: list[GLib.Variant | GLib.Error] = []

    def completed(source: object, result: Gio.AsyncResult, data: object) -> None:
        try:
            results.append(bus.call_finish(result))
        except GLib.Error as error:
            results.append(error)

    bus.call(
        destination,
        path,
        interface,
        method,
        parameters,
        None,
        Gio.DBusCallFlags.NONE,
        1000,
        None,
        completed,
        None,
    )
    until(lambda: bool(results))
    result = results[0]
    if isinstance(result, GLib.Error):
        raise result
    return result


@pytest.fixture
def indicator(
    buses: tuple[Gio.DBusConnection, Gio.DBusConnection],
) -> Iterator[SharingIndicator]:
    item = SharingIndicator(buses[0], Event())
    item.start()
    try:
        yield item
    finally:
        item.close()


# R-ATTACH-3, R-ATTACH-4
def test_menu_and_active_text(
    buses: tuple[Gio.DBusConnection, Gio.DBusConnection], indicator: SharingIndicator
) -> None:
    owner, client = buses
    destination = owner.get_unique_name()
    assert destination is not None
    properties = call(
        client,
        destination,
        ITEM_PATH,
        "org.freedesktop.DBus.Properties",
        "GetAll",
        GLib.Variant("(s)", (ITEM,)),
    ).unpack()[0]
    assert properties["Status"] == "Active"
    assert properties["Title"] == "framewisp desktop sharing is active"
    assert properties["ToolTip"][2] == "framewisp desktop sharing is active"
    assert properties["Menu"] == MENU_PATH
    assert properties["ItemIsMenu"]
    width, height, pixels = properties["IconPixmap"][0]
    assert width == height == 32 and len(pixels) == 32 * 32 * 4
    layout = call(
        client,
        destination,
        MENU_PATH,
        MENU,
        "GetLayout",
        GLib.Variant("(iias)", (0, -1, [])),
    ).unpack()
    assert layout[1][2][0][1]["label"] == "Stop sharing"
    call(
        client,
        destination,
        MENU_PATH,
        MENU,
        "Event",
        GLib.Variant("(isvu)", (1, "hovered", GLib.Variant("s", ""), 0)),
    )
    assert not indicator.stop.is_set()
    call(
        client,
        destination,
        MENU_PATH,
        MENU,
        "Event",
        GLib.Variant("(isvu)", (1, "clicked", GLib.Variant("s", ""), 0)),
    )
    assert indicator.stop.is_set()
    indicator.close()
    with pytest.raises(GLib.Error):
        call(
            client,
            destination,
            ITEM_PATH,
            "org.freedesktop.DBus.Properties",
            "GetAll",
            GLib.Variant("(s)", (ITEM,)),
        )


def test_no_host_diagnostic(
    indicator: SharingIndicator, capsys: pytest.CaptureFixture[str]
) -> None:
    until(lambda: indicator.available is False)
    message = capsys.readouterr().err
    assert "indicator unavailable" in message
    assert "framewisp --detach" in message and "Ctrl+C" in message
    assert not indicator.stop.is_set()


Register = Callable[[str, str, Gio.DBusMethodInvocation], None]


@contextmanager
def watcher_object(
    host: Gio.DBusConnection,
    register: Register,
    host_available: Callable[[], bool],
) -> Generator[None]:
    info = Gio.DBusNodeInfo.new_for_xml(
        """<node><interface name="org.kde.StatusNotifierWatcher">
      <method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
      <signal name="StatusNotifierHostRegistered"/>
      <property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
    </interface></node>"""
    ).interfaces[0]

    def method(
        bus: Gio.DBusConnection,
        sender: str,
        path: str,
        interface: str,
        method: str,
        parameters: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        register(sender, parameters.unpack()[0], invocation)

    def property(
        bus: Gio.DBusConnection, sender: str, path: str, interface: str, name: str
    ) -> GLib.Variant:
        return GLib.Variant("b", host_available())

    registration = host.register_object(
        "/StatusNotifierWatcher", info, method, property, None
    )
    try:
        yield
    finally:
        host.unregister_object(registration)


def own_watcher_name(host: Gio.DBusConnection) -> None:
    host.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "RequestName",
        GLib.Variant("(su)", (WATCHER, 0)),
        None,
        Gio.DBusCallFlags.NONE,
        1000,
        None,
    )


# R-ATTACH-3
@pytest.mark.parametrize("delay_registration", [False, True])
def test_watcher_registration_and_restart(
    buses: tuple[Gio.DBusConnection, Gio.DBusConnection],
    indicator: SharingIndicator,
    delay_registration: bool,
) -> None:
    owner, host = buses
    registrations: list[str] = []
    host_available = False
    pending: list[Gio.DBusMethodInvocation] = []

    def register(sender: str, path: str, invocation: Gio.DBusMethodInvocation) -> None:
        registrations.append(sender + path)
        if delay_registration:
            pending.append(invocation)
        else:
            invocation.return_value(None)

    with watcher_object(host, register, lambda: host_available):
        for count in (1, 2):
            own_watcher_name(host)
            if count == 1:
                until(
                    lambda: indicator.proxy is not None and indicator.available is False
                )
                assert not registrations
                host_available = True
                host.emit_signal(
                    None,
                    "/StatusNotifierWatcher",
                    WATCHER,
                    "StatusNotifierHostRegistered",
                    None,
                )
            if delay_registration:
                until(lambda: len(pending) == 1)
                assert indicator.registering and not indicator.registered
                host_available = False
                host.emit_signal(
                    None,
                    "/StatusNotifierWatcher",
                    WATCHER,
                    "StatusNotifierHostRegistered",
                    None,
                )

                def host_is_absent() -> bool:
                    assert indicator.proxy is not None
                    value = indicator.proxy.get_cached_property(
                        "IsStatusNotifierHostRegistered"
                    )
                    return value is not None and not value.unpack()

                until(host_is_absent)
                pending.pop().return_value(None)
                until(lambda: indicator.registered)
                assert indicator.available is False
                host_available = True
                host.emit_signal(
                    None,
                    "/StatusNotifierWatcher",
                    WATCHER,
                    "StatusNotifierHostRegistered",
                    None,
                )
            until(lambda: len(registrations) == count and indicator.available is True)
            assert registrations[-1] == str(owner.get_unique_name()) + ITEM_PATH
            host.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "ReleaseName",
                GLib.Variant("(s)", (WATCHER,)),
                None,
                Gio.DBusCallFlags.NONE,
                1000,
                None,
            )
            until(lambda: indicator.available is False)


# R-ATTACH-4
def test_menu_cancels_held_input(
    buses: tuple[Gio.DBusConnection, Gio.DBusConnection],
    indicator: SharingIndicator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []
    failures: list[BaseException] = []

    def send(self: DesktopPortal, method: str, signature: str, *values: object) -> None:
        calls.append((method, values))

    monkeypatch.setattr(DesktopPortal, "input", send)
    portal = object.__new__(DesktopPortal)
    portal.size = (800, 600)
    portal.node = 1
    inputs = AttachedInput(portal, indicator.stop)

    def drag() -> None:
        try:
            inputs.perform(
                "drag",
                {
                    "x1": 10,
                    "y1": 10,
                    "x2": 100,
                    "y2": 100,
                    "modifier": ["ctrl", "shift"],
                    "button": "left",
                    "duration": 60,
                },
            )
        except InterruptedError as failure:
            failures.append(failure)

    worker = Thread(target=drag)
    worker.start()
    try:
        until(lambda: bool(inputs.buttons))
        destination = buses[0].get_unique_name()
        assert destination is not None
        call(
            buses[1],
            destination,
            MENU_PATH,
            MENU,
            "EventGroup",
            GLib.Variant("(a(isvu))", ([(1, "clicked", GLib.Variant("s", ""), 0)],)),
        )
        worker.join(timeout=2)
        assert not worker.is_alive() and len(failures) == 1
        assert calls[-3:] == [
            ("NotifyPointerButton", (272, 0)),
            ("NotifyKeyboardKeycode", (42, 0)),
            ("NotifyKeyboardKeycode", (29, 0)),
        ]
        assert not inputs.keys and not inputs.buttons
        before = list(calls)
        with pytest.raises(InterruptedError):
            inputs.perform("key", {"chord": "a"})
        assert calls == before
    finally:
        indicator.stop.set()
        worker.join(timeout=2)


@pytest.mark.parametrize("forced", [False, True])
def test_owner_exit_removes_item(
    buses: tuple[Gio.DBusConnection, Gio.DBusConnection], forced: bool
) -> None:
    script = """
from threading import Event
from gi.repository import Gio, GLib
from framewisp.tray import SharingIndicator
address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
bus = Gio.DBusConnection.new_for_address_sync(address,
    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
    None, None)
indicator = SharingIndicator(bus, Event())
indicator.start()
print(bus.get_unique_name(), flush=True)
GLib.MainLoop().run()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        destination = process.stdout.readline().strip()
        assert destination.startswith(":")
        call(
            buses[1],
            destination,
            ITEM_PATH,
            "org.freedesktop.DBus.Properties",
            "GetAll",
            GLib.Variant("(s)", (ITEM,)),
        )
        if forced:
            process.kill()
        else:
            process.terminate()
        process.wait(timeout=5)
        with pytest.raises(GLib.Error):
            call(
                buses[1],
                destination,
                ITEM_PATH,
                "org.freedesktop.DBus.Properties",
                "GetAll",
                GLib.Variant("(s)", (ITEM,)),
            )
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


_OWNER = """
import sys
from pathlib import Path
from gi.repository import Gio
from framewisp import attach
root = Path(sys.argv[1])
class Portal:
    def __init__(self, stop):
        self.size = (800, 600)
        self.node = 1
        address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
        self.bus = Gio.DBusConnection.new_for_address_sync(address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)
    def open(self):
        pass
    def close(self):
        if not self.bus.is_closed():
            self.bus.close_sync(None)
            (root / 'closed').touch()
attach.DesktopPortal = Portal
raise SystemExit(attach.run_attachment(root / 'session', None))
"""


@pytest.fixture
def start_owner(
    buses: tuple[Gio.DBusConnection, Gio.DBusConnection],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[Callable[[], subprocess.Popen[bytes]]]:
    """Run the real attach owner against a stand-in portal on the isolated bus."""
    processes: list[subprocess.Popen[bytes]] = []

    def start() -> subprocess.Popen[bytes]:
        with (tmp_path / "output").open("wb") as output:
            process = subprocess.Popen(
                [sys.executable, "-c", _OWNER, str(tmp_path)],
                stdout=output,
                stderr=output,
            )
        processes.append(process)
        return process

    with tempfile.TemporaryDirectory(prefix="fw-tray-", dir="/tmp") as runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
        # Never send X11 releases to the developer's real display.
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        monkeypatch.setenv("WAYLAND_DISPLAY", "framewisp-test-unused")
        try:
            yield start
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)


def registered_item(
    host: Gio.DBusConnection, start_owner: Callable[[], subprocess.Popen[bytes]]
) -> tuple[subprocess.Popen[bytes], str, dict[str, object]]:
    """Start an owner beneath a watcher object and read its item as a host does."""
    items: list[tuple[str, str]] = []

    def register(sender: str, path: str, invocation: Gio.DBusMethodInvocation) -> None:
        items.append((sender, path))
        invocation.return_value(None)

    with watcher_object(host, register, lambda: True):
        own_watcher_name(host)
        owner = start_owner()
        until(lambda: bool(items) or owner.poll() is not None, timeout=10)
        assert items, "attach exited or registered no tray item"
    sender, path = items[0]
    properties = call(
        host,
        sender,
        path,
        "org.freedesktop.DBus.Properties",
        "GetAll",
        GLib.Variant("(s)", (ITEM,)),
    ).unpack()[0]
    return owner, sender, properties


# R-ATTACH-3
def test_attachment_offers_active_item_to_host(
    buses: tuple[Gio.DBusConnection, Gio.DBusConnection],
    start_owner: Callable[[], subprocess.Popen[bytes]],
) -> None:
    host = buses[1]
    owner, sender, properties = registered_item(host, start_owner)
    assert properties["Status"] == "Active"
    assert "desktop sharing is active" in str(properties["Title"])
    assert detach_desktop() == 0
    assert owner.wait(timeout=5) == 0
    with pytest.raises(GLib.Error):
        call(
            host,
            sender,
            ITEM_PATH,
            "org.freedesktop.DBus.Properties",
            "GetAll",
            GLib.Variant("(s)", (ITEM,)),
        )


# R-ATTACH-4
def test_tray_stop_ends_attachment(
    buses: tuple[Gio.DBusConnection, Gio.DBusConnection],
    start_owner: Callable[[], subprocess.Popen[bytes]],
    tmp_path: Path,
) -> None:
    host = buses[1]
    owner, sender, properties = registered_item(host, start_owner)
    menu = str(properties["Menu"])
    layout = call(
        host, sender, menu, MENU, "GetLayout", GLib.Variant("(iias)", (0, -1, []))
    ).unpack()
    node, labels, _children = layout[1][2][0]
    assert labels["label"] == "Stop sharing"
    assert (tmp_path / "session/session.json").exists()
    call(
        host,
        sender,
        menu,
        MENU,
        "Event",
        GLib.Variant("(isvu)", (node, "clicked", GLib.Variant("s", ""), 0)),
    )
    assert owner.wait(timeout=5) == 0
    assert (tmp_path / "closed").exists()
    assert not (tmp_path / "session/session.json").exists()


@pytest.mark.parametrize("tray", ["no watcher", "no host", "refuses item"])
def test_attach_names_stop_methods_without_host(
    buses: tuple[Gio.DBusConnection, Gio.DBusConnection],
    start_owner: Callable[[], subprocess.Popen[bytes]],
    tmp_path: Path,
    tray: str,
) -> None:
    host = buses[1]
    output = tmp_path / "output"
    lines: list[str] = []

    def refuse(sender: str, path: str, invocation: Gio.DBusMethodInvocation) -> None:
        invocation.return_dbus_error("org.example.Refused", "Refused in this test")

    def diagnosed() -> bool:
        lines[:] = [
            line
            for line in output.read_text().splitlines()
            if "indicator unavailable" in line
        ]
        return bool(lines)

    with watcher_object(host, refuse, lambda: tray == "refuses item"):
        if tray != "no watcher":
            own_watcher_name(host)
        owner = start_owner()
        until(diagnosed, timeout=10)
    assert "framewisp --detach" in lines[0] and "Ctrl+C" in lines[0]
    assert owner.poll() is None
