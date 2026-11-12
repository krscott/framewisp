# Design

## Scope

Framewisp gives an agent a CLI loop for one Wayland or X11 application on the
current x86_64 NixOS environment: launch, screenshot, click, drag, type, screenshot,
stop. Sessions can optionally record a silent MP4. The included GTK 4 demo is the
acceptance application.

## Components

- `framewisp/__main__.py` handles standalone skill output and emergency detach
  before importing the GUI command dispatcher in `framewisp/cli.py`.
- `framewisp/errors.py` describes expected operational failures. The CLI reports
  them without a Python traceback, with session context. Display startup errors
  include the last 12 lines from at most 4096 bytes of the component log. Failed
  headless display tools include their stderr, display/socket context, and a
  conditional sandbox-access explanation. Unexpected programming errors still
  propagate. Portal imports are deferred to `attach`; vncdotool's Twisted API
  is imported only by the headless runner that owns the persistent input connection.
- `framewisp/lib.py` owns process lifetime and invokes existing display tools.
  `run_session` coordinates startup, monitoring, and shutdown. Separate helpers
  prepare its environment and manage Sway, wayvnc, and optional recorder startup,
  waiting for sockets or the recording header. Each startup helper uses `managed_process` for cleanup and
  yields `None` if shutdown is requested while waiting.
- `framewisp/console.py` drains the launched app's combined stdout/stderr to raw
  `app.log` bytes and timestamped UTF-8 chunks in `console.jsonl`. It also builds
  bounded scrolling text states for optional console panels in recordings.
- `framewisp/layout.py` is the one calculation of a clip's video size, GUI
  rectangle, and console panel rectangle. It also validates panel requests.
- `framewisp/pings.py` selects a clip's `ping` records from `inputs.jsonl` and
  builds the ASS drawing for each click ping and drag hold disc.
- `framewisp/demo.py` displays a text field, button, and result label. Return
  changes the label to `Entered: TEXT`; clicking the button changes it to
  `Applied: TEXT`. A check button with a tooltip, a numbered slider, and a
  two-axis scrolled grid provide visible input feedback. The entry's native
  context menu and word selection cover right/double clicks. Reset restores
  empty text, unchecked option, slider value 25, scroll origin, and entry focus.
  Two columns keep the text/apply controls at their documented coordinates. The
  result label ellipsizes long text so it cannot push the other controls offscreen.
  The demo uses the bundled font configuration and DejaVu Sans 11 so a fresh HOME
  or different desktop font setting cannot move its documented targets.
  Widget state changes print to stdout for integration tests; no desktop service
  or third-party app is required.
- Sway provides the headless Wayland display using the Pixman software renderer.
- `framewisp/inputs.py` validates and serializes headless actions on one worker.
  `framewisp/keys.py` defines supported keys and shortcut translation.
- wayvnc creates virtual pointer and keyboard devices. The runner uses
  vncdotool's threaded API over one private Unix socket connection.
- Xwayland supplies the optional X11 server, owned by Sway. `framewisp/x11.py`
  uses xmodmap and xdotool for Unicode text in that server.
- wtype supplies a temporary Wayland keyboard/keymap for non-ASCII text.
- grim captures the headless output directly to PNG.
- wf-recorder captures the display to H.264 MP4 when `run --record FILE` is used.

The README and bundled agent skill describe publishing finished PNG/MP4 evidence
with GitHub CLI's `--attach` flag, its access and size limits, and the browser
fallback. Media publication stays outside Framewisp's CLI and runtime dependencies.

There is no separate controller daemon or framebuffer cache.
The runner accepts input, recording, status, and stop commands over a private Unix socket.
The foreground `run` command is the lifetime owner.

`compact.py` defines the fixed padding and command-based cut planning for
`run --record --compact` and `record-start --compact`. Each clip chooses
compaction with a `compact` boolean. The `compact_recording` metadata capability
prevents newer clients from silently requesting compaction from an older runner.
Input start/end records in
`inputs.jsonl` supply full input spans; `inputs.py` also writes timestamp-only
start/end records for batch checks and capture delays to `activity.jsonl`,
without conditions or error text. Both logs reset at session startup, and their
paths are reserved against recording/capture destinations. A missing end record
keeps its span through clip stop; commands crossing either clip boundary are
clipped to the recording. An in-progress append cannot discard the earlier start
because cut planning reads only complete lines through `activity_events`.

Cut planning adds 1 second of padding before and 2 seconds after activity spans,
keeps the opening 1 second and closing 2 seconds, rounds ranges outward to 30 fps
frames, and merges touching or overlapping ranges. Each click ping starts inside
its input's span and is shorter than the after-padding, so the padding keeps its
whole animation. A hold disc moves only inside its drag's span, and its fade is
as long as a ping (R-REC-8). Compaction requires the recorder's first-frame
monotonic timestamp even with captions and console disabled. Finalization renders
overlays on the original timeline. ffprobe supplies the encoded duration for
closing cuts, independent of stop/encoder latency. The renderer
normalizes to 30 fps, selects retained frames, then
assigns contiguous 30 fps timestamps. A filter script file avoids exec argument
limits for many cut ranges. Rendering uses one FFmpeg pass and replaces the raw
MP4 only on success. The ordinary recording summary measures the final video.
Console timestamps retain original clip time; original logs are never rewritten.
No visual inactivity detector runs, and uncommanded app activity in gaps can be
omitted. Explicit batch waits remain in full; external sleeps can be cut.

## Startup

The Nix package adds `-I` to the `framewisp` and `framewisp-demo` entry-point
shebangs before Python fixup writes the dependency paths into each script.
Python ignores inherited import settings and user site-packages, then the script
adds the packaged dependency paths (R-ENV-2). The environment variables remain
available to target apps and their activated services.
The internal process supervisor and X11 display probe also use `-I`; both need
only the standard library. Editable installs use the development interpreter
without this entry-point isolation. The standalone package check tests foreign
`gi` and `sitecustomize` modules, invalid `PYTHONHOME`, Wayland/X11 demo
startup, and unset, empty, and populated app `PYTHONPATH` values.

The installed `framewisp` command first runs a standard-library-only launcher
with isolated Python, before entering any generated Nix wrapper. `_launch.py`
captures the complete caller exec environment from `/proc/self/environ` in an
anonymous `memfd`, then executes the wrapped runtime. Only the descriptor number travels in
`FRAMEWISP_CALLER_ENV_FD`; environment contents never enter a temporary file or
an extra exec argument/environment string. `__main__.main` consumes and closes
the descriptor through `environment.initialize_environment` before dispatch.
The runtime keeps Nix's environment for its imports and tools. Target apps use
the captured caller environment with the private session settings applied,
sanitized unless `run --real-env` is given (R-ENV-1, R-ENV-3, R-ENV-8).
This boundary covers every wrapper variable without tracking which variables
Nix adds, prefixes, or defaults, and preserves unset and empty values. Reading
the initial exec environment also excludes Python's startup locale coercion.
Editable installs use the current environment directly.
The bare `framewisp-demo` command falls back to the runtime's bundled executable
when it is absent from the caller's PATH. This supports `nix run` without
modifying the target environment. Caller-provided demos take precedence;
other commands and explicit executable paths receive no fallback.

The two private D-Bus daemons also start with the app environment, so activated
services cannot inherit wrapper defaults for variables absent in the caller.
The runtime resolves the daemon executable through its own PATH before running it
with the app environment. The registry, compositor,
VNC server, and capture tools keep the runtime environment. Sanitizing applies
to the app and its buses. The runner gives its own tools the same font file
through `environment.fixed_fonts`, so compositor title bars and captions cannot
use the caller's fonts (R-ENV-4, R-REC-23). An empty `XCURSOR_PATH` keeps the
compositor on its built-in cursor (R-ENV-4). The process supervisor also reads its original exec environment
and passes it explicitly to each managed child, so its Python startup cannot
add locale variables to the child's environment.
The supervisor runs with the current interpreter binary from `/proc/self/exe`,
bypassing any command wrapper named by `sys.executable`.
Launcher tests cover arbitrary future wrapper variables, Unicode, non-UTF-8
bytes, multiline values, large environments, and descriptor cleanup. Installed
package tests check exact unset/empty/populated wrapper-variable values and
launch a module through a caller-created venv using the bare `python` command.

`framewisp --agent-skill` reads the UTF-8 body from `framewisp/SKILL.md`, writes it
unchanged to stdout, and exits successfully before importing GUI/session tools.
Setuptools includes that file in package data. The flag takes no session or other
arguments; normal CLI help lists it. A test runs this path without site packages
or external commands. The Nix package check compares its output in an empty
environment against the source file. The skill covers the public Nix URL fallback,
all commands and options, GUI workflows, user-started attachment, and permission
before filing issues. For repeated interactions, it describes optional delegation
to a fast subagent or a host-provided classifier workflow. The main agent chooses
the model and whether to use accessibility output, screenshots, or both. Session
handoffs use absolute paths and one controlling agent at a time. Delegates return
verification evidence or blockers; orchestration remains outside Framewisp.

1. Create or reuse the requested session directory. Reject directories not owned
   by the caller or writable by group/others (R-SESSION-6). Acquire a
   nonblocking exclusive flock on the persistent `.headless.lock` (regular,
   single-link, private file, opened with O_NOFOLLOW). Refuse an existing
   `session.json` or ownership journal; ask the caller to recover abandoned
   ownership first. Refuse existing recording destinations and paths reserved
   for session logs or metadata before starting any children.
2. Register SIGINT and SIGTERM handlers that request shutdown.
3. Choose `<tempdir>/framewisp-UID-DEVICEHEX-INODEHEX`, using Python's
   `tempfile.gettempdir()` (`TMPDIR` when writable, normally `/tmp` otherwise)
   (R-SESSION-5).
   Before creating the runtime or starting children, check the socket path budget
   against Linux's 108-byte limit, including the terminating NUL. Reserve space
   for Sway's UID and a ten-digit PID, the longest managed socket filename.
   An excessive length reports the runtime path and byte budget and asks for a
   shorter `TMPDIR`. Atomically write `.headless-owner.json` with the session
   device/inode, the held lock's device/inode, the absolute runtime path, and a
   null runtime identity. Create the runtime with mode 0700, then atomically
   update the journal with its device/inode. It holds compositor configuration and sockets
   (including `control.sock`) and is removed after successful cleanup.
