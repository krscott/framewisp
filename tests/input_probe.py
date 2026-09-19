"""GTK test app that reports the input it actually receives."""

# pyright: reportMissingModuleSource=false

import json
import time
from functools import partial

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, GLib, Gtk  # isort: skip


def log(event: str, **values: str | float | bool) -> None:
    print(json.dumps({"event": event, "time": time.monotonic(), **values}), flush=True)


def main() -> None:
    Gtk.init()
    loop = GLib.MainLoop()
    window = Gtk.Window(title="Input probe")
    window.connect("close-request", lambda *_: loop.quit())
    window.connect("map", lambda *_: log("ready"))
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    area = Gtk.DrawingArea()
    area.set_size_request(640, 400)
    content.append(area)
    motion = Gtk.EventControllerMotion()

    def on_motion(controller: Gtk.EventControllerMotion, x: float, y: float) -> None:
        log(
            "motion",
            x=x,
            y=y,
            pressed=bool(
                controller.get_current_event_state() & Gdk.ModifierType.BUTTON1_MASK
            ),
        )

    motion.connect("motion", on_motion)
    area.add_controller(motion)
    click = Gtk.GestureClick()
    click.set_button(1)

    def on_button(
        event: str, _gesture: Gtk.GestureClick, _count: int, x: float, y: float
    ) -> None:
        log(event, x=x, y=y)

    click.connect("pressed", partial(on_button, "press"))
    click.connect("released", partial(on_button, "release"))
    area.add_controller(click)
    entry = Gtk.Entry()
    entry.set_size_request(640, 48)
    entry.connect("changed", lambda *_: log("text", text=entry.get_text()))
    content.append(entry)
    window.set_child(content)
    window.present()
    loop.run()  # type: ignore[no-untyped-call]


if __name__ == "__main__":
    main()
