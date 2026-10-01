"""Small accessible app with delayed and replaced result widgets."""

# pyright: reportMissingModuleSource=false
import argparse

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # isort: skip

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("delay_ms", type=int, nargs="?", default=350)
parser.add_argument("--nodes", type=int, default=0)
parser.add_argument("--depth", type=int, default=0)
args = parser.parse_args()

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
for index in range(args.nodes):
    box.append(Gtk.Label(label=f"Filler {index}"))
root = box
for _ in range(args.depth):
    parent = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    parent.append(root)
    root = parent


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

    GLib.timeout_add(args.delay_ms, finish)


entry.connect("activate", submit)
window.set_child(root)
window.connect("close-request", lambda *_: loop.quit())
window.connect("map", lambda *_: print("Wait probe ready", flush=True))
window.present()
entry.grab_focus()
loop.run()  # type: ignore[no-untyped-call]
