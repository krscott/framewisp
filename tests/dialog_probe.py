"""Embedded libadwaita preferences dialog for accessibility geometry checks."""

# pyright: reportMissingModuleSource=false

import argparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # isort: skip

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--untitled", action="store_true")
args = parser.parse_args()


def button_clicked(_button: Gtk.Button, name: str) -> None:
    print(f"Activated {name}", flush=True)


def activate(app: Adw.Application) -> None:
    window = Adw.ApplicationWindow(
        application=app, default_width=560, default_height=680
    )
    if not args.untitled:
        window.set_title("Dialog probe")
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    content.append(Gtk.Button(label="Shallow"))
    view = Adw.ToolbarView(content=content)
    view.add_top_bar(Adw.HeaderBar())
    window.set_content(view)
    window.present()
    dialog = Adw.PreferencesDialog(title="Preferences")
    for title in ["General", "Projects"]:
        page = Adw.PreferencesPage(title=title, icon_name="folder-symbolic")
        group = Adw.PreferencesGroup()
        group.add(Adw.ActionRow(title=f"{title} row"))
        button = Gtk.Button(label=f"Activate {title}")
        button.connect("clicked", button_clicked, title)
        group.add(button)
        page.add(group)
        dialog.add(page)
    dialog.present(window)
    print("Dialog probe ready", flush=True)


app = Adw.Application(application_id="dev.framewisp.DialogProbe")
app.connect("activate", activate)
app.run([])