4. Prepare separate runtime and caller app environments. Remove inherited display and session-bus addresses,
   and set the private `XDG_RUNTIME_DIR`. Set `WLR_BACKENDS=headless`,
   `WLR_RENDERER=pixman`, `WLR_LIBINPUT_NO_DEVICES=1`, `GDK_BACKEND=wayland`,
   `QT_QPA_PLATFORM=wayland` and `GSK_RENDERER=cairo`. For the app, these
   session settings apply after sanitizing, so they always win over it. Private
   D-Bus addresses percent-escape runtime filesystem bytes so temporary paths
   with spaces or address delimiters work.
   `environment.sanitized_environment` is the one step that sanitizes the app
   environment (R-ENV-1, R-ENV-4). It creates private HOME/config/cache/data/
   state/tmp directories under this runtime (R-ENV-6), removes inherited locale,
   toolkit, font and desktop identity settings, and fixes `C.UTF-8`, `TZ=UTC` and
   the font file. That one Nix-built Fontconfig file lists only store font
   directories and Fontconfig's stock aliases, leaving out the stock user and
   local rules and the XDG font directories, which would all load host fonts.
   It writes GTK 3/4 settings.ini with Adwaita, DejaVu Sans 11,
   light theme and 96 DPI, and selects GSettings keyfile storage and Qt
   Fusion/software rendering at 96 DPI. It is a denylist: lookup paths and
   app-specific variables pass through, because no fixed list can name them
   (R-ENV-3). Callers change the result with `env` on the app command, which
   runs after this step (R-ENV-5). These directories
   share runtime ownership, shutdown and recovery; nothing in them survives
   successful cleanup, including read-only directories an app leaves behind. See
   README for compatibility limits. `run --real-env` skips only this step, so the
   app gets the caller's environment with the session settings applied and no
   private directories are created (R-ENV-8). Framewisp's own tools keep their
   fixed font file and cursor in both cases.
5. Start Sway with a generated configuration: Xwayland disabled, a single
   `HEADLESS-1` output at the requested width and height (default 1280x720) and
   60 Hz, a fallback seat, US keyboard layout,
   and no window borders. Ordinary clipboard copy/paste and primary selection
   stay enabled within the session.
   Load no host Sway configuration. The CLI validates positive integer dimensions
   before starting the session (R-INPUT-3). Recording requires even dimensions
   (R-REC-2), since wf-recorder otherwise crops the last row or column.
6. Wait up to ten seconds for its Wayland socket, checking for process exit.
   Set `WAYLAND_DISPLAY` to the discovered socket name.
7. Start wayvnc with an empty configuration, US layout, `--disable-clipboard`, and a Unix socket in
   the private runtime directory. Wait up to ten seconds for that socket.
   The Nix overlay applies `patches/wayvnc-disable-clipboard.patch` for this option.
   It skips per-client data-control devices, disabling VNC clipboard forwarding
   while keeping keyboard and pointer input. Short-lived clients can otherwise
   destroy their devices before pending
   clipboard offers arrive. Libwayland discards those offers, leaving gaps in its
   server object map that can make a later offer fail with an invalid object ID.
   Application clipboard and primary-selection transfers do not use these devices
   (R-INPUT-6).
   Open one connection through vncdotool's threaded API with a ten-second
   handshake timeout and a 100 ms focus-settling pause. Then reduce the timeout
   to one second per API call. Keep it connected for input throughout the
   session so the seat retains a keyboard and pointer between CLI commands
   (R-INPUT-1).
   Without it, Qt dismisses context menus when the last input client disconnects.
8. If recording, start wf-recorder for `HEADLESS-1` with continuous capture (`-D`),
   30 fps, software `libx264`, `yuv420p` with explicit full-range conversion, and
   the MP4 muxer. It writes to a private clip, not the destination (see
   "Recording destinations"). Wait up to ten seconds
   for a nonempty clip, checking for recorder exit. With the pinned
   wf-recorder, the MP4 header is written after the first frame is received.
9. Launch the application with the private environment and no shell expansion.
10. Write `session.json` atomically and print `Session ready:`. This indicates process and
   socket readiness, not application rendering readiness.

Each child runs in its own process session with stdin disconnected and combined
stdout/stderr directed to its log. App output passes through a continuously
drained pipe to preserve raw bytes and record receipt timestamps. The runner
monitors all managed children and app-output worker failures.
The persistent VNC connection runs in the runner's process. Cleanup disconnects it and
stops its Twisted reactor thread before stopping wayvnc.

## X11 mode

`run --x11` changes the generated Sway configuration to `xwayland force`.
A Sway `exec` child writes its `DISPLAY` to a file in the private runtime directory.
The runner waits up to ten seconds for that file, checking shutdown and compositor
exit. It never guesses an X display or uses the host's address. Sway allocates the
X sockets and owns the Xwayland process; stopping Sway removes them.
The Nix overlay builds framewisp's Sway against wlroots with
`patches/wlroots-xwayland-map-race.patch`, so X11 windows whose first frame
arrives before their Xwayland surface pairing still map (R-DISPLAY-1).

The app environment omits `WAYLAND_DISPLAY`, sets the discovered `DISPLAY`, uses
`XAUTHORITY=/dev/null` to bypass inherited and home-directory credentials, and
selects X11 for GTK, Qt and SDL. The compositor, VNC server and capture tools keep
the private Wayland environment. The demo prints and displays its actual GDK
backend class so tests and recordings distinguish X11 from Wayland.

ASCII and shortcuts still use VNC. Non-ASCII text uses X11 directly because wtype's
Wayland keymaps do not reach Xwayland correctly. `xmodmap` assigns each distinct
non-ASCII character one of 128 reserved upper codes (120 through 255, skipping
the US modifier codes), above the supported US keyboard and navigation keys.
The runtime directory's `x11-keymap.json` records allocated codes. Mappings stay
installed for the session and never change meaning, so an app with a blocked event
loop can consume queued keys after later commands finish. xdotool's transient
mappings otherwise disappear before GTK processes the events at zero delay.
The helper first sends `keyup Shift_L` and waits 100 ms to initialize Xwayland's
XTest keyboard; sending text as its first event loses the first character.
`xdotool key --delay 0` sends numeric codes for these characters and literal
hexadecimal ASCII keysyms for the rest. `sleep` commands add the requested interval
only between characters. More than 128 distinct non-ASCII characters across the
session returns an
error before changing the keymap or sending input. A new session starts with an
empty allocation. The input worker serializes concurrent typing requests; arbitrary custom X11 keymaps are unsupported.

Before the first X11 pointer command, the worker primes Xwayland with a one-pixel
motion and a 100 ms pause beside the tracked position if known, then restores
it. With no tracked position, it primes beside the destination.
Xwayland drops the first pointer motion in the tested setup; without this step,
an initial drag can disappear. Later commands reuse the initialized pointer.
The requested drag timing starts after its press.

Integration tests force GTK onto X11, poison inherited display credentials, check
the rendered demo and input state, and exercise pacing, Unicode, clips and captions.
They also check that private X server processes and sockets disappear on shutdown
and failed application startup. The package check runs these tests without a host
display, development shell or external app installation.

## Interaction

All CLI commands use `framewisp SESSION COMMAND ...`, with a required positional
session name or explicit path before the subcommand. There is no implicit session.
`framewisp/sessions.py` resolves the original string argument before either
headless or attached dispatch. A slash marks an explicit path, which uses
`Path.resolve()`. Empty names, NUL, bare `.`/`..`, and bare names
over 255 filesystem bytes are rejected (R-SESSION-4).

For a bare name, run `git -C <canonical cwd> rev-parse --show-toplevel` with a
five-second timeout, ignoring inherited `GIT_DIR`, `GIT_WORK_TREE`, and
`GIT_COMMON_DIR` overrides. Canonicalize the resulting worktree root; if not in
a worktree, use canonical cwd. Run Git with the C locale, capture bytes, and
decode the root with `os.fsdecode` to preserve non-UTF-8 filesystem names. Only
Git's "not a git repository" error permits the cwd fallback; other failures
report an actionable error instead of silently changing project scope. Hash its
filesystem bytes with SHA-256, take the first 24 hexadecimal characters, and
resolve `<tempdir>/framewisp-project-<hash>/NAME` under `tempfile.gettempdir()`.
The hash is stable across processes and separates worktrees (R-SESSION-2), while
canonical paths make subdirectories and symlink aliases agree (R-SESSION-1). Git
is a packaged runtime and test dependency; if unavailable, report an actionable
error rather than silently choosing a different project. Explicit paths do not
require Git. No local or legacy directory fallback occurs for names
(R-SESSION-3). Short runtime socket paths are independent of the session
directory.

Agents sharing a project must choose distinct names. Startup diagnostics print
the resolved directory, which can be used explicitly from another project or an
environment with a different `TMPDIR`. Bare names under different temporary
directories select different sessions.
Desktop attachment remains user-started, and `--detach` is session-independent.
The resolver does not change session ownership checks, shutdown, recovery, or
sandbox socket permissions.

CLI calls read `session.json` to locate the display and control socket. The JSON has
`runtime_directory`, `wayland_display`, `x11_display` (null for Wayland),
`width`, `height`, `recording`, and `processes` keys. `recording` is the active
clip's destination or null. `processes` is a map
of `sway`, `wayvnc`, `app`, and optionally `recorder` to their PIDs; no inherited
environment is saved.

`screenshot` invokes grim for `HEADLESS-1` with PNG output. It obtains a new
capture from the compositor, but makes no claim that preceding input has finished
changing the application. The CLI accepts an optional `--delay SECONDS`, a finite,
nonnegative number defaulting to zero, and sleeps for that duration before
invoking capture. The grim process still has its own ten-second deadline; the
delay does not count toward it. Consumers choose the delay and capture again
when needed; there is no automatic animation detection.

Headless screenshots accept `--region X Y WIDTH HEIGHT` in display pixels.
The CLI rejects a negative origin, nonpositive dimensions, and any rectangle
that extends past the session's display dimensions (R-INPUT-3). Crops use grim
`-g "X,Y WIDTHxHEIGHT"`
without `-o`; full captures retain `-o HEADLESS-1`. Neither changes resolution.
`--json` prints `path` (absolute), `x`, `y`, `width`, `height`, `display_width`,
`display_height`, and `capture_seconds` after success. Timing covers the capture
subprocess, excluding delay and CLI startup. Image-local coordinates map to
display coordinates by adding `(x, y)`; input commands always use display pixels.
Both options require headless sessions with dimensions in their metadata;
older sessions must restart to use them. Plain screenshots remain compatible.

