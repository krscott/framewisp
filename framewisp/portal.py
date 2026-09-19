"""A user-approved desktop portal connection owned by an attach process."""

# pyright: reportMissingModuleSource=false

import uuid
from collections.abc import Iterable
from threading import Event
from typing import cast

from gi.repository import Gio, GLib

PORTAL = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
REMOTE = "org.freedesktop.portal.RemoteDesktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"


def dispatch_events() -> None:
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


class DesktopPortal:
    def __init__(self, stop: Event):
        self.stop = stop
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.session: str | None = None
        self.subscription: int | None = None
        self.node = 0
        self.size = (0, 0)

    def request(
        self,
        interface: str,
        method: str,
        signature: str,
        arguments: Iterable[object],
        options: dict[str, GLib.Variant],
    ) -> dict[str, object]:
        token = "framewisp_" + uuid.uuid4().hex
        sender = self.bus.get_unique_name()
        assert sender is not None
        path = (
            "/org/freedesktop/portal/desktop/request/"
            + sender[1:].replace(".", "_")
            + "/"
            + token
        )
        response: list[tuple[int, dict[str, object]]] = []

        def received(
            connection: Gio.DBusConnection,
            sender: str,
            path: str,
            interface: str,
            signal: str,
            parameters: GLib.Variant,
            data: object,
        ) -> None:
            response.append(parameters.unpack())

        subscription = self.bus.signal_subscribe(
            PORTAL,
            "org.freedesktop.portal.Request",
            "Response",
            path,
            None,
            Gio.DBusSignalFlags.NONE,
            received,
            None,
        )
        try:
            self.call(
                interface,
                method,
                signature,
                (*arguments, options | {"handle_token": GLib.Variant("s", token)}),
            )
            while not response:
                dispatch_events()
                if self.stop.wait(0.02):
                    self.bus.call_sync(
                        PORTAL,
                        path,
                        "org.freedesktop.portal.Request",
                        "Close",
                        None,
                        None,
                        Gio.DBusCallFlags.NONE,
                        2000,
                        None,
                    )
                    raise InterruptedError(
                        "Attach stopped while waiting for desktop permission."
                    )
            code, result = response[0]
            if code:
                raise RuntimeError(
                    f"Desktop permission request {method} was cancelled or denied."
                )
            return result
        finally:
            self.bus.signal_unsubscribe(subscription)

    def call(
        self, interface: str, method: str, signature: str, arguments: object
    ) -> GLib.Variant:
        return self.bus.call_sync(
            PORTAL,
            PATH,
            interface,
            method,
            GLib.Variant(signature, arguments),
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )

    def open(self) -> None:
        result = self.request(
            REMOTE,
            "CreateSession",
            "(a{sv})",
            (),
            {
                "session_handle_token": GLib.Variant(
                    "s", "framewisp_" + uuid.uuid4().hex
                ),
            },
        )
        self.session = cast(str, result["session_handle"])

        def closed(
            connection: Gio.DBusConnection,
            sender: str,
            path: str,
            interface: str,
            signal: str,
            parameters: GLib.Variant,
            data: object,
        ) -> None:
            self.stop.set()

        self.subscription = self.bus.signal_subscribe(
            PORTAL,
            "org.freedesktop.portal.Session",
            "Closed",
            self.session,
            None,
            Gio.DBusSignalFlags.NONE,
            closed,
            None,
        )
        self.request(
            REMOTE,
            "SelectDevices",
            "(oa{sv})",
            (self.session,),
            {
                "types": GLib.Variant("u", 3),
                "persist_mode": GLib.Variant("u", 0),
            },
        )
        self.request(
            SCREENCAST,
            "SelectSources",
            "(oa{sv})",
            (self.session,),
            {
                "types": GLib.Variant("u", 1),
                "multiple": GLib.Variant("b", False),
                "cursor_mode": GLib.Variant("u", 2),
            },
        )
        print(
            "Approve keyboard/pointer access and select one monitor in the desktop dialog.",
            flush=True,
        )
        result = self.request(REMOTE, "Start", "(osa{sv})", (self.session, ""), {})
        if result.get("devices") != 3:
            raise RuntimeError("Attach requires both keyboard and pointer permission.")
        streams = cast(list[tuple[int, dict[str, object]]], result.get("streams", []))
        if len(streams) != 1:
            raise RuntimeError("Attach requires exactly one shared monitor.")
        self.node, properties = streams[0]
        self.size = cast(tuple[int, int], properties["size"])

    def input(self, method: str, signature: str, *values: object) -> None:
        options: dict[str, GLib.Variant] = {}
        self.call(
            REMOTE,
            method,
            "(oa{sv}" + signature + ")",
            (self.session, options, *values),
        )

    def capture_fd(self) -> int:
        result, descriptors = self.bus.call_with_unix_fd_list_sync(
            PORTAL,
            PATH,
            SCREENCAST,
            "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self.session, {})),
            GLib.VariantType.new("(h)"),
            Gio.DBusCallFlags.NONE,
            2000,
            None,
            None,
        )
        assert descriptors is not None
        return descriptors.get(result.unpack()[0])

    def close(self) -> None:
        if self.session is not None:
            try:
                self.bus.call_sync(
                    PORTAL,
                    self.session,
                    "org.freedesktop.portal.Session",
                    "Close",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    2000,
                    None,
                )
            except GLib.Error:
                # The desktop may already have revoked and closed the session.
                pass
        if self.subscription is not None:
            self.bus.signal_unsubscribe(self.subscription)
