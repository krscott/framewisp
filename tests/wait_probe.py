"""Small accessible app with delayed and replaced result widgets."""

# pyright: reportMissingModuleSource=false
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # isort: skip

Gtk.init()
loop = GLib.MainLoop()
window = Gtk.Window(title="Wait probe")
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
entry = Gtk.Entry()
box.append(entry)


def result_label(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.update_property([Gtk.AccessibleProperty.LABEL], ["Result"])
    return label


result = result_label("Waiting")
box.append(result)


def submit(_entry: Gtk.Entry) -> None:
    text = entry.get_text()
    print(f"Submitted: {text}", flush=True)

    def finish() -> bool:
        global result
        if text == "exit":
            loop.quit()
        elif text == "replace":
            box.remove(result)
            result = result_label(text)
            box.append(result)
        elif text == "duplicate":
            box.append(result_label(text))
            result.set_text(text)
        else:
            result.set_text(text)
        result.update_property([Gtk.AccessibleProperty.LABEL], ["Result"])
        print(f"Finished: {text}", flush=True)
        return False

    GLib.timeout_add(int(sys.argv[1]) if len(sys.argv) > 1 else 350, finish)


entry.connect("activate", submit)
window.set_child(box)
window.connect("close-request", lambda *_: loop.quit())
window.connect("map", lambda *_: print("Wait probe ready", flush=True))
window.present()
entry.grab_focus()
loop.run()  # type: ignore[no-untyped-call]