`move X Y` sends VNC pointer motion without button commands.
Use `screenshot --delay` to wait for app-defined hover feedback.
A GTK test verifies received coordinates, no button events, a visible tooltip,
and its disappearance after moving away.

Headless pointer approaches for `move`, `click`, `scroll`, and drag starts
accept `--move-duration`, defaulting to 0.3 seconds. A shared constant supplies
CLI, batch, and direct-request defaults. Validation requires finite, nonnegative
values no greater than 300 seconds (R-INPUT-3). Batch JSON uses `move_duration`;
all requested approach durations count toward the shared 300-second pacing
budget. Zero moves immediately. Attached desktops keep immediate positioning and
reject the option.

`InputWorker.pointer_position` starts as `None` and records `(x, y)` after each
successful VNC `mouseMove`. All pointer approaches, X11 priming, and held drag
steps use the same move callback. Targets and sent positions clamp to the
display's pixel bounds so out-of-display commands do not leave tracking stale.
This keeps tracking current during cancelled movement as well as completed
gestures. The persistent worker shares the
position across standalone commands and batches. An unknown starting position
moves immediately; identical tracked start/end positions add no delay. Even in
these cases, sending the target ensures VNC button events use its coordinates.
Apps or other clients can move the compositor cursor independently, leaving
tracking stale; the next animation may jump to the tracked position. This is an
accepted tradeoff to avoid compositor patches and cursor queries. Recording
does not affect tracking.

`pointer.py` uses smoothstep easing and integer coordinates at approximately 60 Hz;
monotonic elapsed time includes transport overhead. Cancellation interrupts the
waits, and the final point is exact. Drag movement while holding a button retains
its separately configured constant-speed timing.

`scroll X Y DIRECTION --steps N` moves the pointer to `(X, Y)` and sends `N`
wheel-button press/release pairs on the same VNC connection. The VNC buttons are
4 for up, 5 for down, 6 for left, and 7 for right. The persistent pointer remains
available to GTK while it consumes queued events, so no disconnect-settling pause
is needed. `N` is a positive integer,
defaulting to one. The widget under the pointer determines the amount scrolled;
there is no pixel-distance guarantee or smooth scrolling. The CLI rejects invalid
counts and directions before input. A two-pane GTK test checks received wheel
counts, axis direction, pane targeting, and returning to the starting position.

`click [--button left|right] [--count 1|2] X Y` moves to the requested position
and sends one or two complete button press/release pairs. Left maps to VNC
button 1 and right to button 3. Defaults are left and one. A double-click uses
one connection with a 0.1-second pause between clicks; recognition depends on
the target app's settings. Unsupported buttons and counts are rejected before
reading the session. Plain `click` moves the pointer and sends one left click.

Clicks and drags accept repeatable `--modifier ctrl|shift|alt`. The CLI
normalizes case and rejects unknown or duplicate modifiers before reading the
session. The worker prefixes the gesture with modifier keydowns and suffixes
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
For ASCII text it sends one VNC key press/release pair per character, with
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

`key CHORD` accepts one letter, digit, ASCII punctuation symbol, or named key,
prefixed by zero or more `Ctrl+`, `Shift+`, or `Alt+` modifiers.
Names are case-insensitive, and letter case does not imply Shift.
Named keys are Space, Return, Tab, BackSpace, Escape,
Delete, Left, Right, Up, Down, Home, End, Page_Up, Page_Down, and F1 through F12.
Punctuation accepts each literal and its X keysym name (listed in the CLI help)
(R-INPUT-2).
`parse_chord` validates and normalizes names to VNC symbols for both headless
and attached input and for log redaction. A final literal plus uses `+` alone
or a doubled separator such as `Ctrl++`; `Ctrl+` remains invalid.
Validation rejects unknown keys, empty components, and repeated modifiers before
input. Names map to vncdotool's vocabulary (`enter`, `bsp`, `esc`, etc.).
`key_commands` emits `keydown` for each modifier, `key` for the final key, and `keyup` for
each modifier in reverse order. With Shift held, letters and digits use the
shifted US-layout symbol, as does unshifted punctuation, and Tab uses ISO_Left_Tab.
Punctuation names map to literal characters, bypassing vncdotool's misleading
`slash` alias (which means backslash). Both backends add an explicit Shift modifier
for shifted punctuation; attachment sends the corresponding US evdev keycode.
Function/navigation keys also have explicit attachment keycodes. wayvnc otherwise
adjusts modifier state to produce the unshifted symbol, removing the intended
shortcut modifiers. vncdotool lacks a name for ISO_Left_Tab, so this keysym is encoded
as `chr(0xFE20)`; its single-character path sends the ordinal as the RFB keysym.
There are no held keys across commands.

The runner queues at most 32 pending inputs and executes one action at a time,
including Unicode helpers. Accepted sockets transfer to the worker; the main loop
continues handling status, stop and recording commands. Validation checks the full
action before queueing, including finite timing, printable text, supported names,
and unsigned 16-bit VNC coordinates (R-INPUT-3). A rejected action leaves the
worker ready for later input (R-INPUT-4).

The CLI waits for a single response. Its disconnect cancels pending or running
input. Paced VNC waits check cancellation every 20 ms; helper subprocess waits
check every 50 ms and terminate, then kill after 500 ms if necessary. Held VNC
keys/buttons release in cleanup (R-INPUT-5). Interrupted XTest helpers
explicitly release their possible held keys; wtype's temporary keyboard
disappears with its process. The worker finishes cleanup before starting another
action. Stop sets the same cancellation event and joins the worker before
stopping the app and recorder. Recorder finalization retains its existing
shutdown time.

A VNC API failure stops the session, disconnects the transport, and rejects queued
input. Connection loss also stops the session. No action is retried automatically;
an API return is not an application acknowledgement. One-second VNC call timeouts
bound stalled API operations independently of intentional pacing. The connection
settles for 100 ms once at startup. Xwayland pointer priming and XTest keyboard
initialization retain their separate 100 ms waits.

## Batched input

`batch --file FILE` loads at most 1 MiB of JSON. `batch.py` parses an object with
`actions` and optional `capture` and `failure_capture`. Each action is a flat object with an `action`
name and the same named parameters as its individual CLI command. Batch defaults
match CLI defaults; modifier arrays use lowercase names. Shared `InputAction`
validation lives in `actions.py`. Reject unknown fields, invalid types, unsupported
inputs, more than 256 actions, more than 16,384 total text characters or 10,000
scroll steps, and more than 300 seconds of requested pacing and check deadlines.
At least one action is required. There are no loops or lifecycle operations.
Conditional checks are specified below. Capture accepts a path and optional finite nonnegative delay.
The CLI resolves its path against its own working directory. The runner requires
an absolute path with an existing parent directory and rejects existing
destinations and reserved session paths.

The CLI sends one control request with `action: "batch"` and the normalized
object in `parameters`. Metadata advertises `batch_input: true`; older runners
are rejected before sending. Control requests are limited to 1 MiB, including
the envelope and trailing newline. The runner repeats all validation before
queueing (R-BATCH-1). Each batch occupies one of the worker's 32 pending slots.
The worker runs each input using its existing logging, pacing, Unicode and
cleanup paths. Other input jobs cannot interleave, including during the optional
final capture (R-BATCH-2). Status and stop remain on the main thread.

Capture calls grim through the cancellable subprocess path and writes into a
temporary directory beside the destination. A hard link publishes the completed
PNG without overwriting a file created since validation (R-REC-1). Cleanup
removes the temporary files on success, failure or cancellation. A capture error
fails the batch but leaves the session running. Captures do not create input
captions.

The response data contains `status` (completed/failed), `error`, `results`,
`completed_actions`, `failed_index`, `failed_phase`, `duration_seconds`,
`capture_seconds`, and `artifacts`. Each attempted input result has zero-based
`index`, `action`, `status`, `error`, and `duration_seconds`. Overall timing starts
when the worker takes the job, excluding queue time. Capture timing includes its
delay. Artifacts list successfully published absolute PNG paths. A failed input
sets its index and phase `action`; failed capture sets phase `capture` and leaves
the index null. Remaining actions and capture are skipped after input failure.
The CLI prints result data even on failure and exits nonzero. Pre-execution
validation/session rejection reports an error without execution results.

Cancellation checks run between actions, during paced waits, and while waiting
for grim or Unicode helpers. Client disconnect or session stop cancels the
remaining job and releases held keys/buttons before the next job. A queued job
cancelled before execution sends no input and creates no input log events.
A disconnected caller cannot receive results; logs and application inspection
are needed to resolve uncertain completion. Nothing is replayed or rolled back.
Transport failure retains the existing stop-session behavior. Capture reports an
artifact, not an application acknowledgement or rendering barrier.

Before an X11 batch sends input, the worker checks all its Unicode characters
against the session's 128-character mapping limit, including mappings allocated
by earlier queued jobs. A capacity failure returns no action results, zero completed
actions, `failed_phase: "validation"`, and the index that exceeds the limit.

## Shutdown and errors

Application exit ends the session and returns its exit code (128 plus the signal
number for signals). Before cleanup, the runner atomically writes `app-exit.json`
via `.app-exit.json`, containing the subprocess `returncode` (negative for signals)
and `time` (Unix seconds). It prints the exit code or signal name and the path to
`app.log` on stderr. Later session commands report this record even during cleanup
(R-LIFE-3).
A new headless run clears the record before startup; an attachment clears it when
it publishes its state. Requested stops do not create an exit record. SIGINT or SIGTERM
requests a clean stop and returns zero. A backend exit or startup timeout reports
an error naming its log. Unexpected errors keep their traceback.

