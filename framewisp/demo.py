"""A native Wayland app for trying framewisp's screenshot and input commands."""

# GI loads these modules from GTK's typelibs, not Python source files.
# pyright: reportMissingModuleSource=false

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # isort: skip


def main() -> None:
    Gtk.init()
    loop = GLib.MainLoop()
    window = Gtk.Window(title="Framewisp demo")
    window.set_default_size(640, 480)
    window.connect("close-request", lambda *_: loop.quit())

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
    content.set_margin_start(40)
    content.set_margin_end(40)
    content.set_margin_top(40)
    content.set_margin_bottom(40)
    content.set_halign(Gtk.Align.START)
    content.set_valign(Gtk.Align.START)

    title = Gtk.Label(label="Framewisp demo")
    title.set_xalign(0)
    content.append(title)

    entry = Gtk.Entry()
    entry.set_placeholder_text("Click here and type")
    entry.set_size_request(400, 48)
    content.append(entry)

    button = Gtk.Button(label="Apply text")
    button.set_size_request(400, 48)
    content.append(button)

    status = Gtk.Label(label="Waiting for input")
    status.set_xalign(0)
    content.append(status)

    def show_text(action: str) -> None:
        message = f"{action}: {entry.get_text()}"
        status.set_text(message)
        print(message, flush=True)

    entry.connect("activate", lambda *_: show_text("Entered"))
    button.connect("clicked", lambda *_: show_text("Applied"))

    window.set_child(content)
    window.present()
    # pygobject-stubs omits the return annotation on MainLoop.run.
    loop.run()  # type: ignore[no-untyped-call]


if __name__ == "__main__":
    main()
