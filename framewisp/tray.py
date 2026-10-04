"""An attachment-owned StatusNotifierItem and a static D-Bus stop menu."""

# pyright: reportMissingModuleSource=false

import sys
from threading import Event

from gi.repository import Gio, GLib

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM = "org.kde.StatusNotifierItem"
MENU = "com.canonical.dbusmenu"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/Menu"
LABEL = "framewisp desktop sharing is active"
UNAVAILABLE = (
    "Sharing indicator unavailable: no compatible tray host or registration failed. "
    "Stop with framewisp --detach (your configured shortcut) or Ctrl+C in this terminal."
)


def interface_xml(
    name: str, properties: dict[str, GLib.Variant], methods: dict[str, tuple[str, str]]
) -> str:
    """Describe the fixed interfaces without duplicating property signatures."""
    entries = [
        f'<property name="{key}" type="{value.get_type_string()}" access="read"/>'
        for key, value in properties.items()
    ]
    for method, (inputs, outputs) in methods.items():
        args: list[str] = []
        for direction, signature in [("in", inputs), ("out", outputs)]:
            for argument in signature.split():
                args.append(f'<arg type="{argument}" direction="{direction}"/>')
        entries.append(f'<method name="{method}">{"".join(args)}</method>')
    return f'<node><interface name="{name}">{"".join(entries)}</interface></node>'


def icon_pixmaps() -> GLib.Variant:
    # Network-order ARGB: a red sharing lamp inside a white monitor outline.
    pixels = bytearray()
    for y in range(32):
        for x in range(32):
            monitor = (
                (4 <= x <= 27 and y in (5, 6, 23, 24))
                or (x in (4, 5, 26, 27) and 5 <= y <= 24)
                or (14 <= x <= 17 and 25 <= y <= 27)
                or (10 <= x <= 21 and y == 28)
            )
            lamp = (x - 16) ** 2 + (y - 15) ** 2 <= 36
            pixels.extend(
                (255, 230, 55, 55)
                if lamp
                else (255, 255, 255, 255) if monitor else (0, 0, 0, 0)
            )
    return GLib.Variant("a(iiay)", [(32, 32, bytes(pixels))])