ExitStack stops owned process trees in reverse order: app, optional recorder,
wayvnc, Sway, registry, and private buses. Each `managed_process` starts a
private single-threaded Python supervisor with `PR_SET_CHILD_SUBREAPER` enabled
before launching its command. It reports the real app PID and return code over a
private, non-inherited socket. It resets inherited SIGCHLD dispositions so child
exit statuses remain waitable. Detached and double-forked descendants reparent
to this supervisor when their parents exit (R-LIFE-1). App exit also initiates
tree cleanup. Finalization's FFmpeg and FFprobe calls run to completion through
`run_owned`, each under its own supervisor with the runner's owner settings. That
keeps them out of the runner's process group, so a terminal's Ctrl+C reaches only
the runner, which still renders the clip (R-REC-3). Like the app's supervisor,
each holds the session lease and writes an owner record, so `recover` waits until
a killed runner's helpers have gone. Each call costs a supervisor start, under
0.1 s.

Cleanup enumerates only the supervisor's immediate children through
`/proc/self/task/PID/children`, signals them through pidfds, and reaps them. It
repeats as grandchildren become adopted children, including descendants that
create their own sessions or subreapers. No concurrent thread or signal handler
reaps children, so listed PIDs cannot be reused before pidfd acquisition. SIGTERM
has a five-second grace period, followed by up to two seconds of repeated SIGKILL
and reaping. The runner allows eight seconds for the supervisor response. Errors
name the log and supervisor/app PIDs; deadline failures list remaining children.
Denied signals do not skip cleanup of the remaining children. The recorder log
reader uses cancellable pipe reads and allows one second for EOF, so a surviving
pipe writer cannot block shutdown indefinitely. Cleanup errors make `stop` fail rather than report success. Uninterruptible kernel
I/O or denied signals can prevent cleanup; the time limit bounds waiting, not the
kernel's ability to terminate a process.

Headless ownership requires Linux 5.3 or newer, readable procfs with the task
children file, and permission to use `prctl`, `pidfd_open`, and `pidfd_send_signal`.
The supervisor checks these before launching anything and fails closed if they
are unavailable. It uses the standard library and needs neither root, cgroup
delegation, nor a systemd user service. The Nix package and development shell use
the same implementation. Ownership covers forked descendants, not work requested
from an existing external service. It does not restrict app access to user files
or protect against an app deliberately killing its supervisor.

The runtime directory and `session.json` are removed; logs remain (R-LIFE-2).
The recorder handles SIGTERM by flushing its encoder and MP4 trailer while Sway
is still alive. A nonzero recorder exit during finalization names its
destination and log. Its incomplete clip is not published. A clip stopped within
moments of its first frame can get a zero-length edit list, which hides that
frame and leaves no container duration, so finalization remuxes the private clip
without the edit list before rendering (R-REC-3). A clip that still has no
duration skips rendering and is published with a null duration in its summary
(R-REC-22), and `recorder.log` records any failed remux. A file ffprobe cannot
open is a different failure and stays an error. Socket EOF requests cleanup when
its supervisor survives.

`recover` acquires the same session lease without using `session.json`. Each
supervisor inherits the lease descriptor, while its application does not. The
runner closes the descriptor without LOCK_UN; recovery remains excluded until all
supervisors exit. Before launching children, each supervisor writes an
`owner-PID.json` record in the private runtime. Only successful descendant cleanup
removes that record. Failure retains diagnostic information. A killed supervisor
leaves its record and recovery refuses to remove the runtime, printing manual
cleanup steps.

Recovery verifies the journal's session identity and reads its absolute runtime
path. It refuses a path whose basename does not match the UID and session
identity, or whose status cannot be checked, and a journal without a path
(R-LIFE-5). Recovery checks the runtime's inode, owner, and permissions regardless
of the current `TMPDIR`. Normal cleanup reads the same journal
path. Recovery never signals stored PIDs or uses `session.json` to authorize
cleanup. A null runtime identity permits only rmdir of an empty directory,
covering interruption between mkdir and journal update. Missing runtimes are
safe to finish recovering. Without a committed journal, recovery only clears a
partial temporary journal if no runtime or session metadata exists. Copied
journals, symlink ownership files, and replaced runtimes fail verification
(R-LIFE-5). Reading the journal also compares its lock identity with the
descriptor the caller holds. A replaced or deleted lock leaves recovery holding a
different inode from any live owner, so it refuses with manual cleanup steps
(R-LIFE-5). A live owner compares its own descriptor, so its cleanup succeeds even
if the lock path was replaced. Only lock identity decides whether a lock is the
session's, so a `chmod -R` that adds read access or a hard-linked copy does not
block recovery of the original, and the copy still fails its session identity check.
A hard-linked copy shares the lock's inode, so a session running in either directory
holds the lease for both until the copy's lock is deleted and any session started
from the copy has exited. A lock that is a symlink or
directory, or that the caller cannot open, is refused with steps. These checks catch
copies, damage, and accidental replacement. A journal deliberately rewritten to
match the session, lock, and runtime passes them (R-LIFE-5).

`read_journal` is the one check for every other unreadable journal: an open,
file-type, size, parse, session, runtime-path, runtime-identity, or lock-identity
failure removes nothing and prints manual cleanup steps (R-LIFE-7). A missing
journal with resources remaining prints the same steps. Those steps use the runtime
path derived from the session identity and the current TMPDIR, because a damaged
journal cannot be trusted and a copied one could name another session. Running
recover under the session's TMPDIR prints its real path. `gettempdir()` skips a full
or read-only directory, so these steps and the no-journal check look for the runtime
in `gettempdir()`, `TMPDIR`, and `/tmp`. A location that cannot be checked counts as
holding it, so recovery refuses, names it, and says to restore access. Unsetting
`TMPDIR` only stops the check, so it is offered just for a session known not to have
run there. A missing session
directory has no identity to derive a name from, so those steps list runtime
directory names and the names running processes use. They never single out a
directory to delete, because a starting session has no marked process yet, and a
leftover directory only uses disk. A directory moved within its filesystem keeps its
identity and recovers at its new path. Once the journal is
verified, a replaced, symlinked, or changed runtime, or a null runtime identity with
a runtime that cannot be removed, prints steps using the journal's path
(R-LIFE-7). Every set of manual steps looks for live processes by the runtime
directory's name, which is the same under any TMPDIR. Stale attachment metadata
without a journal gets attachment steps instead, because `run`, `status`, and `stop`
do not apply to it (R-LIFE-7). That is the only use of `session.json`, and it only
selects the message; recovery reads it as possibly damaged input, like the journal.

Once ownership is verified and no supervisor records remain,
recovery removes the runtime, `session.json`, its temporary file, and the
ownership journal (R-LIFE-4). Logs, app-exit diagnostics, inputs, and recordings
remain. It reports success as JSON or a nonzero error identifying resources that
remain. Running again replaces logs.

Each journal commit syncs the temporary file, renames it, then syncs the session
directory, so it survives power loss. The first commit is durable before mkdir, and
the second before any child starts. Without those syncs a filesystem may persist the
rename without the contents, or lose the rename, leaving a session that needs manual
cleanup. A filesystem that cannot sync directories returns EINVAL, which is
ignored. Removal is not synced: after power loss recovery either accepts a journal
with a missing runtime or refuses resources without a journal, with manual steps.

The lock file persists across runs and must not be unlinked or replaced. Acquiring
the lease refuses session directories that the caller does not own or that
group or others can write (R-SESSION-6); deliberate same-user filesystem changes are outside the protocol (R-LIFE-5).
Recovery
cannot prove cleanup if supervisors also die or kernel I/O prevents child exit;
records and runtime remain for manual investigation. Attached sessions acquire this same session lease before writing metadata and
refuse abandoned headless journals. They retain their separate desktop reservation
and emergency-detach workflow.

## Environment and verification

The standalone Nix package wraps both entry points with Python dependencies,
GTK libraries and introspection data, and a PATH containing Sway, wayvnc, grim,
wf-recorder, vncdotool, wtype, Xwayland, xmodmap, xdotool, Bash (for Sway exec), and its own bin directory (for the bundled demo). The
flake exports this package as `packages.x86_64-linux.default` and `framewisp`,
with `meta.mainProgram` selecting the CLI for `nix run`. NixOS and Home Manager
can install the same package onto PATH. No system service is required.

The development shell additionally supplies FFmpeg for test decoding and the
Python development tools. The installed-package shards run the
integration tests against the built package with an empty environment and only the package
FFmpeg, and the Qt QML regression runner on PATH. Python dependencies are also declared
in `pyproject.toml` and `default.nix`. The demo uses a plain GTK window and GLib
loop and runs without a D-Bus session.

The development shell creates a separate `.venv` for each checkout. Nix supplies
runtime dependencies, development tools, and setuptools. The creation hook runs
`python -m pip install --no-deps --no-build-isolation -e .` to install only the
local project. New worktrees reuse dependencies from the Nix store without pip
resolving packages or creating an isolated build environment. Pyright runs as
Nix's Node program. CI creates the development environment with `PIP_NO_INDEX=1`.
Nix is optional for development. Manual environments install `.[dev]` with pip,
including the Python pyright wrapper that provides the `pyright` command. Native
runtime dependencies and Just must be installed separately. The justfile skips
Nix formatting when Nix is absent.

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

The pinned recorder tags H.264 as full-range. Its default conversion produced a
white canvas decoded as RGB 235 instead of 255. `scale=out_range=full` keeps the
conversion consistent with that tag (R-REC-4); recording tests compare a decoded
background patch with the screenshot to catch brightness shifts.

## Recording destinations

wf-recorder gives a destination no protection (R-REC-1). Its only guard is a
startup prompt that treats EOF on stdin as consent, and it later opens the path
with `O_TRUNC` when the first frame arrives. Rendering also renames over the path
it reads. Framewisp therefore keeps both away from the requested path.

`start_recording` creates a private `.framewisp-recording-*` directory beside the
destination, so both share a filesystem. wf-recorder and `render_recording` work
only on `recording.mp4` inside it. After finalization, `destination.hardlink_to`
publishes the clip. That is the only write to the requested path, and `link(2)`
fails rather than replaces, so no race window exists. Batch captures publish the
same way. `recording_path_error` is the early rejection for the common case.

The destination therefore does not exist until finalization, so an existence
check cannot see it. `capture_path_error` rejects it explicitly for batch capture
and headless `screenshot`, comparing resolved paths so a symlink to it is caught
(R-REC-21). An attached session cannot record, so its screenshots have no such
check. Batch validation calls it, and `batch_screenshot` calls it again just
before publishing, because a batch can run for minutes. The headless CLI runs grim
itself, so it reads the destination from `recording` in `session.json` rather than
asking the runner: `record-stop` renders in the control loop, and a request would
wait for rendering. `session.json` is rewritten before `record-start` replies. A
runner from an older framewisp writes no `recording` key, so a screenshot there
still works, without this check.

