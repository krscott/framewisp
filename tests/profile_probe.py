"""Run native GTK and exercise persistent preferences on the private bus."""

# pyright: reportMissingModuleSource=false
import json
import locale
import os
import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # isort: skip

Gtk.init()
settings = Gtk.Settings.get_default()
assert settings is not None
label = Gtk.Label(label="Profile text café")
context = label.get_pango_context()
description = context.get_font_description()
assert description is not None
font = context.load_font(description)
assert font is not None
source = Gio.SettingsSchemaSource.new_from_directory(
    os.environ["FRAMEWISP_TEST_GTK_SCHEMAS"],
    Gio.SettingsSchemaSource.get_default(),
    False,
)
schema = source.lookup("org.gtk.Demo4.Application", False)
assert schema is not None
preferences = Gio.Settings.new_full(schema, None, None)
if sys.argv[1:] == ["--dconf-write"]:
    assert preferences.get_string("color") == "red"
    assert preferences.set_string("color", "blue")
    Gio.Settings.sync()
    raise SystemExit(0)

bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
# Exercise an app settings write that requires dconf activation, with an
# explicit backend override. Ordinary profiles use keyfile storage instead.
service_env = os.environ | {
    "GSETTINGS_BACKEND": "dconf",
    "GIO_EXTRA_MODULES": os.environ["FRAMEWISP_TEST_DCONF_MODULES"],
}
subprocess.run(
    [sys.executable, __file__, "--dconf-write"],
    env=service_env,
    check=True,
    timeout=10,
)
service_pid = bus.call_sync(
    "org.freedesktop.DBus",
    "/org/freedesktop/DBus",
    "org.freedesktop.DBus",
    "GetConnectionUnixProcessID",
    GLib.Variant("(s)", ("ca.desrt.dconf",)),
    None,
    Gio.DBusCallFlags.NONE,
    5000,
    None,
).unpack()[0]
app = subprocess.Popen([os.environ["FRAMEWISP_TEST_GTK_APP"]], env=service_env)
names = bus.call_sync(
    "org.freedesktop.DBus",
    "/org/freedesktop/DBus",
    "org.freedesktop.DBus",
    "ListNames",
    None,
    None,
    Gio.DBusCallFlags.NONE,
    5000,
    None,
).unpack()[0]
Path(os.environ["FRAMEWISP_TEST_REPORT"]).write_text(
    json.dumps(
        {
            "color": preferences.get_string("color"),
            "font": settings.get_property("gtk-font-name"),
            "font_family": font.describe().get_family(),
            "text_size": label.get_layout().get_pixel_size(),
            "locale": locale.setlocale(locale.LC_ALL, ""),
            "dpi": settings.get_property("gtk-xft-dpi"),
            "theme": settings.get_property("gtk-theme-name"),
            "home": os.environ["HOME"],
            "bus_names": names,
            "service_pid": service_pid,
            "gui_pid": app.pid,
        }
    )
)
# Exercise persistent app preferences without writing the caller's keyfile.
preferences.set_string("color", "blue")
Gio.Settings.sync()
loop = GLib.MainLoop.new(None, False)
loop.run()  # type: ignore[no-untyped-call]
