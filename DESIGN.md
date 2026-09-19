# Design

## Scope

Framewisp gives an agent a CLI loop for one Wayland or X11 application on the
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
  `Applied: TEXT`. A check button with a tooltip, a numbered slider, and a
  two-axis scrolled grid provide visible input feedback. The entry's native
  context menu and word selection cover right/double clicks. Reset restores
  empty text, unchecked option, slider value 25, scroll origin, and entry focus.
  Two columns keep the text/apply controls at their documented coordinates. The
  result label ellipsizes long text so it cannot push the other controls offscreen.
  Widget state changes print to stdout for integration tests; no desktop service
  or third-party app is required.
- Sway provides the headless Wayland display using the Pixman software renderer.
- wayvnc creates virtual pointer and keyboard devices. vncdotool's `vncdo`
  command sends input over a private Unix socket, connecting once per CLI call.
- Xwayland supplies the optional X11 server, owned by Sway. `framewisp/x11.py`
  uses xmodmap and xdotool for Unicode text in that server.
- wtype supplies a temporary Wayland keyboard/keymap for non-ASCII text.
- grim captures the headless output directly to PNG.
- wf-recorder captures the display to H.264 MP4 when `run --record FILE` is used.

There is no separate controller daemon or framebuffer cache.
The runner accepts recording commands over a private Unix socket.
The foreground `run` command is the lifetime owner.

## Startup

1. Create or reuse the requested session directory. Refuse an existing
   `session.json`; concurrent runs and stale-session recovery are unsupported.
   Refuse existing recording destinations and paths reserved for session logs or
   metadata before starting any children.
2. Register SIGINT and SIGTERM handlers that request shutdown.
3. Create a temporary runtime directory. It holds the compositor configuration
   and sockets (including `control.sock`) and is removed when the runner exits normally. Its short path
   avoids Unix socket path limits even when the session log directory is long.
4. Copy the environment, remove inherited display and session-bus addresses,
   and set the private `XDG_RUNTIME_DIR`. Set `WLR_BACKENDS=headless`,
   `WLR_RENDERER=pixman`, `WLR_LIBINPUT_NO_DEVICES=1`, `GDK_BACKEND=wayland`,
   and `GSK_RENDERER=cairo`.
5. Start Sway with a generated configuration: Xwayland disabled, a single
   `HEADLESS-1` output at the requested width and height (default 1280x720) and
   60 Hz, a fallback seat, US keyboard layout,
   and no window borders. Disable primary selection to avoid the observed wayvnc
   crash on automatic selection offers; ordinary clipboard copy/paste stays enabled.
   Load no host Sway configuration. The CLI validates positive integer dimensions
   before starting the session. Recording requires even dimensions, since
   wf-recorder otherwise crops the last row or column.
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
10. Write `session.json` atomically and print `Session ready:`. This indicates process and
   socket readiness, not application rendering readiness.

Each child runs in its own process session with stdin disconnected and combined
stdout/stderr directed to its log. The runner monitors all managed children.
The idle VNC connection runs in the runner's process. Cleanup disconnects it and
stops its Twisted reactor thread before stopping wayvnc.

## X11 mode

`run --x11` changes the generated Sway configuration to `xwayland force`.
A Sway `exec` child writes its `DISPLAY` to a file in the private runtime directory.
The runner waits up to ten seconds for that file, checking shutdown and compositor
exit. It never guesses an X display or uses the host's address. Sway allocates the
X sockets and owns the Xwayland process; stopping Sway removes them.

The app environment omits `WAYLAND_DISPLAY`, sets the discovered `DISPLAY`, uses
`XAUTHORITY=/dev/null` to bypass inherited and home-directory credentials, and
selects X11 for GTK, Qt and SDL. The compositor, VNC server and capture tools keep
the private Wayland environment. The demo prints and displays its actual GDK
backend class so tests and recordings distinguish X11 from Wayland.

ASCII and shortcuts still use VNC. Non-ASCII text uses X11 directly because wtype's
Wayland keymaps do not reach Xwayland correctly. `xmodmap` assigns each distinct
non-ASCII character one of 128 reserved upper codes (120 through 255, skipping
the US modifier codes), above the supported US keyboard and navigation keys. These mappings stay installed after typing; xdotool's
transient mappings otherwise disappear before GTK processes the events at zero
delay. The helper reasserts the current X window focus before XTest input so GTK receives
the first character after pointer input. `xdotool key --delay 0` sends numeric codes for these characters and literal
hexadecimal ASCII keysyms for the rest. `sleep` commands add the requested interval
only between characters. More than 128 distinct non-ASCII characters returns an
error before changing the keymap or sending input. Commands remain sequential;
concurrent typing or arbitrary custom X11 keymaps are unsupported.