The check is not atomic with the write. A standalone screenshot may replace an
ordinary file, so it cannot use `O_EXCL`. Two windows remain:

- A screenshot sent while `record-start` is still running passes, because the
  destination is published only after the clip starts. That can take up to ten
  seconds.
- A batch capture publishes while a recording to the same path is still starting.

The check also knows only the calling session, so another session can take this
session's destination. In all of these, the failure is the R-REC-18 path:
`record-stop` keeps the clip at another path, names it, and ends the session.

The private directory is removed after publishing and after a failed or cancelled
start. A finalized clip is published even when rendering failed (R-REC-10). A clip
the recorder did not finalize stays private, so an incomplete file never takes
the requested path. If publishing fails, usually because a file already holds the
destination, the clip also stays private, and the error includes any render
failure (R-REC-10, R-REC-18). Both take the
ordinary finalization failure path, which ends the session. `Recordings.close`
adds the kept clip's path to any finalization error.

Before starting the recorder, framewisp creates a file with the destination's
name inside the private directory and hard-links it, so an unusable name or a
filesystem without hard links fails at clip start rather than after the capture
(R-REC-19). There is no copy fallback, because a copy would make a partly written
destination visible.

During a clip, the `.active-recording` symlink in the session directory points at
its private clip. The link goes when the clip is published or discarded. A kept
clip stays linked, because a later teardown failure can replace the error that
named it, until `recover` or the next clip clears the link. A symlink is created
atomically, so `recover` never reads a partial value. `recover` reports that path as
`interrupted_recording` and removes the link. The directory and clip stay for the
user to inspect or delete (R-REC-20). If no clip was written yet, `recover`
removes the empty directory. A refusal runs none of this: it removes nothing, names
any kept clip, and its manual steps leave the link for a later `recover` to report
(R-LIFE-7, R-REC-20). Each private directory has a unique name, so a
leftover never affects a later recording, even to the same destination.

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
any active recorder before stopping the display (R-REC-3).

The runner listens on `control.sock` in its private runtime directory. Each
control CLI call sends one newline-terminated JSON request with `action`, the
absolute `session` directory, a `destination` (absolute recording path or null),
a `captions` mode, `click_pings` and `compact` booleans, and an optional `console` object. Replies carry an `error` string or null
and optional `data`. The runner checks the session directory against its own
before acting, so copied metadata cannot control another session (R-LIFE-6). Its
monitoring loop handles recording/status/stop requests; validated input sockets
transfer to the worker. It replies only after capture is ready or finalization
has finished. Metadata includes `control_protocol: 1`. The CLI rejects an absent
or unknown version before connecting. Input also requires `persistent_input:
true`; a legacy recording-only runner would otherwise interpret new status or
stop requests as recording-stop requests. Validation/startup errors are returned
to the caller without stopping the app. Unexpected recorder exit or finalization
failure still fails the session, after `record-stop` replies with the error.

The runner and an attachment handle each request in two stages, and the stage
decides what a failure means. `receive_request` in `connection.py` reads the line
and has the owner's parser turn it into a typed request. Neither changes session
state, so every failure there gets an error reply and the session continues
(R-CONTROL-1, R-CONTROL-2). That holds for any command, and for a request the
owner cannot read or decode. A client has `REQUEST_SECONDS` in total to send its
line, however slowly it sends, so one connection holds the runner's control loop
for no longer than that. A failure to log a validator bug does not end the session either. Resolving a
requested path refuses a NUL byte and
any lone surrogate that the filesystem encoding cannot represent. The surrogates
that stand for undecodable filename bytes stay valid, so a non-UTF-8 destination
records normally. `REJECTED_REQUEST` lists what validators raise for a bad request, and
the reply carries that message. Any other exception there is a validator bug: the
reply names its type, and the traceback goes to the owner's stderr without the
message, which can quote typed text. In the runner, an error during execution that
is not returned as a reply is a runner failure and ends the session, even if it is
a `ValueError`. An attachment's execution keeps its own rule: a `ValueError` or
cancellation is that command's reply, and anything else closes the portal. So a
screenshot opens its destination before capture starts and accepts only a regular
file. Failing to open it, or to write it, is that command's error.

`record-stop` returns a summary in `data`, printed as JSON by the CLI. After
finalization and caption rendering, ffprobe supplies duration and dimensions;
the file's stat supplies byte size. The summary also includes the absolute path.
The runner retains the last completed summary until another clip completes.

`status` queries the owner, rather than interpreting stale PIDs in metadata. It
returns JSON describing the backend, display dimensions, application process
state, and active recording path/process state. `stop` retains the request
connection and sets the runner's stop event. The runner responds only after its
resource stack, runtime directory, and metadata have been cleaned up, including
any active recording's finalization. Cleanup errors produce a failure response.
No stored PID is signaled. Stop requires a responsive owner; it does not recover
from a suspended or dead runner. These commands reject attached sessions, which
retain their independent emergency detach mechanism.

State updates use `.session.json` followed by an atomic rename. The `processes`
map includes `recorder` only while recording. Both metadata filenames are
reserved along with the session logs when choosing recording destinations.
Recording requests reject odd display dimensions (R-REC-2), existing/reserved
output paths (R-REC-1), a start while already active, and a stop while inactive.
Each start replaces `recorder.log`.

## Input logs and captions

The CLI validates input arguments. For headless sessions, the worker appends a
start event to `inputs.jsonl` when the action starts executing and an end event
after cleanup, so queue time does not extend captions. Cancelled requests that
never start do not produce captions. Attached sessions retain CLI-owned logging. Each JSONL event carries an ID,
`event`, monotonic `time` in seconds, `action`, `parameters`, `returncode`, and
`error`. A `ping` event also carries the display position `x` and `y`. Session
metadata stores `retain_input_content`, default false. The
`run/attach --retain-input-content` option sets it once for the session. The headless
worker owns the policy for individual and batch actions; attached command clients
read it from metadata. Recording captions use the retained parameters from the log,
so all clips and caption modes follow the same policy. No command or batch
parameter overrides it.

Captions, pings, and compaction read `inputs.jsonl`, and compaction also reads
`activity.jsonl`, all through `activity_events` in `compact.py`. A record is
complete once its newline is written, so the reader skips only the bytes after
the last newline, which an interrupted write leaves behind (R-LOG-7). Every line
before that must parse, so a malformed one fails rendering (R-LOG-8, R-REC-10)
whenever the clip needs one of those readers. The reader splits bytes on `\n`
alone: retained text can hold characters such as U+2028 that text-mode line
splitting would break on.

Before each input event append, the writer opens `inputs.jsonl` in binary
append/read mode and takes an exclusive flock on that descriptor. It checks the
last byte and, if no newline ends the file, scans backward in bounded chunks to
truncate only the suffix after the last newline (or the whole file if none
exists). Valid JSON without a newline is also uncommitted and discarded.
Complete records, including malformed JSON, stay unchanged (R-LOG-7, R-LOG-8).
The descriptor stays locked through the buffered append and close, which flushes
before releasing the lock. This serializes repair and append across attached
CLI clients without a separate lock file; headless inputs already share one
worker. Readers hold a shared flock on their reading descriptor through the
whole byte snapshot, so repair cannot splice old tail bytes into a new record.
They release the lock before parsing and still ignore an unfinished trailing line.

By default, type parameters omit `text` while retaining `interval` (R-LOG-1).
Literal key chords (letters, digits, punctuation, Space, optionally Shift) omit
the key and retain only modifiers. Ctrl/Alt shortcuts and named
navigation/control keys retain their chord. Captions use "Type text" and "Key"
for omitted content, with any Shift modifier as a `Shift+` prefix. Opt-in preserves full text and chords. Text/key failures
omit backend messages that can include literal or encoded input, while
preserving exception types, cancellation, and exit codes. Unicode subprocess
diagnostics omit arguments and stderr by default. Tracebacks retain stack
locations without content-bearing exception messages.

Screenshots and recordings can still show secrets displayed by apps. This policy
does not redact screen content or guarantee app-log redaction. Shell history, batch
files, and runtime input state (including X11 key mappings) are outside its scope.

Exceptions are logged and re-raised; the CLI formats expected operational
failures, while unexpected exceptions retain their traceback. Normal
nonzero returns are recorded as failures. Input logging is independent of
recording. The runner truncates the log when creating a new session.

For recordings with a caption mode other than `none`, click pings, a console
panel, or compaction, only wf-recorder receives `WAYLAND_DEBUG=client`.
The first screencopy `ready` event in `recorder.log` supplies the first captured
frame's timestamp. The pinned Sway backend uses the monotonic clock, and the
pinned wf-recorder makes this frame time zero. This establishes each clip's
origin without guessing from subprocess startup or MP4-header detection.
A pipe reader filters complete protocol-trace lines as the recorder emits them.
It flushes the first screencopy `ready` line and preserves non-protocol diagnostics
in `recorder.log`. A worker thread drains the pipe throughout capture and is joined
after recorder shutdown. Startup waits for both the MP4 header and the retained
origin before accepting captioned input. The log is replaced for each clip.

`--captions` selects input kinds for one clip, and the session's retention policy
decides what each caption shows. `keys`, the default, selects `key` and `type`
(R-LOG-4). `pointer` selects `move`, `click`, `drag`, and `scroll`. `all` selects
both, and `none` selects nothing. `run` requires `--record` with `--captions`.
One table in `captions.py` maps each mode to its actions. The input log is written before and without reference to the mode
(R-LOG-5), and compaction reads that unfiltered log, so an uncaptioned input
still counts as activity. The runner rejects a `captions` request value that is
not a mode. Session metadata advertises `caption_modes: true`, and `record-start`
requires it before connecting, so an older runner reports a restart instead of a
confusing validation error.

After recorder finalization, `captions.py` selects commands of the clip's mode
started between that origin and the stop request. Unselected commands are
ignored before timing is computed. Commands that began before the clip are
excluded, even if they finish during it. Each caption spans its command, or at
least 0.8 seconds for quick commands, capped at the next captioned command or
clip stop. An unfinished
command spans to clip stop. Captions show requested actions; they do not claim
application acknowledgement. The video keeps its original idle and input timing.

