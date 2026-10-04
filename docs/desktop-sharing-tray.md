# Desktop sharing tray verification

The indicator uses org.kde.StatusNotifierItem and com.canonical.dbusmenu through
Gio, already present in the Nix package and development environment. Normal use
requires the user's session bus and portal, without root access.

## Desktop setup

- KDE Plasma: enable the system tray and allow the framewisp item in its settings.
- COSMIC: include the status area applet in the panel or dock.
- GNOME: install and enable a compatible StatusNotifierItem/AppIndicator extension,
  such as AppIndicator and KStatusNotifierItem Support.

The desktop controls placement, overflow, panel auto-hide, and icon visibility.
A successful registration is not proof that the user can see an icon.

## Verification status

Automated tests use an isolated real D-Bus daemon to check the active-sharing
properties, icon pixels, menu access, stop event, input cancellation with held
Ctrl/Shift and pointer button release, object removal, missing-host diagnostic,
and registration after watcher restart. Attachment tests cover emergency detach,
suspended owners, terminal loss, exit signals, and cancellation.

Visual desktop acceptance has not been performed in the implementation environment.
Do not treat protocol tests as desktop acceptance. Record desktop, portal, and
GNOME extension versions when running the following checks on each desktop.

| Desktop | Desktop version | Extension version | Visual and live portal checks |
| --- | --- | --- | --- |
| KDE Plasma | Not tested | Not applicable | Pending |
| COSMIC | Not tested | Not applicable | Pending |
| GNOME | Not tested | Not tested | Pending |

## Live checks

Configure and test both emergency detach shortcuts described in the README.
Start `framewisp SESSION attach` yourself in a foreground desktop terminal,
type ATTACH, and approve sharing for one monitor. Never automate user consent.

1. Confirm the icon, active-sharing label or tooltip, and "Stop sharing" menu.
2. Choose "Stop sharing" while idle. Verify capture and input requests fail and
   the user's apps stay open and usable.
3. Reattach with fresh approval. Start a long drag with Ctrl and Shift held,
   then choose "Stop sharing". Confirm cancellation and release of the pointer
   button and modifiers. Confirm later capture/input requests fail.
4. Reattach separately for ordinary detach, Ctrl+C, terminal closure, owner exit,
   forced owner death, and portal revocation. Confirm each removes the item.
5. Verify emergency detach during held-modifier input and after suspending the
   owner with SIGSTOP. Confirm access ends and the icon disappears.
6. Disable the compatible tray host, then attach again. Confirm the terminal
   diagnostic explains `framewisp --detach` and Ctrl+C, and both still work.
7. Repeat with the installed Nix package and with `nix develop`. Record the package
   revision and required desktop setup alongside the version results.