Integration tests force GTK onto X11, poison inherited display credentials, check
the rendered demo and input state, and exercise pacing, Unicode, clips and captions.
They also check that private X server processes and sockets disappear on shutdown
and failed application startup. The package check runs these tests without a host
display, development shell or external app installation.

## Interaction

All CLI commands use `framewisp SESSION COMMAND ...`, with a required positional
session directory before the subcommand. There is no default session.

CLI calls read `session.json` to locate the display and VNC socket. The JSON has
`runtime_directory`, `wayland_display`, `x11_display` (null for Wayland), and `processes` keys. The last is a map
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

Clicks and drags accept repeatable `--modifier ctrl|shift|alt`. The CLI
normalizes case and rejects unknown or duplicate modifiers before reading the
session. `send_input` prefixes the gesture with modifier keydowns and suffixes
it with keyups in reverse order, all on the same connection. GTK tests verify
modifier state during the gesture, release order, and a subsequent plain click.

`drag [--button left|right] --duration SECONDS X1 Y1 X2 Y2`
moves to the start, presses the chosen button (default: left), and sends linearly interpolated
integer coordinates at approximately 60 steps per second before releasing at
the endpoint. The number of steps is `max(1, ceil(duration * 60))`, with a pause
of `duration / steps` before each move. Zero duration sends one endpoint move
without a pause. The default is 0.4 seconds. This is a straight path at a steady
pace; it has no random variation or easing.

`type --interval SECONDS TEXT` accepts characters satisfying Python's
`str.isprintable`, rejecting control and format characters before session access.
For ASCII text it sends one `vncdo type CHARACTER` command per character, with
explicit pauses only between characters.
The default interval is 0.08 seconds; zero disables pauses. Empty text sends
no keys and adds no duration. The CLI rejects negative and nonfinite timing values.

For text containing non-ASCII characters, call wtype in the session's Wayland
environment. Its generated keymap supports characters absent from wayvnc's US
keymap. Pass each character as `-k UXXXX` (hexadecimal Unicode code point),
separating characters with `-s MILLISECONDS` when the interval is nonzero. This
avoids interpreting text as options and adds no trailing interval. Round intervals
up to whole milliseconds. The subprocess timeout is fifteen seconds plus those
intervals and wtype's 4 ms per-character key press/release time. No clipboard or
input-method composition is involved.

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

All steps of a VNC input operation use one connection. Each operation presses
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
wf-recorder, vncdotool, wtype, Xwayland, xmodmap, xdotool, Bash (for Sway exec), and its own bin directory (for the bundled demo). The
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
demo's emitted messages and changed screenshot regions. It also exercises Shift+Tab
and drags across the entry to select and replace text, then clicks the button.
Demo widget tests exercise the native context menu, double-click word selection,
slider drag and arrow adjustment, both scroll axes and reversal, tooltip appearance,
Space activation, and reset. Assertions inspect resulting widget state and screenshots.
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

## Recording controls

The runner owns a `Recordings` object with an optional recorder process and an
`ExitStack` for its lifetime. `run --record FILE` and `record-start FILE` use the
same startup path. `record-stop` closes that recorder's stack, finalizes the MP4,
and clears its process reference without stopping the app. Runner cleanup closes
any active recorder before stopping the display.

The runner listens on `control.sock` in its private runtime directory. Each
recording CLI call sends one newline-terminated JSON request with a `destination`
(an absolute path for start, null for stop), and a `captions` boolean, then waits for a JSON response with
an `error` string or null. The runner handles requests serially in its monitoring
loop. It replies only after capture is ready or finalization has finished.
Validation/startup errors are returned to the caller without stopping the app.
Unexpected recorder exit or finalization failure still fails the session.

State updates use `.session.json` followed by an atomic rename. The `processes`
map includes `recorder` only while recording. Both metadata filenames are reserved
along with the session logs when choosing recording destinations. Recording
requests reject odd display dimensions, existing/reserved output paths, a start
while already active, and a stop while inactive. Each start replaces `recorder.log`.

## Input logs and captions

The CLI validates input arguments, then appends a start event to `inputs.jsonl`
before dispatch and an end event afterward. Each JSONL event carries an ID,
`event`, monotonic `time` in seconds, `action`, `parameters`, `returncode`, and
`error`. Exceptions are logged and re-raised with their traceback. Normal
nonzero returns are recorded as failures. Input logging is independent of
recording. The runner truncates the log when creating a new session.

For captioned recordings, only wf-recorder receives `WAYLAND_DEBUG=client`.
The first screencopy `ready` event in `recorder.log` supplies the first captured
frame's timestamp. The pinned Sway backend uses the monotonic clock, and the
pinned wf-recorder makes this frame time zero. This establishes each clip's
origin without guessing from subprocess startup or MP4-header detection.
The diagnostic log grows throughout capture and is replaced for each clip.