FFmpeg/libass renders an ASS script into a temporary H.264 MP4 beside the
private clip. Caption rendering uses the same Fontconfig file as apps (R-REC-23),
passed only to FFmpeg, including Noto Sans, CJK, and monochrome emoji. On
success, the rendered file replaces the raw recording; on failure, the raw MP4
stays private until publishing, and the command fails with the `captions.log` path. A single wrapper
around `render_recording` turns every rendering failure, including malformed logs
and an unwritable `captions.log`, into one message naming the raw video, that log,
and `inputs.jsonl` (R-REC-10). The rendered video
preserves GUI dimensions, exactly 30 fps, full-range color, and no audio. `record-stop`
waits for rendering, as does normal session cleanup. With `--captions none` and
`--no-click-pings`, timestamp extraction is skipped unless a console panel or
compaction is requested. A clip with no captions or pings to show and neither of
those keeps the recorder's MP4 without a rendering pass, so its frame rate is only
approximately 30 fps and it has no faststart index. Input logging remains enabled. Caption formatting cannot affect
app screenshots (R-LOG-2). Both `inputs.jsonl` and `captions.log` are reserved
session paths.

## Click pings in recordings

Clips show click pings and drag hold discs by default (R-REC-5).
`--no-click-pings` turns both off for one clip (R-REC-7), and caption modes leave
them on (R-REC-11). Command start and
end times can't place a ping: a press follows the pointer approach, and double
clicks and drags press more than once. So the input worker calls `ping` from
`log_input` right after each click's `mouseDown`, and after a drag's `mouseDown`
and final `mouseUp` return, appending a `ping` event with the same ID as its
command. The position is `pointer_position`, the clamped coordinates actually sent.
Scroll presses, refused presses, and the cleanup release of a cancelled action
log no ping. `ping` events carry no typed content, and readers of start and end
events ignore them.

`pings.py` draws a ring for each press in `origin <= time < stopped`, and a hold
disc for each completed drag pressed before `stopped` and released at or after
`origin`. It writes one ASS drawing per ring or disc into the script that holds captions and console text, so all
overlays share one FFmpeg pass and the existing filter order aligns them: `ass`
draws on the original timeline, and the compact `select` cuts afterwards. No
mode needs its own handling.
The ring grows from radius 10 to 40 pixels with a 4-pixel stroke while fading over
0.5 seconds, scaled by display height like captions. Its path uses only positive
coordinates, because libass otherwise shifts a scaled drawing off its `\pos`
(R-REC-6). The ring's `\pos` adds the GUI offset from the clip's `VideoLayout`, and
a `\clip` to the GUI rectangle keeps it off the console panel on every edge (R-REC-9).

A drag's press ping draws the same ring. `pings.py` pairs it with the drag's
release ping by command ID and adds one hold disc: the same circle path with no
stroke, red fill at 30% opacity, radius 24 pixels. The input worker moves the
pointer in equal steps of equal time between those pings, so the disc's
`\move` from the press position to the release position over the logged hold
time follows the pointer with no extra log data (R-REC-6). The disc grows from
radius 10 at the ring's rate, filling the ring until it reaches its own radius,
and fades over 0.5 seconds from the release. Its `\move` and `\clip` use the
same GUI offset and rectangle as the ring (R-REC-9). libass moves over the whole
line, fade included, when both `\move` times are zero, so the hold time is at
least 1 ms. Discs precede rings in the script so rings draw on top. A drag
pressed before the clip's origin and released after it starts its disc at the
clip's first frame, fully grown, at the position interpolated for the origin
(R-REC-5). A cancelled drag logs no release, so it keeps only its press ring.
Screenshots read the compositor, which never sees this overlay (R-LOG-3).

The control request's `click_pings` boolean defaults to true. Session metadata
advertises `click_pings_recording: true`. A `record-start` that wants pings
requires it before connecting, so an older runner cannot silently omit them.

## Console output in recordings

Every headless session captures combined app stdout/stderr through a worker-drained
pipe. `console.py` flushes original bytes to `app.log` and writes JSONL events with
monotonic receipt `time` and incremental UTF-8 decoded `text` to `console.jsonl`.
Invalid UTF-8 uses replacement characters only in the decoded sidecar. Capture
does not wait for line endings, alter the app environment, or use a pseudo-terminal.
App buffering can delay receipt. Descendants inheriting these descriptors share
capture; independently activated services do not. No input-content redaction
applies to app output. Both files are replaced on each new session.

`run --record FILE --console` and `record-start --console FILE` opt individual clips
into a console panel. `--console-position` picks the video edge, default `right`
(R-REC-12). `--console-size` sets the panel's width for `left` and `right` and its
height for `top` and `bottom`, default 640 pixels (R-REC-13). The other panel
dimension equals the matching GUI dimension. `run --console` without `--record` is
invalid. The parser rejects any `--console-*` option without `--console`, on both
commands, before it starts a session or clip. The control request's `console`
value is absent, null, or an object with exactly `position`, `size` and
`cell_scale`, so a position can't exist without a panel.
Console capture uses the same first-frame monotonic origin as input captions,
including with `--captions none`. A lock protects sidecar snapshots during rendering.
Session metadata advertises `console_cell_scale: true`. Every `--console` request,
including the default placement, requires this capability before connecting, because
an older runner can't read the object. The error tells the user to restart the
session. A clip without a panel omits `console`, because an older runner also rejects
null, so it still works there.
The app-output worker is checked during monitoring, joined after app cleanup and
before final recording rendering, and bounded on shutdown if a pipe writer remains.

The renderer streams the snapshot into bounded text state, strips split ANSI CSI
and OSC sequences, replaces lines after carriage returns, and handles backspaces,
tabs, wide characters, wrapping, and a limited scrollback. The panel's width sets
the wrap column and its height sets the rows kept: `(height - 64) // 18` rows and
`(width - 32) * 100 // 931 - 10` columns, which is 55 columns in a 640-pixel panel.
Both divisors are multiplied by `--console-cell-scale` (R-REC-25), computed
exactly from the decimal given. The fixed font (R-REC-23) lets these use measured
metrics: libass draws DejaVu Sans Mono 18 with 18-pixel lines and 9.30-pixel
cells, rounded up to 9.31. Text starts
16 pixels in from the left and 48 down, below the title, and keeps 16 pixels of
padding at the right and bottom (R-REC-24). Each line starts with a 7-character
timestamp and ` | `, so 10 cells are reserved; times from 1000 seconds before the
clip or 10000 after drop their decimals, and clamp to -999999 and 9999999, to keep
that width. Captions and console text escape braces as `\{` and `\}`, and follow
each backslash with a word joiner so it can't start `\N`, `\n` or `\h`; all three
then draw as themselves, one cell wide. Spaces render as ASS hard spaces, because
libass drops a line's leading
spaces and would shift the `|` after a shorter timestamp. Printable ASCII stays
inside the panel (R-REC-24), and CJK and emoji fallbacks happen to fit their two
cells. Other fallback fonts are not measured: Thai, Devanagari and Vietnamese lines
are 19 to 20 pixels tall, and Tamil letters about 1.3 cells wide (ஔ 3.2), so a
full panel of them needs a larger cell scale.
Line timestamps are seconds relative to clip origin; negative values identify
pre-clip context. Output received at or after the stop request is excluded. Updates
coalesce to at most one per 30 fps frame. This is plain text rendering, without
terminal emulation.

`video_layout` turns the display size and panel request into a `VideoLayout`: video
size, GUI rectangle, and panel rectangle. `layout.py` is the only module that branches
on the position: `video_layout` for the rectangles and `ConsolePanel` for each edge's
minimum size. `pad`, the ASS play resolution, click pings, captions, and console text
all read those rectangles, and a clip without a panel has a GUI rectangle that fills the
video. Caption and ping scale follows GUI height, not video height, so a top or
bottom panel doesn't resize them. Caption margins come from the distance between the
GUI and each video edge, and a `\clip` to the GUI rectangle stops a caption wider
than a narrow GUI from reaching the panel (R-REC-15).

A panel must be encodable and readable (R-REC-16):

- `--console-size` is even. Yuv420p needs an even video, and `pad` rounds an odd
  offset down, which would shift the GUI off its overlays.
- The panel has at least 20 text columns and 3 text rows, so at least 312 pixels wide
  and 118 tall at the default cell scale. Both minimums grow with the scale, and
  errors name it. This includes the dimension that follows the GUI, so a default
  panel on a display under 118 pixels tall is rejected, naming the display size.
- `--console-cell-scale` is a number from 1 to 58. Above about 58.5 even the
  narrowest panel with 20 columns is wider than 16384 pixels.
- Neither video dimension exceeds 16384 pixels, the libx264 limit, and
  `(width + 128) * (height + 128)` stays below 2^28, FFmpeg's frame size limit.
  Measured with the pinned FFmpeg, 16384x16128 and 16258x16254 encode, while
  16384x16130 and 16256x16256 fail, so pixel area alone doesn't describe the limit.
  Without these checks the failure would appear only after capture. `ConsolePanel` also rejects a
  larger size by itself, without formatting it, because Python can't format an
  integer of more than 4300 digits.

`ConsolePanel` checks what its own values decide, and `video_layout` checks what also
depends on the display. `run` knows its display size, so the parser runs both before
a session starts. For `record-start`, the parser checks the panel and the runner
calls `video_layout` before it starts the recorder, so a rejected panel leaves the app
running and creates no file (R-REC-17). The runner also validates the request's
values, because the control socket is an input of its own.

One FFmpeg pass pads the original video, renders console text with DejaVu Sans
Mono 18 and existing input captions in the GUI area, and preserves video timing,
30 fps, full-range color, and silent H.264 MP4 output. GUI pixels, coordinates,
screenshots, and captured display size stay unchanged (R-REC-14); the finished
summary reports the extended video. Text cannot inject ASS formatting and is clipped
to the panel. Rendering uses a temporary file next to the destination. A rendering
failure preserves the raw GUI MP4 and reports its path and `captions.log`. `console.jsonl` is
reserved against capture and recording destinations.


## Attached desktop sessions

