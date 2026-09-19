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
   Open one idle connection through vncdotool's threaded API and wait up to ten
   seconds for its handshake by calling `pause(0)`. Keep it connected for the
   session so the seat retains a keyboard and pointer between CLI commands.
   Without it, Qt dismisses context menus when the last input client disconnects.
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
The idle VNC connection runs in the runner's process. Cleanup disconnects it and
stops its Twisted reactor thread before stopping wayvnc.

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

`move X Y` invokes `vncdo move X Y` without button commands. Movement is
immediate. Use `screenshot --delay` to wait for app-defined hover feedback.
A GTK test verifies received coordinates, no button events, a visible tooltip,
and its disappearance after moving away.

`scroll X Y DIRECTION --steps N` moves the pointer to `(X, Y)` and sends `N`
wheel-button press/release pairs on the same VNC connection. The VNC buttons are
4 for up, 5 for down, 6 for left, and 7 for right. After the last release, the
connection stays open for 100 ms. Without that pause, GTK sometimes discarded
queued wheel events after wayvnc removed the pointer device, logging an invalid
seat error. This is a measured workaround, not an input-delivery acknowledgement. `N` is a positive integer,
defaulting to one. The widget under the pointer determines the amount scrolled;
there is no pixel-distance guarantee or smooth scrolling. The CLI rejects invalid
counts and directions before input. A two-pane GTK test checks received wheel
counts, axis direction, pane targeting, and returning to the starting position.

`click [--button left|right] [--count 1|2] X Y` moves to the requested position
and sends one or two complete button press/release pairs. Left maps to VNC
button 1 and right to button 3. Defaults are left and one. A double-click uses
one connection with a 0.1-second pause between clicks; recognition depends on
the target app's settings. Unsupported buttons and counts are rejected before
reading the session. Plain `click` invokes `vncdo move X Y click 1`.

`drag --duration SECONDS X1 Y1 X2 Y2`
moves to the start, presses the left button, and sends linearly interpolated
integer coordinates at approximately 60 steps per second before releasing at
the endpoint. The number of steps is `max(1, ceil(duration * 60))`, with a pause
of `duration / steps` before each move. Zero duration sends one endpoint move
without a pause. The default is 0.4 seconds. This is a straight path at a steady
pace; it has no random variation or easing.

`type --interval SECONDS TEXT` sends one `vncdo type CHARACTER` command per
printable ASCII character, with explicit pauses only between characters.
The default interval is 0.08 seconds; zero disables pauses. Empty text sends
no keys and adds no duration. The CLI rejects negative and nonfinite timing values.

`key CHORD` accepts one letter, digit, or named key, prefixed by zero or more
`Ctrl+`, `Shift+`, or `Alt+` modifiers. Names are case-insensitive, and letter case
does not imply Shift. Named keys are Space, Return, Tab, BackSpace, Escape,
Delete, Left, Right, Up, and Down. `key_commands` validates the complete
combination before input, rejecting unknown keys, empty components, and repeated
modifiers. It maps names to vncdotool's vocabulary (`enter`, `bsp`, `esc`, etc.),
then emits `keydown` for each modifier, `key` for the final key, and `keyup` for
each modifier in reverse order. With Shift held, letters and digits use the
shifted US-layout symbol, and Tab uses ISO_Left_Tab. wayvnc otherwise adjusts
modifier state to produce the unshifted symbol, removing the intended shortcut
modifiers. vncdotool lacks a name for ISO_Left_Tab, so this one keysym is encoded
as `chr(0xFE20)`; its single-character path sends the ordinal as the RFB keysym.
There are no held keys across commands.

All steps of one input operation use one VNC connection. Each operation presses
and releases its buttons or keys within that connection. vncdotool's implicit
command delay is disabled so only our explicit pauses control pacing. Each
connection waits 100 ms before sending input: without that delay, Sway dropped
the first key while focusing the newly created virtual keyboard in the tested
environment. This is a measured workaround for this setup, not a general
readiness guarantee.

The requested input duration is the drag duration or `(len(text) - 1) * interval`
for nonempty text. VNC's client deadline is ten seconds plus that duration,
rounded up to whole seconds. The subprocess deadline is fifteen seconds plus
that duration. These allow paced input to exceed the base deadlines while
retaining timeouts for stalled tools. Timing is approximate and includes process,
connection, and scheduling overhead.

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
and recorder failure. A separate GTK input probe logs received pointer events, button releases, and
text changes with monotonic timestamps. Click tests check coordinates, left and
right button identity, paired releases, and GTK's recognized click count for
single and double clicks. Tests also check the drag path and timing,
and verify that the app has pointer and keyboard devices before the first input
and receives no device removal between commands. Other tests check
character spacing, zero timing, and typing that exceeds the base timeouts without
an extra interval after the final character. The probe also checks shortcut
press/release order and modifier state, followed by an unmodified key. Entry
editing tests exercise Ctrl+A, Shift+Right, arrows, Delete, and BackSpace. Both mypy and pyright check the
Python code.

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