After recorder finalization, `captions.py` selects commands started between that
origin and the stop request. Commands that began before the clip are excluded,
even if they finish during it. Each caption spans its command, or at least 0.8
seconds for quick commands, capped at the next action or clip stop. An unfinished
command spans to clip stop. Captions show requested actions; they do not claim
application acknowledgement. The video keeps its original idle and input timing.

FFmpeg/libass renders an ASS script into a temporary H.264 MP4 beside the requested
output. Caption rendering uses a bundled Fontconfig configuration passed only to
FFmpeg, including Noto Sans, CJK, and monochrome emoji. On success, the rendered
file replaces the raw recording; on failure, the raw MP4 remains and the command
fails with the `captions.log` path. The final video preserves dimensions, 30 fps,
full-range color, and no audio. `record-stop` waits for rendering, as does normal
session cleanup. With `--no-captions`, rendering and timestamp extraction are
skipped, but input logging remains enabled. Caption formatting cannot affect
app screenshots. Both `inputs.jsonl` and `captions.log` are reserved session paths.


## Attached desktop sessions

`attach.py` owns a foreground connection to the user's existing desktop. It does
not launch or own that desktop's app or compositor. The CLI requires a controlling
terminal with this process group in the foreground, prints stop-binding and access
instructions, and requires the exact typed confirmation ATTACH before creating any
portal connection. Agents receiving a rejection must ask the user to run the
command in another terminal. There is no noninteractive override. This prevents
accidental startup, not deliberate bypass by another program under the same UID.

`portal.py` uses a private Gio connection to the user's session bus to create a
RemoteDesktop session, select keyboard and pointer devices, select one monitor
through ScreenCast, and request consent with Start. Every attachment requests fresh
permission. Closing the private bus connection revokes all its portal sessions;
process death also closes that connection. No access-owning child processes exist.
SIGINT, SIGTERM, SIGHUP, and SIGTSTP request shutdown. Loss of terminal foreground
ownership or the portal's Closed signal also stops access.

An exclusive flock on $XDG_RUNTIME_DIR/framewisp/desktop.lock limits desktop access
to one owner per user runtime directory, including while permission is pending.
The directory must be private and owned by the user. The lock inode is never
removed. After acquiring the lock, a new owner removes stale socket and global
metadata files. Atomic JSON writes publish global desktop.json and session.json;
the latter records kind, the runtime directory, an attachment token, and the PID
for inspection. Commands carry the token, so stale session metadata cannot direct
input to a later attachment using the same global socket.

`connection.py` sends newline-delimited JSON requests over control.sock. A listener
thread accepts requests; workers serialize actions with a lock. Before portal
consent they reject input. Paced actions and queued workers check a shared stop
Event. Unexpected worker errors stop access and print a traceback. Shutdown closes
the portal before bounded waits for workers, then removes state under the ownership
lock. Held buttons and keys release during action cleanup; on connection loss the
compositor removes the virtual input devices and releases their held inputs.

`framewisp --detach` connects to a separate detach.sock, checks its peer UID, and
uses Linux SO_PEERPIDFD (6.5+) to obtain a stable handle to the socket owner. It
sends SIGTERM and waits up to 500 ms, then SIGKILL if needed, and reports success
only after process exit. This avoids PID reuse and works when the owner is stopped
or its command workers are blocked. The emergency socket never queues behind
ordinary commands. Stale files after SIGKILL do not hold the kernel lock. Detach
never signals the user's apps or compositor.

Pointer coordinates use screenshot pixels and the portal stream node. Portal
Notify methods deliver absolute motion, buttons, wheel steps, and keyboard
keycodes for held keys and shortcuts, with keysyms for literal text. Keycode
names currently assume the tested desktop's US layout. Each screenshot opens a PipeWire remote file descriptor through the
portal and passes it to an in-process one-frame GStreamer pipewiresrc pipeline.
videoconvert and pngenc write the PNG. Cancellation tears down the pipeline; process
death closes every capture descriptor. A ten-second limit reports capture failures
with capture.log. Nix supplies GStreamer and the
PipeWire, base, and good plugins in both the installed wrapper and dev shell.

The user configures a desktop binding for `framewisp --detach`. COSMIC on
this host does not expose the GlobalShortcuts portal. The binding is independent
of which app has focus. Attached recording is explicitly rejected; input JSONL
logging still happens in the command client.

The tested COSMIC portal initializes its EI sender lazily on the first Notify
call. Attach sends zero relative motion and allows 100 ms for that setup before
announcing readiness. Clicks, drags, and wheel input allow 50 ms between initial
pointer placement and button/wheel events. Drags also pause after pressing
the button and before releasing it, so a zero-duration move stays inside the
held-button interval. Immediate combined motion and clicking missed the
intended widget in the live test. These waits are interruptible.

COSMIC matches the current modifier combination, including injected modifiers.
A Ctrl+Alt+Escape binding alone did not stop a Shift-drag. The user must also bind
Ctrl+Alt+Shift+Escape to the same detach command.