`attach.py` owns a foreground connection to the user's existing desktop. It does
not launch or own that desktop's app or compositor. The CLI requires a
controlling terminal with this process group in the foreground, prints
stop-binding and access instructions (R-ATTACH-5), and requires the exact typed confirmation
ATTACH before creating any portal connection (R-ATTACH-1). Agents receiving a
rejection must ask the user to run the command in another terminal. There is no
noninteractive override. This prevents accidental startup, not deliberate bypass
by another program under the same UID.

`portal.py` uses a private Gio connection to the user's session bus to create a
RemoteDesktop session, select keyboard and pointer devices, select one monitor
through ScreenCast, and request consent with Start. Every attachment requests fresh
permission. Closing the private bus connection revokes all its portal sessions;
process death also closes that connection. No access-owning child processes exist;
the X11 release guard below holds no portal or capture access.
SIGINT, SIGTERM, SIGHUP, and SIGTSTP request shutdown. Loss of terminal foreground
ownership or the portal's Closed signal also stops access.

`tray.py` exports org.kde.StatusNotifierItem at /StatusNotifierItem and a static
com.canonical.dbusmenu at /Menu on the owner's private portal bus connection,
after portal approval and input setup. It supplies a 32x32 ARGB monitor icon,
active-sharing title and tooltip (R-ATTACH-3), and one enabled "Stop sharing"
menu item. The menu sets the same stop Event used by signals and portal revocation
(R-ATTACH-4). Pending
input cancels through the existing worker cleanup; no app process is stopped.
Shutdown unregisters the objects before closing the portal connection. Forced
owner death closes the connection and removes both sharing access and the item.
There is no tray helper process or new runtime dependency. Gio is already packaged
in the installed Nix wrapper and development environment.

The indicator watches org.kde.StatusNotifierWatcher and registers its object path
only when IsStatusNotifierHostRegistered is true. Watcher appearance, disappearance,
and property/signal changes update the diagnostic; watcher restarts register the
item again. Proxy setup and registration use asynchronous calls so an unresponsive
tray watcher cannot block input cancellation or terminal checks. Registration
errors and missing hosts report the terminal/shortcut stop methods without ending
sharing. Successful registration reports no promise of visible placement.
KDE Plasma, COSMIC's status area, and GNOME with an enabled compatible
StatusNotifierItem/AppIndicator extension are the supported host configurations.
The host owns placement, overflow, auto-hide, and visibility (R-ATTACH-6). The independent
emergency detach command remains necessary for suspended or unresponsive owners.
Desktop verification status and required setup live in docs/desktop-sharing-tray.md.

An exclusive flock on $XDG_RUNTIME_DIR/framewisp/desktop.lock limits desktop access
to one owner per user runtime directory, including while permission is pending.
Attach refuses a directory that is not private and owned by the user
(R-ATTACH-2). The lock inode is never
removed. After acquiring the lock, a new owner removes stale socket and global
metadata files. Atomic JSON writes publish global desktop.json and session.json;
the latter records kind, the runtime directory, an attachment token, and the PID
for inspection. Commands carry the token, so stale session metadata cannot direct
input to a later attachment using the same global socket.

`connection.py` sends newline-delimited JSON requests over control.sock and shares
the request stage with the runner (see Recording controls). A listener
thread accepts requests; workers serialize actions with a lock. Before portal
consent they reject input. Paced actions and queued workers check a shared stop
Event. Unexpected worker errors stop access and print a traceback. Shutdown closes
the portal before bounded waits for workers, then removes state under the ownership
lock. Held buttons and keys release during action cleanup. On Wayland, connection
loss makes the compositor remove the virtual input devices and their held inputs.

Mutter's X11 backend sends portal input as XTEST events on its own X connection,
which stays open, so closing a session leaves held keys and buttons down. XTEST
state belongs to the device, so any X client can release it. On X11 desktops,
detected by `XDG_SESSION_TYPE` when it is `x11` or `wayland` and otherwise by
a missing `WAYLAND_DISPLAY`, attach
starts `release_guard.py`'s bash helper after taking the desktop lock and before
opening the portal. It runs in a new session, so terminal signals and hangup miss
it. It runs one `xdotool keyup` of Escape and keycodes 10 to 255 plus `mouseup` of
buttons 1 to 3, then blocks reading the owner's pipe. Attach fails unless that
first release succeeds within five seconds, and refuses X11 without `DISPLAY`.
Any owner exit, including SIGKILL, closes the pipe. The helper then releases,
waits 500 ms for presses the owner sent just before dying, releases again, and
exits. Normal shutdown closes the pipe after the portal and does not wait, so
`framewisp --detach` sees the owner exit within its SIGKILL deadline. If the
helper exits early, the owner stops access and starts the same final release
itself in a new session. Xorg drops releases for keys and buttons the XTEST device does not
hold, and XTEST releases never change physical devices' state. The helper can
release keys another XTEST client holds. The attach prompt states that, on X11,
other programs running as the user can capture and inject without the portal.
The test suite runs the release against Xvfb while another client holds keys
and buttons, as mutter does.

`framewisp --detach` connects to a separate detach.sock, checks its peer UID, and
uses Linux SO_PEERPIDFD (6.5+) to obtain a stable handle to the socket owner. It
sends SIGTERM and waits up to three seconds, then SIGKILL if needed, and reports success
only after process exit. This avoids PID reuse and works when the owner is stopped
or its command workers are blocked. The emergency socket never queues behind
ordinary commands. Stale files after SIGKILL do not hold the kernel lock. Detach
never signals the user's apps or compositor.

Pointer coordinates use screenshot pixels and the portal stream node. Portal
Notify methods deliver absolute motion, buttons, wheel steps, and keyboard
keycodes for held keys and shortcuts, with keysyms for literal text. Keycode
names currently assume the tested desktop's US layout. Each screenshot opens a PipeWire remote file descriptor through the
portal and passes it to an in-process GStreamer pipewiresrc pipeline. Mutter can
deliver a frame left from an earlier connection before current ones and sets no
timestamps, so an appsink limited to raw system-memory video keeps only the newest
buffer and is read again 100 ms
after the first frame arrives (R-ATTACH-7). A deep copy of that frame goes through
a second appsrc, videoconvert, and pngenc pipeline. Cancellation tears down the
pipelines; process death closes every capture descriptor. Ten-second limits for the
first frame and for encoding report capture failures with capture.log. Nix supplies GStreamer and the
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

## Teaching demonstrations

`teach DIRECTORY --goal TEXT` uses the attached-session owner, terminal consent,
desktop lease, sharing indicator, and emergency detach. It creates a ScreenCast
session rather than a RemoteDesktop session; it never initializes input devices
or an X11 release guard. Its request handler accepts screenshots and `teach-note`
only (R-TEACH-1). Teaching uses the same foreground-terminal and exact `ATTACH`
confirmation as attachment. Normal `attach` and its recording restrictions are
unchanged.

`teaching.py` creates a new private output directory with an atomic mkdir
(R-TEACH-2). After portal approval it opens one PipeWire connection, owned by the
attach process. GStreamer's live `imagefreeze` repeats the latest capture at
15 fps, including unchanged screens; an upstream EOS probe still detects lost
capture. A tee writes a silent VP8 WebM with an embedded cursor and samples PNGs
at one per second. Both use the clocked stream's presentation timestamps.
The main loop reads at most one observation per iteration from a bounded appsink
and writes images and timestamped entries to `timeline.jsonl`. Teaching announces
readiness only after saving its first observation. These images are observations, not a complete input trace;
fast interactions may only appear in the video. No physical keystrokes,
clipboard, accessibility data, or audio are captured.

The bundle's versioned `lesson.json` names `demonstration.webm`,
`timeline.jsonl`, and the goal. Status starts as `recording`, becomes `complete`
only after at least one video frame and successful EOS finalization, and becomes
`failed` on capture or output failure (R-TEACH-3, R-TEACH-4). Abrupt process death
can leave `recording`; consumers must treat that as interrupted. WebM uses
streamable output to retain decodable clusters without a final seek. Notes use
pipeline running time in seconds; frame entries use buffer running time.
`teach-note TEXT` appends under the attachment's existing request lock.

Shutdown sends EOS directly to `imagefreeze` (which otherwise holds its last
image after input EOS), stops capture before closing the portal, and bounds
encoder finalization.
The desktop detach command allows three seconds for graceful shutdown before
killing an unresponsive owner, including the encoder's one-second EOS budget.
The main loop only takes the request lock when immediately available, so a slow
screenshot cannot block desktop stop events or foreground checks. A desktop
portal revocation ends teaching normally; an independent source failure still
marks the evidence incomplete. Destination parent validation runs before consent.
Already saved files remain on failure. A rejected destination does not remove
anything. Images, notes, and video are intentionally unredacted, and the consent
text makes this explicit. The bundled agent guide explains how to study the
timeline, inspect additional video frames, clarify uncertainty, derive a procedure
with variable inputs and outcome checks, and validate it in a fresh example.
Framewisp does not call a model or generate a procedure itself.

## Structured UI inspection

`inspect --json` is a read-only AT-SPI prototype for headless sessions. Every
runner starts two private `dbus-daemon --nofork` processes and an
`at-spi2-registryd` before the display/app. Default bus configuration has no
service activation directories. The runner replaces inherited session/accessibility bus
addresses and sets `AT_SPI_BUS_ADDRESS`, `GTK_A11Y=atspi`, and
`QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1` for its children. It monitors all three
processes and closes them after the application/display during ordinary cleanup.
Their sockets live in the private runtime directory, and their logs are reserved
session files. This separates sessions from each other and the user's desktop;
it is not a security boundary against other processes with the same UID.

`session_environment_for_run` also defaults `ACCESSIBILITY_ENABLED` to `1`
with `setdefault`, enabling Chromium's AT-SPI bridge on headless hosts while
preserving caller values such as `0`. Both runtime and app environments receive
this default, and sanitizing preserves it. An `env ACCESSIBILITY_ENABLED=0`
prefix on the target command can override it for that app. Chromium launch
guidance retains `--force-renderer-accessibility`; the variable alone has not
been verified without that flag.

Metadata carries `inspection_protocol: 1` and the private accessibility address.
The CLI requires that address to equal the runtime directory's accessibility
socket. It never uses the caller's desktop bus (R-INSPECT-1), autolaunches a
bus, or inspects attached sessions. Gio, already a dependency, connects directly
to this address and calls read-only AT-SPI methods. No additional Python package
is needed.

