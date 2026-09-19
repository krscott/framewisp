"""Two scrollable panes reporting wheel events and their resulting positions."""

# pyright: reportMissingModuleSource=false

import json
from functools import partial

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # isort: skip


def log(event: str, **values: str | float) -> None:
    print(json.dumps({"event": event, **values}), flush=True)


def scrolled(
    pane: str, _controller: Gtk.EventControllerScroll, dx: float, dy: float
) -> bool:
    log("scroll", pane=pane, dx=dx, dy=dy)
    return False


def adjusted(pane: str, axis: str, adjustment: Gtk.Adjustment) -> None:
    log("position", pane=pane, axis=axis, value=adjustment.get_value())


def main() -> None:
    Gtk.init()
    loop = GLib.MainLoop()
    window = Gtk.Window(title="Scroll probe")
    window.connect("close-request", lambda *_: loop.quit())
    window.connect("map", lambda *_: log("ready"))
    content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    for name in ["left", "right"]:
        pane = Gtk.ScrolledWindow()
        pane.set_hexpand(True)
        pane.set_vexpand(True)
        label = Gtk.Label(
            label="\n".join(
                f"{name} row {row}: " + "wide content " * 20 for row in range(200)
            )
        )
        label.set_xalign(0)
        label.set_yalign(0)
        pane.set_child(label)
        controller = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.BOTH_AXES
            | Gtk.EventControllerScrollFlags.DISCRETE
        )
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("scroll", partial(scrolled, name))
        pane.add_controller(controller)
        pane.get_hadjustment().connect("value-changed", partial(adjusted, name, "x"))
        pane.get_vadjustment().connect("value-changed", partial(adjusted, name, "y"))
        content.append(pane)
    window.set_child(content)
    window.present()
    loop.run()  # type: ignore[no-untyped-call]


if __name__ == "__main__":
    main()
