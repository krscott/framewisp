# Design

## Scope

Framewisp gives an agent a CLI loop for one native Wayland application on the
current x86_64 NixOS environment: launch, screenshot, click, type, screenshot,
stop. The included GTK 4 demo is the acceptance application.

## Components

- `framewisp/__main__.py` parses the CLI and dispatches to the runner or a tool.
- `framewisp/lib.py` owns process lifetime and invokes existing display tools.
- `framewisp/demo.py` displays a text field, button, and result label. Return
  changes the label to `Entered: TEXT`; clicking the button changes it to
  `Applied: TEXT`. It also prints those messages to stdout for integration tests.
- Sway provides the headless Wayland display using the Pixman software renderer.
- wayvnc creates virtual pointer and keyboard devices. vncdotool's `vncdo`
  command sends input over a private Unix socket, connecting once per CLI call.
- grim captures the headless output directly to PNG.

There is no controller daemon, RPC API, framebuffer cache, or video pipeline.
The foreground `run` command is the lifetime owner.

## Startup

1. Create or reuse the requested session directory. Refuse an existing
   `session.json`; concurrent runs and stale-session recovery are unsupported.
2. Register SIGINT and SIGTERM handlers that request shutdown.
3. Create a temporary runtime directory. It holds the compositor configuration
   and sockets and is removed when the runner exits normally. Its short path
   avoids Unix socket path limits even when the session log directory is long.
4. Copy the environment, remove inherited display and session-bus addresses,
   and set the private `XDG_RUNTIME_DIR`. Set `WLR_BACKENDS=headless`,
   `WLR_RENDERER=pixman`, `WLR_LIBINPUT_NO_DEVICES=1`, `GDK_BACKEND=wayland`,
   and `GSK_RENDERER=cairo`.
5. Start Sway with a generated configuration: Xwayland disabled, a single
   `HEADLESS-1` output at 1280x720 and 60 Hz, a fallback seat, US keyboard layout,
   and no window borders. Load no host Sway configuration.
6. Wait up to ten seconds for its Wayland socket, checking for process exit.
   Set `WAYLAND_DISPLAY` to the discovered socket name.
7. Start wayvnc with an empty configuration, US layout, and a Unix socket in
   the private runtime directory. Wait up to ten seconds for that socket.
8. Launch the application with the private environment and no shell expansion.
9. Write `session.json` and print `Session ready:`. This indicates process and
   socket readiness, not application rendering readiness.

Each child runs in its own process session with stdin disconnected and combined
stdout/stderr directed to its log. The runner monitors the three direct children.

## Interaction

CLI calls read `session.json` to locate the display and VNC socket. The JSON has
`runtime_directory`, `wayland_display`, and `processes` keys. The last is a map
of `sway`, `wayvnc`, and `app` to their PIDs; no inherited environment is saved.

`screenshot` invokes grim for `HEADLESS-1` with PNG output. It obtains a new
capture from the compositor, but makes no claim that preceding input has finished
changing the application. Consumers poll for their expected visual result when
needed. Capture commands have a ten-second deadline.

`click` invokes `vncdo move X Y click 1`. `type` invokes `vncdo type TEXT`, keeping
the text as one argument; it accepts printable ASCII. `key` maps Return to
`enter`, Tab to `tab`, and BackSpace to `bsp`. Each operation presses and releases
its buttons or keys within one connection. Each connection waits 100 ms before
sending input: without that delay, Sway dropped the first key while focusing
the newly created virtual keyboard in the tested environment. This is a measured
workaround for this setup, not a general readiness guarantee.
VNC operations have a ten-second
client deadline and a fifteen-second subprocess deadline.

## Shutdown and errors

Application exit ends the session and returns its exit code. SIGINT or SIGTERM
requests a clean stop and returns zero. A backend exit or startup timeout reports
an error naming its log. Unexpected errors keep their traceback.

ExitStack stops managed children in reverse order: app, wayvnc, Sway. It sends
SIGTERM, waits up to five seconds per process, then uses SIGKILL if needed and
reaps the process. The runtime directory and `session.json` are removed. Logs
remain in the session directory.

This guarantees cleanup of the managed direct children in the tested paths.
Descendant containment, crash recovery, and concurrent command coordination are
deferred.

## Environment and verification

The pinned Nix development shell supplies Sway, wayvnc, grim, GTK's libraries and
introspection data, and Python dependencies. Python dependencies are also declared
in `pyproject.toml` and `default.nix`. The demo uses a plain GTK window and GLib
loop and runs without a D-Bus session.

The integration test starts the real CLI, waits for the app's initial screenshot,
types text, sends BackSpace and Return, clicks the button, and verifies both the
demo's emitted messages and changed screenshot regions. It also exercises Tab.
Shutdown tests check that the recorded child PIDs and private sockets are gone
after SIGTERM and SIGINT. Both mypy and pyright check the Python code.

## Capture decision

In the tested Sway 1.12, wlroots 0.20.2, wayvnc 0.10.1, and vncdotool 1.2.0
combination, VNC connected and delivered input but full-frame screenshot requests
timed out. grim captured the same display successfully. The MVP uses grim for
all screenshots instead of patching a display dependency or implementing RFB.
The cause of the VNC capture failure has not been established.