Neither private bus activates services. An app that needs activation runs under
`dbus-run-session --config-file=...`, which keeps activation out of framewisp:
the nested bus starts after `env` on the app command, so services inherit injected
variables, and it stays in the app's process tree for shutdown. Accessibility is
unaffected because apps reach it through `AT_SPI_BUS_ADDRESS`.

The CLI owns each query connection independently of the input/control socket.
A Gio cancellable with a deadline timer bounds connection setup and requests;
each synchronous call also receives the remaining request budget. An unresponsive
application produces a timeout observation without blocking runner status/stop
(R-INSPECT-6).
There is no persistent AT-SPI object cache or background observer.

`inspection_limits.py` defines the shared query defaults and maxima. CLI help,
inspection validation and hints, and batch checks use those constants. The module
has no GUI imports, so batch validation can run without loading Gio. Inspection
and batch observations have separate default timeouts.

Traversal is breadth-first, starting with application objects at depth 1. It
fetches individual child references rather than an unbounded tree. Defaults are
8 levels, 20 returned matches, 256 visited nodes, and 5 seconds. Hard CLI maxima
are 64 levels, 100 matches, 4096 nodes, and 10 seconds. Role filtering compares
case-insensitive exact toolkit role names; name/text filtering uses
case-insensitive substrings. Filters combine with AND. Strings and text reads
are capped at 1024 characters, and actions at 16 per match. Truncation anywhere
that could affect the observation makes it partial, including when no match is
found. The private buses reject messages over 1 MiB.

Schema version 1 returns `snapshot_id`, `status`, `matches`, `match_count`,
`visited`, `applications`, `reasons`, `errors`, `hints`, `longest_call_ms`, and
`elapsed_ms`. Each match has
`id`, `role`, `name`, `text`, `states`, `actions`, `bounds`, `display_bounds`,
`display_bounds_reason`, `value`, and `depth`.
Unavailable optional interfaces produce null text/value/bounds or empty actions.
Errors while reading advertised interfaces make the whole observation partial;
already read fields remain available. State names reproduce AT-SPI flags, not a
normalized toolkit-independent enabled/disabled assertion. GTK's tested backend
reports `sensitive` without `enabled` even for usable controls.

Only `ok` exits zero. An `ok` empty list means no match in the fully traversed
exposed tree. `unsupported` means no accessible application has registered yet;
it can also occur while an app is starting. `partial` reports limits or vanished
objects, `timeout` reports an exhausted budget, and `unavailable` reports
failure to reach the bus/registry. Non-ok observations still print JSON and
exit 1. `longest_call_ms` measures the slowest synchronous D-Bus method call,
including failed calls but excluding connection setup. A failed call to an app
that spends at least 80% of the entire query budget and reaches the deadline
adds the `app-unresponsive` reason (R-INSPECT-5). This is evidence of a slow or
blocked app, not proof of a hung process. Absence of this reason does not prove
responsiveness: an app can block late in traversal. Cancellation before the
deadline does not add it. Up to five bounded error strings include the failed
call's bus name, object path, interface, method, and duration. `hints` names
each limiting flag and its maximum; for an unresponsive app it recommends
waiting or a screenshot instead of raising the timeout. Filters reduce matches
and optional reads, but do not prune traversal, so they do not remove depth/node
limits.

Invalid command arguments exit 2. Toolkit omissions cannot be detected from a
successful traversal, so `ok` does not certify that every visual widget exists
in the accessibility tree.

IDs are unique only within that snapshot. They cannot be passed back as selectors
or reused across queries. Duplicate matches are returned separately; ordering is
not a selection rule. Every query reads fresh state, but the reads are not atomic:
the app can change between objects or fields, or after the response. Failed or
defunct references are reported as partial (R-INSPECT-4).
`bounds`, when valid, uses AT-SPI's window-relative coordinate system in toolkit
units. `display_bounds` uses the same rectangle shape with `coordinate_space:
"display"`; its center can be passed directly to pointer commands
(R-INSPECT-2). Failed
conversion leaves `display_bounds` null and sets `display_bounds_reason`. This
optional conversion does not make otherwise complete observations partial.

`display_bounds.py` reads GET_TREE directly from the sole Sway IPC socket in the
session's private runtime directory, not the caller's SWAYSOCK (R-INSPECT-1).
Each tree read shares the query deadline, takes at most 250 ms, polls
cancellation, and caps replies at 1 MiB. The before/after tree reads and D-Bus
traversal share the query timeout. Traversal carries accessible
frame/dialog/window references and their full titles, nearest first. D-Bus
GetConnectionUnixProcessID identifies the accessible process. Conversion
requires PID and exact window title to identify one Sway window (R-INSPECT-3);
duplicate titles remain ambiguous. An empty accessible title can match only if
the PID has exactly one Sway window, since GTK may supply a compositor title for
an untitled window. If a dialog has no compositor match, conversion tries its
enclosing accessible windows. Ambiguity or a failed geometry check on a matching
window never triggers fallback. Enclosing window identities have to agree with
the selected compositor window; conflicting matches produce `ambiguous-window`,
even if their sizes agree. Window selection uses titles before fetching
geometry, so an unused enclosing window's Component interface cannot suppress a
valid nearer match. PID and component geometry reads are cached only within the
query.

For matched controls, conversion reads the component bounds of their traversed
ancestors up to the nearest accessible window, including ancestors excluded by
filters. Parents without a Component interface or with empty extents are skipped.
If a rectangle extends outside a parent's reported rectangle, the control and
its descendants retain raw `bounds` but receive null `display_bounds` and
`display_bounds_reason: "bounds-outside-parent"`. This can indicate faulty
toolkit extents or intentional overflow; framewisp does not infer corrected
coordinates. Failed ancestor metadata reads leave conversion unavailable without
making an otherwise complete observation partial. Consistent rectangles do not
prove that the toolkit reported the drawn position correctly.

The client origin is Sway's absolute `rect` origin plus its relative
`window_rect` origin, so server-side title bars and borders are excluded. A
conversion requires a visible window on an unscaled, unrotated output at the
origin. Conversion also requires the accessible toplevel to start at window (0,
0), its size to match Sway's client size, and the control to fit inside that
client area and the display (R-INSPECT-3). Missing windows, ambiguous mappings,
unavailable metadata, geometry mismatches, or unsupported transforms produce
reasons instead of guesses. The matching window has to keep the same ID, PID,
title, visibility, client rectangle, and supported transform in both tree
snapshots, and window selection has to choose the same accessible ancestor in
each snapshot (R-INSPECT-3). A changed mapping produces `window-changed`. This
detects changes between snapshots, not a move away and back between reads or
changes after inspection. Use a screenshot when conversion is unavailable. No
semantic input action is provided.

The tested toolkit matrix, timings, reproducible benchmark, and decision about
conditional waits are in [docs/ui-inspection.md](docs/ui-inspection.md).
`tests/test_real_apps.py` adds opt-in host coverage for installed Writer GTK 3
and Blender Flatpaks on private Wayland and Xwayland. It verifies edited Writer
paragraph text through AT-SPI and Blender geometry through its GUI console,
records screenshots and backend evidence, and checks normal-stop cleanup.
These app/version-specific tests do not run in the standalone package check.


## Conditional checks

`checks.py` defines conditions and `Check` steps alongside `InputAction` in a
batch. Parsing validates all selectors, typed expected values, positive bounded
deadlines, and backward baseline references before any input. The worker executes
checks in sequence and retains the last observation in each result. Each query
resolves the selector afresh; no accessible object handle survives an observation.
Only a complete observation with exactly one readable match can verify state
(R-CHECK-1). A transition wait cannot run until its baseline has established a
nonmatching value (R-CHECK-2).
Replacement widgets are allowed under the same logical selector.

Each step's optional `observation` object controls `max_nodes` (default 256,
maximum 4096), `max_depth` (default 8, maximum 64), and per-query `timeout`
(default 2 seconds, maximum 10). Validation rejects counts that are not positive
integers and times that are not finite and positive (R-INPUT-3). The two-match
ambiguity limit remains fixed. Each query receives the smaller of its configured
time budget and the remaining monotonic step deadline. Only step deadlines count
toward the shared batch budget. Baseline references compare conditions, allowing
different observation budgets. Results include normalized
`observation_settings`. Errors identify supported JSON overrides and retain
traversal caps seen before a final short query times out; a subsequent complete
observation clears those earlier cap diagnostics.

`inspection.Bus` accepts a cancellation callback. A watcher checks it every 20 ms
and cancels the Gio cancellable, interrupting connection setup and in-flight calls.
The existing deadline timer independently bounds each observation. Both stop on
connection cleanup. Polls open private connections and close them after traversal;
there is no observer cache or notification subscription. The runner's control loop
remains responsive while its serialized input worker polls.

The complete schema, state/transition semantics, result fields, limits, and
failure-capture rules are in [conditional checks](docs/conditional-checks.md).
That document is part of this design specification. Input completion and successful
PNG capture alone never set batch `verified` to true (R-CHECK-3).

## Continuous integration

GitHub Actions runs formatting and development-shell quality checks alongside
two library test shards and twelve installed-package test shards on standard Ubuntu
runners. Each library shard builds the package and runs its assigned unit cases
serially. The `library` check depends on both library shards. Each package shard
builds the package without its test phase, then runs its assigned GUI cases
serially. Default package builds still run the full unit suite. `nix flake check`
runs the aggregate and all fourteen shards. Individual checks are available as
`checks.x86_64-linux.library`, `library-1` through `library-2`, and `package-1`
through `package-12`.

`nix/package-check.nix` runs installed-package tests in an empty environment with
explicit GTK, Qt, recording, and Python dependencies. `tests/conftest.py` assigns
collected cases by their collection index modulo the shard count. Every runner
must collect the same cases in the same order. The default shard count of one
selects every case for ordinary pytest invocations. Both CI matrices use this
collector, with their own shard counts.

Library and installed-package checks print the slowest test durations in their logs.
Successful package checks publish JUnit reports. Failed installed-package checks
retain their Nix build directories so CI can upload test logs, recordings, and
JUnit reports.
The `build` status requires formatting, quality checks, library tests, and every
package shard to succeed. New commits cancel obsolete runs for the same workflow
and Git ref.
