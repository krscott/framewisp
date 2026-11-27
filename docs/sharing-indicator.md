# Sharing indicator verification

The sharing indicator uses org.kde.StatusNotifierItem and com.canonical.dbusmenu through
Gio, already present in the Nix package and development environment. Normal use
requires the user's session bus and portal, without root access.

## Desktop setup

- KDE Plasma: enable the system tray and allow the framewisp indicator in its settings.
- COSMIC: include the status area applet in the panel or dock.
- GNOME: install and enable a compatible StatusNotifierItem/AppIndicator extension,
  such as AppIndicator and KStatusNotifierItem Support.

The desktop controls placement, overflow, panel auto-hide, and visibility.
A successful registration is not proof that the user can see the indicator.

## Verification status

Automated tests use an isolated real D-Bus daemon to check the active-sharing
properties, icon pixels, menu access, stop event, input cancellation with held
Ctrl/Shift and pointer button release, object removal, missing-host diagnostic,
and registration after watcher restart. They also run the real attach owner with
a stand-in portal and a fake watcher to check its indicator, stop menu, and missing-host
diagnostic. Desktop share tests cover `--stop-sharing`,
suspended owners, terminal loss, exit signals, and cancellation.

Do not treat protocol tests as desktop acceptance. Record desktop, portal, and
GNOME extension versions when running the following checks on each desktop.

| Desktop | Desktop version | Extension version | Visual and live portal checks |
| --- | --- | --- | --- |
| KDE Plasma | Not tested | Not applicable | Pending |
| COSMIC | Not recorded | Not applicable | Visual check passed; the indicator's stop button works. Other live checks pending |
| GNOME (Xorg) | 42.9 on Ubuntu 22.04; xdg-desktop-portal 1.14.4, -gnome 42.1 | Ubuntu AppIndicators 42 | Visual check passed; the indicator's stop works. Every input command and screenshot freshness passed. Held Shift and button released after an emergency stop, SIGKILL of a suspended owner, and terminal loss. Shortcuts and installed package pending |
| GNOME (Wayland) | Not tested | Not tested | Pending |

## Live checks

Configure and test both emergency stop shortcuts described in the README.
Start `framewisp SESSION attach` yourself in a foreground desktop terminal,
type ATTACH, and approve sharing for one monitor. Never automate user consent.

1. Confirm the indicator, active-sharing label or tooltip, and "Stop sharing" menu.
2. Choose "Stop sharing" while idle. Verify screenshot and input requests fail and
   the user's apps stay open and usable.
3. Reattach with fresh approval. Start a long drag with Ctrl and Shift held,
   then choose "Stop sharing". Confirm cancellation and release of the pointer
   button and modifiers. Confirm later screenshot/input requests fail.
4. Reattach separately for `--stop-sharing`, Ctrl+C, terminal closure, owner exit,
   forced owner death, and portal revocation. Confirm each removes the indicator.
5. Verify `--stop-sharing` during held-modifier input and after suspending the
   owner with SIGSTOP. Confirm the desktop share ends and the indicator disappears.
   On X11, one second after each stop in steps 3 to 5, check that
   `xinput query-state "Virtual core XTEST keyboard"` and the matching
   `pointer` device report nothing `down`.
6. Disable the compatible tray host, then attach again. Confirm the terminal
   diagnostic explains `framewisp --stop-sharing` and Ctrl+C, and both still work.
7. Repeat with the installed Nix package and with `nix develop`. Record the package
   revision and required desktop setup alongside the version results.
8. Drag a window with `framewisp SESSION drag`, then take a screenshot at once.
   Confirm it shows the new position (R-ATTACH-7). Repeat a few times.
9. On a Wayland desktop, run an X11 app such as `GDK_BACKEND=x11 framewisp-demo`.
   Confirm screenshots show it and input reaches it through Xwayland. Not yet
   checked on any desktop.
