"""A native Wayland app for trying framewisp's screenshot and input commands."""

# GI loads these modules from GTK's typelibs, not Python source files.
# pyright: reportMissingModuleSource=false

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk, Pango  # isort: skip


def main() -> None:
    Gtk.init()
    loop = GLib.MainLoop()
    window = Gtk.Window(title="Framewisp demo")
    window.set_default_size(960, 640)
    window.connect("close-request", lambda *_: loop.quit())
    window.connect("map", lambda *_: print("Demo ready", flush=True))

    columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=40)
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
    content.set_margin_start(40)
    columns.set_margin_end(40)
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
    status.set_ellipsize(Pango.EllipsizeMode.END)
    status.set_max_width_chars(40)
    content.append(status)

    def show_text(action: str) -> None:
        message = f"{action}: {entry.get_text()}"
        status.set_text(message)
        print(message, flush=True)

    entry.connect("activate", lambda *_: show_text("Entered"))
    button.connect("clicked", lambda *_: show_text("Applied"))

    entry.connect("changed", lambda *_: print(f"Text: {entry.get_text()}", flush=True))

    toggle = Gtk.CheckButton(label="Enable option (hover for a tip)")
    toggle.set_tooltip_text("This option can be toggled with a click or Space.")
    toggle.connect(
        "toggled", lambda *_: print(f"Option: {toggle.get_active()}", flush=True)
    )
    content.append(toggle)

    slider_label = Gtk.Label(label="Drag the slider, or focus it and use arrow keys")
    slider_label.set_xalign(0)
    content.append(slider_label)
    slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
    slider.set_draw_value(True)
    slider.set_digits(0)
    slider.set_value(25)
    slider.connect(
        "value-changed",
        lambda *_: print(f"Value: {slider.get_value():.0f}", flush=True),
    )
    content.append(slider)

    hint = Gtk.Label(
        label="Select text with Shift + arrows or Shift + click.\nDouble-click a word; right-click to cut or select all."
    )
    hint.set_xalign(0)
    content.append(hint)

    right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
    right.set_margin_top(40)
    right.set_valign(Gtk.Align.START)
    right.append(Gtk.Label(label="Scroll in both directions"))
    scroll = Gtk.ScrolledWindow()
    scroll.set_size_request(440, 360)
    scroll.set_policy(Gtk.PolicyType.ALWAYS, Gtk.PolicyType.ALWAYS)
    scroll.set_has_frame(True)
    grid = Gtk.Grid()
    for row in range(20):
        for column in range(8):
            cell = Gtk.Label(label=f"Row {row + 1:02} / Column {column + 1}")
            cell.set_size_request(180, 44)
            grid.attach(cell, column, row, 1, 1)
    scroll.set_child(grid)
    right.append(scroll)
    position = Gtk.Label(label="Scroll: x=0 y=0")
    position.set_xalign(0)
    right.append(position)

    def show_position(_adjustment: Gtk.Adjustment) -> None:
        message = f"Scroll: x={scroll.get_hadjustment().get_value():.0f} y={scroll.get_vadjustment().get_value():.0f}"
        position.set_text(message)
        print(message, flush=True)

    scroll.get_hadjustment().connect("value-changed", show_position)
    scroll.get_vadjustment().connect("value-changed", show_position)

    def reset(_button: Gtk.Button) -> None:
        entry.set_text("")
        status.set_text("Waiting for input")
        toggle.set_active(False)
        slider.set_value(25)
        scroll.get_hadjustment().set_value(0)
        scroll.get_vadjustment().set_value(0)
        entry.grab_focus()
        print("Reset", flush=True)

    reset_button = Gtk.Button(label="Reset demo")
    reset_button.connect("clicked", reset)
    right.append(reset_button)
    columns.append(content)
    columns.append(right)
    window.set_child(columns)
    window.present()
    # pygobject-stubs omits the return annotation on MainLoop.run.
    loop.run()  # type: ignore[no-untyped-call]


if __name__ == "__main__":
    main()