class SharingIndicator:
    def __init__(self, bus: Gio.DBusConnection, stop: Event):
        self.bus = bus
        self.stop = stop
        self.objects: list[int] = []
        self.watch = 0
        self.proxy: Gio.DBusProxy | None = None
        self.closed = False
        self.registered = False
        self.registering = False
        self.generation = 0
        self.available: bool | None = None
        pixmaps = icon_pixmaps()
        self.properties = {
            ITEM: {
                "Category": GLib.Variant("s", "SystemServices"),
                "Id": GLib.Variant("s", "framewisp-sharing"),
                "Title": GLib.Variant("s", LABEL),
                "Status": GLib.Variant("s", "Active"),
                "WindowId": GLib.Variant("u", 0),
                "IconName": GLib.Variant("s", ""),
                "IconPixmap": pixmaps,
                "OverlayIconName": GLib.Variant("s", ""),
                "OverlayIconPixmap": GLib.Variant("a(iiay)", []),
                "AttentionIconName": GLib.Variant("s", ""),
                "AttentionIconPixmap": GLib.Variant("a(iiay)", []),
                "AttentionMovieName": GLib.Variant("s", ""),
                "ToolTip": GLib.Variant(
                    "(sa(iiay)ss)",
                    ("", pixmaps.unpack(), LABEL, "Open the menu to stop sharing."),
                ),
                "ItemIsMenu": GLib.Variant("b", True),
                "Menu": GLib.Variant("o", MENU_PATH),
            },
            MENU: {
                "Version": GLib.Variant("u", 3),
                "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"),
                "IconThemePath": GLib.Variant("as", []),
            },
        }
        self.menu_properties = {
            "label": GLib.Variant("s", "Stop sharing"),
            "enabled": GLib.Variant("b", True),
            "visible": GLib.Variant("b", True),
        }

    def start(self) -> None:
        methods = {
            ITEM: {
                "ContextMenu": ("i i", ""),
                "Activate": ("i i", ""),
                "SecondaryActivate": ("i i", ""),
                "Scroll": ("i s", ""),
            },
            MENU: {
                "GetLayout": ("i i as", "u (ia{sv}av)"),
                "GetGroupProperties": ("ai as", "a(ia{sv})"),
                "GetProperty": ("i s", "v"),
                "Event": ("i s v u", ""),
                "EventGroup": ("a(isvu)", "ai"),
                "AboutToShow": ("i", "b"),
                "AboutToShowGroup": ("ai", "ai ai"),
            },
        }
        for name, path in [(ITEM, ITEM_PATH), (MENU, MENU_PATH)]:
            info = Gio.DBusNodeInfo.new_for_xml(
                interface_xml(name, self.properties[name], methods[name])
            ).interfaces[0]
            self.objects.append(
                self.bus.register_object(path, info, self.method, self.property, None)
            )
        self.watch = Gio.bus_watch_name_on_connection(
            self.bus,
            WATCHER,
            Gio.BusNameWatcherFlags.NONE,
            self.appeared,
            self.vanished,
        )

    def report(self, available: bool) -> None:
        if self.available != available:
            print(
                (
                    "Sharing indicator registered; the desktop controls icon visibility and placement."
                    if available
                    else UNAVAILABLE
                ),
                file=sys.stderr,
                flush=True,
            )
            self.available = available

    def appeared(self, bus: Gio.DBusConnection, name: str, owner: str) -> None:
        generation = self.generation

        def created(source: object, result: Gio.AsyncResult, data: object) -> None:
            if self.closed or generation != self.generation:
                return
            try:
                self.proxy = Gio.DBusProxy.new_finish(result)
                self.proxy.connect(
                    "g-properties-changed", lambda *args: self.check_host()
                )
                self.proxy.connect("g-signal", lambda *args: self.refresh_host())
                self.check_host()
            except GLib.Error:
                self.report(False)

        Gio.DBusProxy.new(
            bus,
            Gio.DBusProxyFlags.DO_NOT_AUTO_START,
            None,
            owner,
            "/StatusNotifierWatcher",
            WATCHER,
            None,
            created,
            None,
        )

    def vanished(self, bus: Gio.DBusConnection, name: str) -> None:
        self.generation += 1
        self.proxy = None
        self.registered = False
        self.registering = False
        if not self.closed:
            self.report(False)

    def check_host(self) -> None:
        proxy = self.proxy
        if self.closed or proxy is None:
            return
        host = proxy.get_cached_property("IsStatusNotifierHostRegistered")
        if host is None or not host.unpack():
            self.report(False)
            return
        if self.registered:
            self.report(True)
            return
        if self.registering:
            return
        self.registering = True
        generation = self.generation

        def completed(source: object, result: Gio.AsyncResult, data: object) -> None:
            if self.closed or generation != self.generation:
                return
            self.registering = False
            try:
                proxy.call_finish(result)
                self.registered = True
                self.check_host()
            except GLib.Error:
                self.registered = False
                self.report(False)

        proxy.call(
            "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (ITEM_PATH,)),
            Gio.DBusCallFlags.NONE,
            2000,
            None,
            completed,
            None,
        )

    def refresh_host(self) -> None:
        # Watchers can announce hosts without emitting PropertiesChanged.
        proxy = self.proxy
        generation = self.generation
        if self.closed or proxy is None:
            return

        def completed(source: object, result: Gio.AsyncResult, data: object) -> None:
            if self.closed or generation != self.generation:
                return
            try:
                value = proxy.call_finish(result).get_child_value(0).get_variant()
                proxy.set_cached_property("IsStatusNotifierHostRegistered", value)
                self.check_host()
            except GLib.Error:
                self.report(False)

        proxy.call(
            "org.freedesktop.DBus.Properties.Get",
            GLib.Variant("(ss)", (WATCHER, "IsStatusNotifierHostRegistered")),
            Gio.DBusCallFlags.NONE,
            2000,
            None,
            completed,
            None,
        )

    def property(
        self, bus: Gio.DBusConnection, sender: str, path: str, interface: str, name: str
    ) -> GLib.Variant:
        return self.properties[interface][name]

    def node_properties(self, node: int, names: list[str]) -> dict[str, GLib.Variant]:
        properties = (
            {"children-display": GLib.Variant("s", "submenu")}
            if node == 0
            else self.menu_properties
        )
        return {
            key: value for key, value in properties.items() if not names or key in names
        }

    def event(self, node: int, event: str) -> None:
        if node == 1 and event == "clicked":
            self.stop.set()

    def method(
        self,
        bus: Gio.DBusConnection,
        sender: str,
        path: str,
        interface: str,
        method: str,
        parameters: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        args = parameters.unpack()
        if interface == ITEM:
            invocation.return_value(None)
            return
        result: GLib.Variant | None = None
        if method == "GetLayout":
            parent, depth, names = args
            if parent not in (0, 1):
                invocation.return_dbus_error(
                    "com.canonical.dbusmenu.Error.InvalidMenuItem", "Unknown menu item"
                )
                return
            children = (
                [GLib.Variant("(ia{sv}av)", (1, self.node_properties(1, names), []))]
                if parent == 0 and depth != 0
                else []
            )
            result = GLib.Variant(
                "(u(ia{sv}av))",
                (1, (parent, self.node_properties(parent, names), children)),
            )
        elif method == "GetGroupProperties":
            nodes, names = args
            result = GLib.Variant(
                "(a(ia{sv}))",
                (
                    [
                        (node, self.node_properties(node, names))
                        for node in (nodes or [0, 1])
                        if node in (0, 1)
                    ],
                ),
            )
        elif method == "GetProperty":
            node, name = args
            properties = self.node_properties(node, []) if node in (0, 1) else {}
            if name not in properties:
                invocation.return_dbus_error(
                    "com.canonical.dbusmenu.Error.InvalidProperty",
                    "Unknown menu property",
                )
                return
            result = GLib.Variant("(v)", (properties[name],))
        elif method == "Event":
            self.event(args[0], args[1])
        elif method == "EventGroup":
            errors: list[int] = []
            for node, event, _data, _timestamp in args[0]:
                if node not in (0, 1):
                    errors.append(node)
                else:
                    self.event(node, event)
            result = GLib.Variant("(ai)", (errors,))
        elif method == "AboutToShow":
            result = GLib.Variant("(b)", (False,))
        elif method == "AboutToShowGroup":
            result = GLib.Variant(
                "(aiai)", ([], [node for node in args[0] if node not in (0, 1)])
            )
        invocation.return_value(result)

    def close(self) -> None:
        self.closed = True
        if self.watch:
            Gio.bus_unwatch_name(self.watch)
            self.watch = 0
        for registration in self.objects:
            self.bus.unregister_object(registration)
        self.objects.clear()
        self.proxy = None
