# Design

## Scope

Framewisp gives an agent a CLI loop for one native Wayland application on the
current x86_64 NixOS environment: launch, screenshot, click, drag, type, screenshot,
stop. Sessions can optionally record a silent MP4. The included GTK 4 demo is the
acceptance application.

## Components

- `framewisp/__main__.py` parses the CLI and dispatches to the runner or a tool.
- `framewisp/lib.py` owns process lifetime and invokes existing display tools.
  `run_session` coordinates startup, monitoring, and shutdown. Separate helpers
  prepare its environment and manage Sway, wayvnc, and optional recorder startup,
  waiting for sockets or the recording header. Each startup helper uses `managed_process` for cleanup and
  yields `None` if shutdown is requested while waiting.
- `framewisp/demo.py` displays a text field, button, and result label. Return
  changes the label to `Entered: TEXT`; clicking the button changes it to
  `Applied: TEXT`. It also prints those messages to stdout for integration tests.
- Sway provides the headless Wayland display using the Pixman software renderer.
- wayvnc creates virtual pointer and keyboard devices. vncdotool's `vncdo`
  command sends input over a private Unix socket, connecting once per CLI call.
- grim captures the headless output directly to PNG.
- wf-recorder captures the display to H.264 MP4 when `run --record FILE` is used.

There is no controller daemon, RPC API, or framebuffer cache.
The foreground `run` command is the lifetime owner.

## Startup

1. Create or reuse the requested session directory. Refuse an existing
   `session.json`; concurrent runs and stale-session recovery are unsupported.
   Refuse existing recording destinations and paths reserved for session logs or
   metadata before starting any children.
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
8. If recording, start wf-recorder for `HEADLESS-1` with continuous capture (`-D`),
   30 fps, software `libx264`, `yuv420p` with explicit full-range conversion, and
   the MP4 muxer. Wait up to ten seconds
   for a nonempty output file, checking for recorder exit. With the pinned
   wf-recorder, the MP4 header is written after the first frame is received.
9. Launch the application with the private environment and no shell expansion.
10. Write `session.json` and print `Session ready:`. This indicates process and
   socket readiness, not application rendering readiness.

Each child runs in its own process session with stdin disconnected and combined
stdout/stderr directed to its log. The runner monitors all managed children.

## Interaction

CLI calls read `session.json` to locate the display and VNC socket. The JSON has
`runtime_directory`, `wayland_display`, and `processes` keys. The last is a map
of `sway`, `wayvnc`, `app`, and optionally `recorder` to their PIDs; no inherited
environment is saved.

`screenshot` invokes grim for `HEADLESS-1` with PNG output. It obtains a new
capture from the compositor, but makes no claim that preceding input has finished
changing the application. The CLI accepts an optional `--delay SECONDS`, a finite,
nonnegative number defaulting to zero, and sleeps for that duration before
invoking capture. The grim process still has its own ten-second deadline; the
delay does not count toward it. Consumers choose the delay and capture again
when needed; there is no automatic animation detection.

`click` invokes `vncdo move X Y click 1`. `drag X1 Y1 X2 Y2` invokes
`vncdo move X1 Y1 mousedown 1 move X2 Y2 mouseup 1` on one connection. It moves
directly between the endpoints, without intermediate points or timing options.
`type` invokes `vncdo type TEXT`, keeping
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

ExitStack stops managed children in reverse order: app, optional recorder,
wayvnc, Sway. It sends
SIGTERM, waits up to five seconds per process, then uses SIGKILL if needed and
reaps the process. The runtime directory and `session.json` are removed. Logs
remain in the session directory. The recorder handles SIGTERM by flushing its
encoder and writing the MP4 trailer while Sway is still alive. A nonzero recorder
exit during finalization raises an error naming the destination and log. A killed
recorder may leave an incomplete file; no crash recovery is attempted.

This guarantees cleanup of the managed direct children in the tested paths.
Descendant containment, crash recovery, and concurrent command coordination are
deferred.

## Environment and verification

The standalone Nix package wraps both entry points with Python dependencies,
GTK libraries and introspection data, and a PATH containing Sway, wayvnc, grim,
wf-recorder, vncdotool, and its own bin directory (for the bundled demo). The
flake exports this package as `packages.x86_64-linux.default` and `framewisp`,
with `meta.mainProgram` selecting the CLI for `nix run`. NixOS and Home Manager
can install the same package onto PATH. No system service is required.

The development shell additionally supplies FFmpeg for test decoding and the
Python development tools. `checks.x86_64-linux.package` runs the integration
tests against the built package with an empty environment and only the package
and FFmpeg on PATH. Python dependencies are also declared
in `pyproject.toml` and `default.nix`. The demo uses a plain GTK window and GLib
loop and runs without a D-Bus session.

The integration test starts the real CLI, waits for the app's initial screenshot,
types text, sends BackSpace and Return, clicks the button, and verifies both the
demo's emitted messages and changed screenshot regions. It also exercises Tab
and drags across the entry to select and replace text, then clicks the button.
Shutdown tests check that the recorded child PIDs and private sockets are gone
after SIGTERM and SIGINT. Recording tests decode the resulting MP4s with FFmpeg,
check changing frames, and cover app exit, both shutdown signals, startup failure,
and recorder failure. Both mypy and pyright check the Python code.

## Recording decision

wf-recorder 0.6.0 captured the headless Pixman output and finalized a playable MP4
in the local probe. It uses the compositor's screencopy protocol and handles
encoding and frame timestamps. Continuous capture keeps idle periods in the video
and lets the recorder continue receiving frames during shutdown. Framewisp owns
its process alongside the display tools; it does not implement a frame queue or
feed repeated PNG screenshots into an encoder.

The pinned recorder tags H.264 as full-range. Its default conversion produced
a white canvas decoded as RGB 235 instead of 255. `scale=out_range=full` keeps
the conversion consistent with that tag; recording tests compare a decoded
background patch with the screenshot to catch brightness shifts.

## Capture decision

In the tested Sway 1.12, wlroots 0.20.2, wayvnc 0.10.1, and vncdotool 1.2.0
combination, VNC connected and delivered input but full-frame screenshot requests
timed out. grim captured the same display successfully. The MVP uses grim for
all screenshots instead of patching a display dependency or implementing RFB.
The cause of the VNC capture failure has not been established.
