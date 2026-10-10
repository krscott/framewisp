"""Accessible app with shallow and deeply nested buttons."""

# pyright: reportMissingModuleSource=false
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # isort: skip

Gtk.init()
loop = GLib.MainLoop()
window = Gtk.Window(title="Deep probe")
content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
content.append(Gtk.Button(label="Shallow"))
box = Gtk.Box()
content.append(box)
for _ in range(40):
    inner = Gtk.Box()
    box.append(inner)
    box = inner
box.append(Gtk.Button(label="Deep"))
window.set_child(content)
window.connect("close-request", lambda *_: loop.quit())
window.connect("map", lambda *_: print("Deep probe ready", flush=True))
window.present()
loop.run()  # type: ignore[no-untyped-call]
