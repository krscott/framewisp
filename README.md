# framewisp

Run a Wayland or X11 app without a physical display, save PNG screenshots or MP4 recordings, and
send clicks, drags, and keystrokes from the CLI. The MVP targets this repo's NixOS
development environment and includes a small GTK demo.

## Stack

Framewisp's Python CLI manages the session and calls these tools:

| Component | Role |
| --- | --- |
| Sway / wlroots | Runs the headless Wayland display with Pixman software rendering. |
| wayvnc | Provides virtual pointer and keyboard devices through a VNC server on a private Unix socket. |
| vncdotool API | Sends clicks, drags, scrolling, and keyboard input to wayvnc. |
| Xwayland | Runs X11 clients inside the private Sway session when `run --x11` is selected. |
| xmodmap / xdotool | Keep X11 Unicode key mappings and send their key events. |
| wtype | Types non-ASCII text through a Wayland virtual keyboard with a matching keymap. |
| grim | Captures the Wayland display as a PNG. Screenshots do not use VNC. |
| wf-recorder | Records the display as silent H.264 MP4 when requested, using its FFmpeg libraries. |
| GTK 4, GLib, and PyGObject | Provide the bundled demo and test apps, their event loops, and Python bindings. |

The Nix flake pins and packages these dependencies and wraps the installed
commands with their runtime environment. Target applications, including Flatpaks,
are installed separately.

Development uses pytest and Pillow to run real GUI tests and compare screenshots.
FFmpeg's `ffmpeg` and `ffprobe` commands render captions, inspect finished recordings,
and decode recordings in tests. The package supplies them for Framewisp's internal
use; the development shell also puts them on PATH. Mypy and Pyright check types; Black, isort, and
nixfmt format the source. Setuptools builds the Python package, and Just provides
development command recipes.

See [DESIGN.md](DESIGN.md) for how the components communicate and
[flake.nix](flake.nix), [default.nix](default.nix), and
[pyproject.toml](pyproject.toml) for dependency declarations.

## Install or run with Nix

The flake supports `x86_64-linux`. The package supplies framewisp's display,
input, screenshot, and recording tools, plus the bundled GTK demo. No development
shell, checkout, virtual environment, or sudo is needed for `nix run`. External
applications such as Flatpaks still need their own installation.

Run directly from the public repository:

```sh
nix run github:krscott/framewisp -- \
  demo run -- framewisp-demo
```

Repeat the same `nix run ... --` prefix for input and screenshot commands.
The first invocation builds or downloads the package and its dependencies.

To put `framewisp` and `framewisp-demo` on PATH, add the flake to your NixOS or
Home Manager configuration's inputs:

```nix
inputs.framewisp.url = "github:krscott/framewisp";
```

Pass `inputs` to your modules through `specialArgs` (NixOS) or
`extraSpecialArgs` (standalone Home Manager). Then select the package:

```nix
{ inputs, pkgs, ... }:
{
  # Home Manager:
  home.packages = [
    inputs.framewisp.packages.${pkgs.stdenv.hostPlatform.system}.default
  ];

  # For NixOS, use environment.systemPackages instead of home.packages.
}
```

Apply your configuration normally. No framewisp service or dedicated module is
needed.

## Agent instructions

Print an agent skill with every command's syntax, options, and workflow examples:

```sh
framewisp --agent-skill
# Without framewisp on PATH:
nix run github:krscott/framewisp -- --agent-skill
```

This prints the bundled [skill body](framewisp/SKILL.md) as plain Markdown and
exits. It needs no session or display and does not start an app.

## Try it

Apps started with `run` use a private display and run as the invoking user,
with the file and account access allowed by the surrounding environment.
The private display does not restrict that access.

For known targets with an accessible outcome, batch the inputs and a bounded
check. Use screenshots to discover unknown pointer coordinates and to check
appearance, layout, canvas content, or controls missing from accessibility.
Accessible bounds are window-relative toolkit units, not necessarily display
pixels. Inspection cost and coverage vary by app.

After installation, start a fresh demo session in the first terminal:

```sh
check_dir=$(mktemp -d /tmp/framewisp-check.XXXXXX)
cd "$check_dir"
framewisp demo run -- framewisp-demo
```

Wait for `Session ready:` and keep that process running. The app may still be
drawing. In a second terminal, change to the working directory chosen in the
first terminal, then create and run this batch:

```sh
check_dir=$(pwd)
cat > check.json <<'JSON'
{
  "actions": [
    {"action": "wait", "timeout": 5, "condition": {"role": "text box", "field": "text", "equals": ""}},
    {"action": "click", "x": 120, "y": 100},
    {"action": "type", "text": "HelloGUI", "interval": 0},
    {"action": "key", "chord": "Return"},
    {"action": "wait", "timeout": 5, "condition": {"role": "label", "name": "Entered:", "field": "text", "equals": "Entered: HelloGUI"}}
  ],
  "failure_capture": {"path": "check-failed.png"}
}
JSON
framewisp demo batch --file check.json
```

The first wait checks that the fresh demo's empty entry is accessible before
clicking its known coordinates. `interval: 0` removes deliberate typing pauses;
use paced typing for demonstrations or timing-sensitive behavior. The final wait
checks for `Entered: HelloGUI`. Read `status: "completed"` and `verified: true` in
the JSON. This verifies the requested accessible conditions, not appearance or
every app effect. Successful input without checks has `verified: null`.

To try failure, repeat with a fresh demo session and working directory, changing
only the last `equals` to `"Entered: Wrong"`. The batch exits 1 with
`status: "failed"`, `verified: false`, `failed_phase: "check"`, and `failed_index: 4`.
Read `error` and `results[4].observation` for the last accessible result and its
diagnostics. Open the failure PNG listed in `artifacts`, or read
`failure_capture_error` if capture failed. Earlier input remains in the app;
inspect its state before deciding what to do next. The fresh working directory
ensures the failure capture uses a new filename.

No success screenshot needs inspection. For visual evidence or an accessibility
gap, capture a PNG and open it, or have the agent use its image-viewing tool.
Choose a delay when intentionally allowing time for rendering or an animation:

```sh
framewisp demo screenshot --delay 0.3 "$check_dir/visual.png"
```

Stop the session when finished:

```sh
framewisp demo stop
```

The primary path uses three CLI invocations (run, batch, stop) and zero images
inspected. The optional visual fallback adds one capture and image inspection.
Ctrl+C in the first terminal or SIGTERM to the runner also stops it. An agent can
keep the foreground runner alive through its shell tool while sending the batch.

The installed package supplies the display and input tools. You can also use
these commands inside `nix develop` when working on the source.

## Commands

Every command takes a session name or explicit path before the subcommand: `framewisp SESSION COMMAND ...`.
Use a bare name such as `browser` or `demo` by default. Names resolve to
`/tmp/framewisp-project-<hash>/NAME`, where `<hash>` is the first 24 hexadecimal
characters of SHA-256 of the canonical project path. Inside Git, the project is
the worktree root, so commands from its subdirectories agree. Separate worktrees
have separate sessions. Outside Git, the project is the current working directory.
Symlinked project paths resolve to the same canonical path. Git must be installed
(it is included in the Nix package).

Explicit paths such as `/tmp/framewisp-demo`, `./browser`, and `sessions/browser`
keep their existing behavior. A bare name never falls back to a local or legacy
session directory. Empty names and `.`/`..` are invalid; use `./` or `../` to
explicitly select those directories. Names must fit in 255 bytes.

Agents in the same project must choose distinct names to avoid sharing a session.
Both `run` and user-started `attach` print the resolved session directory at
startup. Use that explicit path to control the session from another project.
Project scoping does not change sandbox socket permissions. `--detach` remains
session-independent.

| Command | Behavior |
| --- | --- |
| `run [--retain-input-content] [--width W] [--height H] [--record FILE] -- APP [ARGS...]` | Start the display, optional recording, and application; stay in the foreground. |
| `attach [--retain-input-content]` | Request capture and input access to your existing desktop; stay in the foreground. |
| `--detach` (no session) | Stop the active desktop attachment and leave your apps running. |
| `record-start [--no-captions] FILE` | Start a clip in the running session. |
| `record-stop` | Finalize the clip without stopping the app; print its measured summary as JSON. |
| `status` | Query the live headless runner and print app, display, and recording state as JSON. |
| `inspect --json [--role ROLE] [--name TEXT] [--text TEXT]` | Query a bounded set of accessible controls in a headless session. |
| `stop` | Stop the headless session and wait for cleanup and recording finalization. |
| `recover` | Recover an abandoned headless session after its owner and supervisors exit, preserving logs. |
| `screenshot [--delay SECONDS] [--region X Y WIDTH HEIGHT] [--json] PATH` | Write a full-resolution PNG. Headless sessions support crops and JSON origin, dimensions, display size, path, and capture time. Default delay: 0. |
| `batch --file FILE` | Execute a JSON sequence in a headless session, optionally capture a PNG, and print ordered results and timing. |
| `move X Y` | Move the pointer immediately without pressing any button. |
| `scroll X Y DIRECTION [--steps N]` | Send wheel steps to the pane at these coordinates; directions: up, down, left, right. |
| `click [--button left\|right] [--count 1\|2] [--modifier NAME] X Y` | Move the pointer and click (default: one left click). Coordinates start at the top left. |
| `drag [--button left\|right] [--modifier NAME] [--duration SECONDS] X1 Y1 X2 Y2` | Hold the chosen button while moving along a straight path (default: left, 0.4 seconds). |
| `type [--interval SECONDS] TEXT` | Send printable Unicode with a pause between characters (default: 0.08 seconds). |
| `key CHORD` | Press/release a key with optional Ctrl, Shift, and Alt modifiers. |

The automated tests cover the bundled demo on Wayland and Xwayland. Swell Foop 50.0 and
KolourPaint 26.04.3 have also been tested manually as Flatpaks (see below).
Native X11 desktop attachment and GPU-dependent apps are outside this MVP.

Clipboard forwarding between VNC clients and the session is disabled. Applications
can still use Ctrl+C/Ctrl+V and primary selection. Framewisp's Nix package patches
wayvnc to disable forwarding independently of keyboard and pointer input, avoiding
the clipboard-offer crash described in [#30](https://github.com/krscott/framewisp/issues/30).

The display defaults to 1280 by 720 pixels. Set `run --width 1600 --height 900`
for more room. Screenshots and input use those pixel dimensions, with `(0, 0)`
at the top left. Width and height must be positive integers; recording also
requires both to be even, because the recorder crops odd dimensions.

`Session ready:` means the display and input sockets exist and the application
process has started. The app may still be drawing its first frame. A screenshot
captures the current display; it does not wait for the app to finish responding
to input automatically. Prefer a [bounded check](docs/conditional-checks.md) for
a known accessible result. For visual-only changes such as animations, choose
a delay before capture:

```sh
framewisp demo screenshot --delay 0.5 /tmp/after.png
```

The delay accepts finite, nonnegative seconds, including fractions. It defaults
to zero and does not count toward the capture process's ten-second timeout.
Capture again when necessary.

## Demo controls

`framewisp-demo` includes controls for every input command. The demo uses bundled DejaVu Sans 11 to keep its control positions stable. At the default
1280x720 session size, use these targets:

| Interaction | Target and result |
| --- | --- |
| Type, Return, Ctrl+A, Shift+arrows | Entry near `(120, 100)`; Return displays `Entered: TEXT`. |
| Click | Apply near `(120, 170)` displays `Applied: TEXT`; the option near `(54, 266)` toggles. |
| Double-click | A word in the entry, for example `(80, 100)` after typing `alpha beta`; replacement typing changes only that word. |
| Right-click | The entry opens GTK's text menu. Select All followed by BackSpace clears the entry. |
| Move / hover | Move to `(150, 266)` and capture with `--delay 1` to see the option's tooltip. |
| Drag | Move the slider from `(147, 382)` toward `(350, 382)`; its number changes. The entry also supports selection drags and Shift-click. |
| Key | Space toggles a focused option; arrow keys adjust the focused slider. Tab and Shift+Tab move between controls. |
| Scroll | At `(700, 250)`, scroll down/up or right/left. Row/column labels and the `Scroll: x=... y=...` label show the position. |
| Reset | Click `(700, 513)` to clear text, turn the option off, restore the slider to 25, scroll to the origin, and focus the entry. |

Coordinates apply to the packaged demo in the default headless session. Inspect a
screenshot when using a different window size, desktop theme, or display scale.
Use `--duration` for paced drags, `--interval` for paced typing, and screenshot
`--delay` when a tooltip or scroll animation needs time. The demo logs resulting
widget state to `app.log`; screenshots show the same values. Run `framewisp-demo`
directly to use these controls on your desktop.

## Attach to an existing desktop

Use this when an app is already running on your desktop and you want an agent to
inspect its current state. The initial target is COSMIC on Wayland. Your desktop
must provide the RemoteDesktop and ScreenCast portals; framewisp supplies its own
capture tools through the Nix package.

Before sharing, configure a desktop shortcut that runs:

```sh
framewisp --detach
```

In COSMIC Settings, open **Input devices > Keyboard > Keyboard shortcuts** and add
custom shortcuts with that command for both Ctrl+Alt+Escape and
Ctrl+Alt+Shift+Escape. The second binding lets physical Ctrl+Alt+Escape stop
access while framewisp holds Shift for a gesture. Use the absolute path to
the installed `framewisp` executable if your desktop does not have it on PATH.
The same bindings work for every session. Only one desktop attachment can run
at a time.
Verify the shortcuts appear in the saved list. On the tested COSMIC version,
the custom-shortcut form displayed the chord without saving it. The equivalent
entries in `~/.config/cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom` are:

```ron
(
    modifiers: [Ctrl, Alt], key: "Escape",
    description: Some("framewisp-detach"),
): Spawn("framewisp --detach"),
(
    modifiers: [Ctrl, Alt, Shift], key: "Escape",
    description: Some("framewisp-detach-held-shift"),
): Spawn("framewisp --detach"),
```

Close Settings before editing that file. Add the entries inside its existing
outer braces, keeping your other bindings.

The user must start sharing from an interactive terminal on their desktop:

```sh
framewisp /tmp/debug-app attach
```

Read the instructions and type `ATTACH` to confirm your stop bindings are configured
and you understand the access. Then approve keyboard/pointer access and select one
monitor in the desktop dialog.
Keep that terminal open. Do not background or suspend the command. Agents must ask
the user to run it, never allocate a terminal to bypass the startup check. The
terminal check prevents accidental agent startup; it is not a security boundary
against programs running under the same user account.

Other processes with access to the attachment's state and control socket can
issue commands. Portal approval grants desktop access to the attachment; it
does not authenticate a particular agent.

Wait for `Attached:`. An agent running under the same user account can now use
another terminal:

```sh
framewisp /tmp/debug-app screenshot /tmp/current.png
framewisp /tmp/debug-app click 400 300
framewisp /tmp/debug-app type 'Hello'
```

Coordinates refer to pixels in the selected monitor's screenshot. Input shares
your live pointer and keyboard focus. Keyboard input goes to the focused app,
even if that app is on another monitor. Screenshots include everything visible
on the shared monitor.

Once sharing becomes active, framewisp registers a tray icon with the label
"framewisp desktop sharing is active" and a "Stop sharing" menu item. Open its
menu to end access. Supported tray hosts are KDE Plasma, COSMIC's status area,
and GNOME with an enabled StatusNotifierItem/AppIndicator extension (for example,
AppIndicator and KStatusNotifierItem Support). Framewisp uses its existing Gio
runtime dependency; no root access or extra indicator library is required.
The desktop controls icon placement, overflow, panel auto-hide, and visibility.
Registration does not guarantee a visible icon. If no compatible host is available,
the terminal reports that the indicator is unavailable and gives the shortcut
and Ctrl+C stop methods. Keep the emergency shortcuts configured even with a tray
icon: its menu requires a responsive owner. The indicator disappears when the
attachment ends or its owner exits; a new attachment registers a new item.
See [tray verification](docs/desktop-sharing-tray.md) for setup and verification status.

Ctrl+Alt+Escape runs `framewisp --detach`. Ctrl+C, loss of the controlling terminal,
pressing Ctrl+Z, or portal revocation also ends access. Detaching from tmux
or screen can leave that terminal and the attach process running. Use `--detach`
before disconnecting from a terminal multiplexer. If the attach
process crashes or is killed, its private portal connection and capture handles
close with it. The stop command escalates to killing an unresponsive attach process
after half a second. It uses Linux 6.5 or newer to identify the socket owner safely.
Disconnecting cancels ongoing input and releases framewisp's held keys and buttons. Your app stays
open in its current state. Reconnecting requires running `attach` and approving
the dialog again. Test your binding before handing control to an agent.

Attached sessions support screenshots and input commands, including input logs.
Keyboard shortcuts currently assume a US keyboard layout.
Recording attached sessions is not implemented yet.

## Hover feedback

Move over a control, then allow time for its tooltip to appear:

```sh
framewisp paint move 510 50
framewisp paint screenshot --delay 1 /tmp/tooltip.png
```

`move` sends no button presses. Coordinates start at the display's top left.
Movement is immediate; the app decides what hover feedback to show and when.

## Right-click and double-click

Choose the mouse button and click count:

```sh
framewisp paint click --button right 500 400
framewisp paint click --count 2 500 400
```

`--button` accepts `left` (default) or `right`. `--count` accepts `1` (default)
or `2`. Two clicks use the same connection and position, with a 0.1-second pause
between them. Every click presses and releases the chosen button. The target
app's settings and the control under the pointer determine how it responds.
Plain `click X Y` still sends one left click.

The runner sends input through one persistent connection for the session. It
serializes concurrent commands, including Unicode typing. This keeps the
keyboard and pointer available between commands, so Qt context menus remain open
for a later screenshot or click. Disconnecting an input CLI cancels its queued
or running action and releases held keys/buttons before the next action. Status
and stop remain available during paced input. Connection failures stop the session
without replaying input; restart the session before trying again.

## Batch known actions

The primary example above batches input and checks an accessible outcome.
For a visual-only outcome, save known inputs and an optional final capture as JSON:

```json
{
  "actions": [
    {"action": "click", "x": 120, "y": 100},
    {"action": "type", "text": "HelloGUI", "interval": 0},
    {"action": "key", "chord": "Return"}
  ],
  "capture": {"path": "/tmp/result.png"}
}
```

```sh
framewisp demo batch --file check.json
```

The headless runner validates the complete sequence, then executes it without
interleaving other clients' input, including during capture. Defaults match the
individual commands. `interval: 0` removes deliberate typing pauses; explicit
pacing remains available. An optional capture `delay` waits before the screenshot.
Relative capture paths use the CLI's working directory. Use a new filename in an
existing directory. The PNG appears only after successful capture.

The CLI prints structured results, timing, completed-action count, and artifact
paths. This input-and-capture example returns `verified: null`; inspect its PNG
before claiming visual success. On runtime failure it exits with status 1 and reports the zero-based
`failed_index` and `failed_phase`. Capture failure has a null failed index.
Execution stops at that point; completed inputs cannot be rolled back, and even
the failed action may have sent partial input. Never replay a failed batch without
inspecting the app first. Disconnecting cancels queued or remaining work and
releases held input. A missing reply leaves completion uncertain; consult the app
and per-action input log. Capture does not prove the app has finished processing.

Batches require 1 to 256 steps, at most 16,384 typed characters and 10,000 scroll
steps, and at most 300 seconds of requested pacing and check deadlines. File and wire request sizes
are limited to 1 MiB each. Supported actions are `move`, `click`, `drag`, `scroll`,
`type`, `key`, `wait`, `assert`, and `baseline`; capture is separate. No attached desktops, loops, lifecycle
commands. Checks support known accessible text, name, value, and checked/enabled
state. Checks keep conservative traversal defaults. For larger trees, set
`"observation": {"max_nodes": 1024, "max_depth": 16, "timeout": 5}` on each
`baseline`, `wait`, or `assert` step. The step's own `timeout` remains the total
deadline; partial observations never verify state. See [conditional checks](docs/conditional-checks.md) for waits, assertions,
transition baselines, and failure screenshots. See the [agent skill](framewisp/SKILL.md#batch-known-actions)
for the schema and error handling, and [batch measurements](docs/batch-latency.md)
for complete CLI timings with app acknowledgements.

## Pointer gestures with modifiers

Clicks and drags accept `--modifier ctrl`, `--modifier shift`, or
`--modifier alt`. Names are case-insensitive. Repeat the option to combine
different modifiers; duplicates are rejected.

```sh
framewisp drawing click --modifier shift 480 330
framewisp drawing drag --modifier ctrl 310 330 410 330
framewisp drawing drag --button right 300 300 500 300
```

Modifiers stay pressed for the entire gesture, then release in reverse order
on the same connection. They do not remain held for the next command. Drags
accept the same left/right button choices as clicks. The app decides what each
combination does.

## Scrolling

Move the pointer to the pane you want to scroll, then send wheel steps:

```sh
framewisp paint scroll 500 400 down --steps 3
framewisp paint scroll 500 400 up --steps 3
```

Directions are `up`, `down`, `left`, and `right`. The step count must be a positive
integer and defaults to one. Steps are discrete wheel ticks, not pixels; the
widget under the pointer determines how far content moves. Each step presses and
releases its wheel button. All steps use one connection, with no buttons left
held. There is no smooth scrolling or momentum control.

## Keyboard shortcuts

Use `key` for a key or modifier combination:

```sh
framewisp demo key Ctrl+a
framewisp demo type 'Replacement text'
framewisp demo key Shift+Left
framewisp demo key Escape
framewisp paint key Ctrl+z
framewisp paint key Ctrl+Shift+z
framewisp paint key Ctrl+s
```

Accepted keys are `a` through `z`, `0` through `9`, `Space`, `Return`, `Tab`,
`BackSpace`, `Escape`, `Delete`, `Left`, `Right`, `Up`, and `Down`. Prefix a key
with any combination of `Ctrl+`, `Shift+`, and `Alt+`, each at most once.
Names are case-insensitive: `Ctrl+A` and `ctrl+a` mean the same shortcut.
Letter case does not add Shift; use `Shift+a` to send a capital A, or `type`
to enter literal text.

Each command presses the modifiers, presses and releases the key, then releases
the modifiers in reverse order on the same connection. Modifiers do not remain
held for the next command. Unknown keys and malformed combinations are rejected
before connecting. Shortcut behavior depends on the focused app and control.

## Record a session

Add `--record` before the application command to save a silent MP4:

```sh
framewisp demo run --record /tmp/demo.mp4 -- framewisp-demo
```

Recording starts before the app launches. You can also start and stop individual
clips after setting up the app:

```sh
framewisp demo record-start /tmp/first.mp4
framewisp demo type 'First demonstration'
framewisp demo record-stop
# Change the app's state, then record another clip.
framewisp demo record-start /tmp/second.mp4
framewisp demo type 'Second demonstration'
framewisp demo record-stop
```

`record-start FILE` returns when capture is ready. `record-stop` returns after the
MP4 is finalized and playable, leaving the app running. It can also stop a clip
started with `run --record FILE`. Only one recording can be active at a time;
starting another or stopping when none is active reports an error.

On success, `record-stop` prints JSON with the output `path`, `duration_seconds`,
`width`, `height`, and `size_bytes`. Framewisp uses ffprobe and the completed file
to measure these values after caption rendering; duration is not a wall-clock estimate.

Recordings are silent H.264 MP4 files at 30 fps and the session's display size.
Recording requires even width and height. Existing output files are never
overwritten. The session runner owns the recorder and finalizes an active clip
on normal app exit, Ctrl+C, or SIGTERM. A recorder failure stops the session;
an invalid recording request or failed `record-start` leaves the app running.
The recorder writes diagnostics to `recorder.log`, replaced for each clip.
Keep the runner alive until shutdown completes so it can finalize the MP4.
The development shell includes FFmpeg for video inspection.

## Input logs and recording captions

Every accepted input command appends start and end records to `SESSION/inputs.jsonl`,
even without a recording. Records include an action ID, monotonic timestamp in
seconds, the command and retained parameters, and its return code or exception type. They
record what framewisp attempted and whether the input command completed, not
whether the app responded as intended. A start without an end indicates an
unfinished command. Reusing a session directory starts a fresh log.

Input logs and captions omit literal typed text by default, including Unicode and
individual letter, digit, Space, and Shift-only literal key events. Captions say
"Type text" or "Key" (with any Shift modifier). Ctrl/Alt shortcuts and named keys
such as Return and arrows remain visible. These describe shortcuts, not text entry.
Input failure details that could contain text or encoded keys are omitted; error
types, cancellation, and exit codes remain available.

To retain full input content for debugging or demonstrations, start the session
with `run --retain-input-content` or `attach --retain-input-content`. This explicit
opt-in applies to every individual input, batch action, and clip for that session.
It cannot be changed by a batch file or `record-start`. Attached sessions log input
but do not support recordings.

Recordings show input captions by default, including shortcuts and pointer gestures. Captions stay visible during paced input and briefly after quick
commands, until the next action. Opted-in text is abbreviated in the video; the log
keeps the full text. Inputs between clips are excluded from the next clip.
Captions appear only in recordings, never in app screenshots.

Disable captions for a clip with:

```sh
framewisp demo record-start --no-captions /tmp/plain.mp4
# Or start the session with an uncaptioned recording:
framewisp demo run --record /tmp/plain.mp4 --no-captions -- framewisp-demo
```

Input logging remains enabled and follows the session retention policy. Captions are rendered into the video frames so
GitHub's inline player displays them; viewers cannot toggle them off afterward.
`record-stop` and normal session shutdown wait for caption rendering to finish.
This adds encoding time when stopping a captioned clip. If rendering fails, the
command fails and leaves the uncaptioned MP4 at the requested path; see
`captions.log` for details.

The recorder log retains diagnostics and the first frame's timestamp used for
caption alignment. Framewisp filters the remaining Wayland protocol trace as it
arrives instead of retaining it for every frame.

The Nix package includes FFmpeg and caption fonts for Latin, Greek, Cyrillic, CJK,
and monochrome emoji. Apps inherit font configuration unless you select a
[fresh profile](#fresh-app-profiles). Caption text uses fullwidth equivalents
for braces and backslashes to prevent subtitle formatting;
opted-in input logs preserve the original characters. Screenshots and recordings
can still show secrets displayed by apps. Omitting input content does not redact
screen content or provide a general app-log redaction guarantee. Shell history,
batch files, application logs, and runtime input state are outside this policy.
Review artifacts before sharing.

## Fresh app profiles

Opt in when you want repeatable initial app preferences:

```sh
framewisp browser run --profile fresh -- your-app
framewisp browser run --profile fresh --x11 -- your-x11-app
```

Each run creates private HOME, XDG config/cache/data/state directories and TMPDIR
under its owned runtime directory. It starts with empty app preferences.
`XDG_CONFIG_DIRS` points at the private config directory too. Normal runs still
inherit the caller's app settings.

The profile sets `LANG` and `LC_ALL` to `C.UTF-8`, removes inherited locale
overrides, and uses a separate bundled Fontconfig configuration with DejaVu and
Noto fonts. It loads no host Fontconfig rules or system font directories. GTK 3/4
use Adwaita, DejaVu Sans 11, a light theme, 96 DPI and scale 1, with animations
and overlay scrollbars disabled. Qt uses Fusion,
96 DPI, scale 1 and software Qt Quick rendering. Inherited GTK/GDK/Qt settings
are cleared except Qt plugin lookup paths needed to load installed binaries.
GTK settings use the keyfile backend inside the profile instead of the user's
dconf database. The display uses scale 1 and the existing US keyboard layout.
Input methods and non-US shortcuts are outside this profile's supported scope.

The existing private session and accessibility buses remain in use. Neither
loads the desktop's default service activation directories. If an app requires
activation, supply a directory containing only its required `.service` files:

```sh
framewisp app run --profile fresh --dbus-service-dir ./app-services -- your-app
```

Repeat `--dbus-service-dir` for additional directories. Only the private app bus
loads these directories; the accessibility bus stays unchanged. Activated
services receive the final app environment, including the discovered display.
Use direct `Exec` entries for these services. User systemd activation and desktop
portals are not configured. Every service in a supplied directory can activate,
so avoid supplying a whole desktop service directory. Framewisp owns activated
services through the existing bus process supervisor and stops them on cleanup.

Successful shutdown deletes all profile directories after stopping the app and
services. There is no profile reuse or persistence option. Copy wanted app data
to an explicit path before stopping. Screenshots, recordings and session logs
retain their normal lifecycle. After a failed cleanup, use `recover` as usual;
it preserves logs and removes the abandoned runtime and profile.

This is a preferences profile, not a security sandbox. The app can still access
the host filesystem, network and system bus. Existing session/artifact path
protections apply; save artifacts outside the disposable runtime. The profile
preserves PATH, working directory, XDG data search directories, library/plugin
paths and app-specific environment variables. App versions, installed schemas,
icons, toolkit overrides, rendering differences, time-dependent content and
remote data remain outside its control. Explicit app arguments or an `env`
wrapper can override the defaults. Apps with hard-coded settings paths, separate
daemons, or their own font engines need app-specific configuration. Flatpak can
remap HOME, XDG directories and fonts inside its sandbox, so these guarantees
apply to native apps that honor the profile environment, not arbitrary Flatpaks.

`tests/test_profiles.py` runs GTK's shipped demo application on Wayland and X11,
including an explicit dconf backend override and private service activation.
It checks fresh preferences,
font/theme/DPI settings, matching content pixels across conflicting host scale
settings, private preference writes, unchanged host config, and cleanup. The
bundled framewisp demo keeps DejaVu Sans 11 and selects the profile's Fontconfig
file during fresh runs, even when its installed wrapper sets caption fonts.

## Flatpak game

The [real-app compatibility runs](docs/ui-inspection.md#real-app-compatibility-runs)
cover a GTK 3 Writer Flatpak and Blender on private Wayland and Xwayland, with
versions, launch commands, input outcomes, accessibility limits, and opt-in tests.

With the `org.gnome.SwellFoop` Flatpak installed, run:

```sh
framewisp swell run -- \
  flatpak run --socket=wayland org.gnome.SwellFoop
```

Use the same screenshot and click commands as for the demo. A manual test of
Swell Foop 50.0 with GNOME runtime 50 covered starting a game, removing tile
groups, Undo, Redo, and stopping the runner with SIGTERM. All test processes,
including the Flatpak wrapper and game, exited on shutdown.

The game rendered and accepted input despite warnings about the missing D-Bus
session and settings portal. Other Flatpak apps may need those services. An
immediate screenshot after clicking Let's Play still showed the welcome screen;
later captures showed the board. Waiting 0.5 seconds after subsequent moves was
enough for this test; use `screenshot --delay 0.5 PATH` to request that wait.
This is not a general animation-completion guarantee.

## Drawing with a drag

With the `org.kde.kolourpaint` Flatpak installed, run:

```sh
framewisp paint run -- \
  flatpak run --socket=wayland --env=QT_QPA_PLATFORM=wayland org.kde.kolourpaint
```

In the tested default layout, select the Rectangle tool, drag across the blank
canvas, and capture the result:

```sh
framewisp paint click 57 301
framewisp paint drag 150 130 400 300
framewisp paint screenshot --delay 0.5 /tmp/rectangle.png
framewisp paint click 310 50  # Undo
framewisp paint screenshot --delay 0.5 /tmp/undone.png
```

Inspect a screenshot first if your toolbar or canvas layout differs. The manual
test used KolourPaint 26.04.3 with KDE runtime 6.10. The gesture sends intermediate positions along a straight path while holding the
left button. By default it takes about 0.4 seconds. Choose a duration for slower
or faster gestures:

```sh
framewisp paint drag --duration 1.2 150 130 400 300
framewisp demo type --interval 0.15 'Slower typing'
```

Typing waits 0.08 seconds between characters by default, with no extra pause
after the last character. Both options accept finite, nonnegative seconds;
use zero for immediate input. Input timeouts allow for the requested duration.
These defaults provide a readable pace, not a simulation of human behavior.
Scheduling and app rendering can affect the observed timing.

A drag still moves immediately to its starting point, and clicks still move
immediately to their destination. Smooth movement before clicks, random timing,
and curved paths are deferred. `screenshot --delay` remains a separate wait
before capture; it does not change input timing.

## Headless X11 apps

Add `--x11` to `run` to start a private Xwayland server inside the headless compositor:

```sh
framewisp x11 run --x11 -- framewisp-demo
# In another terminal:
framewisp x11 type 'Hello X11! café 日本語 😀'
framewisp x11 key Return
framewisp x11 screenshot /tmp/x11.png
```

The demo prints `Display: X11Display` in `app.log` and shows that backend in its
heading. This verifies GTK is using X11. The same click, drag, scroll, keyboard,
screenshot, and recording commands work in this mode. All required tools and the
demo come with the Nix package; no X11 desktop or development shell is needed.

The runner discovers Xwayland's allocated display number. It removes the host's
`DISPLAY`, `WAYLAND_DISPLAY`, and `XAUTHORITY`, then gives the app the private
`DISPLAY` and `XAUTHORITY=/dev/null`. It sets `GDK_BACKEND=x11`,
`QT_QPA_PLATFORM=xcb`, and `SDL_VIDEODRIVER=x11` for the app. Display selection is
not a security sandbox for untrusted applications. Sway owns Xwayland and shuts
it down when the session ends.

The bundled GTK demo is the tested X11 target. GPU/game support, desktop services,
and native X11 desktop attachment are outside this mode's scope. X11 Flatpak
launching has not been verified. X11 pointer commands prime the virtual pointer
one pixel beside the target before moving to it; this adds 100 ms before the
requested action to avoid losing its first motion.

## Unicode text

```sh
framewisp writer type 'café Ελληνικά Русский 日本語 😀'
```

`type` accepts characters that Python classifies as printable, including combining
accents and individual emoji. Use `key Return` and `key Tab` for those keys.
Control characters, line breaks, and format characters such as zero-width joiners
are rejected before input. This does not implement an input method or compose
emoji sequences with joiners. The app's fonts determine how characters look.

ASCII text uses VNC. In a Wayland session, a command containing non-ASCII text uses `wtype`, included
in the Nix package, to create a keymap for its characters. Unicode intervals round
up to whole milliseconds; `wtype` also adds about 4 ms per character for key
press/release. `--interval 0` removes the between-character pauses.

In an X11 session, Unicode text uses persistent mappings in the private server
and `xdotool` key events. A 100 ms pause initializes XTest input before typing;
then pauses occur between characters. `--interval 0` removes
the between-character pauses. Each session supports at most 128 distinct
non-ASCII characters across its text commands; repeats do not count again.
Start a new session to use a different character set once that limit is reached.
A command that would exceed the limit fails before sending any input. These
mappings use upper keycodes reserved by framewisp; ordinary US keys
and the supported shortcuts retain their mappings.

LibreOffice Writer was tested as a Flatpak with a private D-Bus session:

```sh
framewisp writer run -- \
  dbus-run-session -- flatpak run --socket=wayland --nosocket=x11 \
  --env=SAL_USE_VCLPLUGIN=gtk3 org.libreoffice.LibreOffice --writer
```

Inkscape 1.4.4 was tested with Shift-click selection and Ctrl-drag constraints.
Papers 50.2 was tested with PDF navigation, zoom, and search at 1600x900.

## Logs and cleanup

Use `framewisp SESSION status` to query the live headless runner. Its JSON output
includes the backend, display dimensions, app PID and running state, and the
active recording path/PID/running state (or null).

Use `framewisp SESSION stop` to request normal shutdown. It waits for managed
processes to exit, runtime sockets and session metadata to be removed, and any
active clip to be finalized. Success prints JSON with `status: "stopped"` and the
last recording summary, if one exists. Neither command sends signals to stored
PIDs. Disconnected sessions and metadata copied from another session are rejected.
These commands manage headless sessions; use `framewisp --detach` for desktop
attachment. Stop requires a responsive runner; it is not a forced crash-recovery command.
After upgrading from a recording-only control protocol, stop the old runner with
Ctrl+C or SIGTERM and start a fresh session. New control commands reject that old
protocol before sending a request.

The session directory contains `sway.log`, `wayvnc.log`, and `app.log`, plus
`recorder.log` when recording. During a
run, `session.json` records the private runtime directory, Wayland socket name,
and managed process IDs. Normal shutdown removes the runtime sockets and
`session.json`, and keeps the logs. Reusing the directory replaces its logs.

The runner stops its managed processes on Ctrl+C, SIGTERM, or application
exit. It returns the application's exit code when the app exits on its own.
Each managed process has a private Linux subreaper supervisor that also stops
forked helpers and detached descendants. Cleanup allows five seconds for SIGTERM
and two more for SIGKILL per tree, and reports failures with process IDs and logs.
Other sessions and unrelated processes remain outside these trees.

Headless runs require Linux 5.3+, readable procfs (including task `children`), and
permission for `prctl`, `pidfd_open`, and `pidfd_send_signal`. Startup checks these
before launching apps and fails if ownership is unavailable. Neither root nor a
systemd user service is required. This owns forked processes; it does not restrict
access to your files or own work launched by an existing external service.
After runner SIGKILL or interrupted startup, run `framewisp SESSION recover`,
then reuse the directory with `framewisp SESSION run -- APP`. Supervisors start
cleanup on runner loss. Recovery refuses while the runner or any supervisor
holds the kernel ownership lock; wait a few seconds and retry. It removes the
owned runtime and stale session metadata, preserves logs and recordings, and
prints JSON with `status: "recovered"`. Copy those diagnostics before the next
run replaces its logs. An interrupted recording may be incomplete.

Recovery never signals PIDs from metadata or follows its runtime path. Ownership
is bound to the session directory's device and inode, so copied metadata cannot
recover the original run. Keep `.headless.lock` in place, including between runs;
deleting or replacing ownership files while processes are live breaks the lock
protocol. Use a private session directory. Startup and recovery reject session
directories owned by another user or writable by the group or others. Same-user programs that deliberately
replace files or remove ownership records are outside this protocol's protection.

If a supervisor dies or cannot finish cleanup, recovery fails and lists the
remaining supervisor records in the runtime directory. Inspect those records and
processes manually; their PIDs are diagnostic, not safe signal targets. Resources
and ownership metadata remain so recovery cannot claim success. Recovery also
refuses unverifiable journals, replaced runtimes, and stale sessions created by
older versions without an ownership journal. It does not recover desktop
attachments; use `framewisp --detach` for those. Attachment shares the session
metadata lock and refuses directories with abandoned headless ownership.

Headless sessions start private D-Bus session and accessibility buses plus an
AT-SPI registry. These buses do not activate host desktop services. Apps that
need additional D-Bus services may still require their own setup.

Expected startup failures include a bounded tail of the component log. Failed
headless display commands identify the session, display/socket directory, and
tool error. If running in an agent sandbox, both the runner and every control
command need permission to access the private sockets. A successful launch
outside the sandbox does not give later sandboxed calls access. Use your execution
environment's normal permission process when needed. A connection failure can
also mean the app or display exited; inspect the diagnostic before deciding.

## Development

Enter `nix develop` from a checkout. It creates the development virtual
environment on first use. Then run:

```sh
python -m pytest
python -m mypy .
python -m pyright
```

Tests run a real compositor, VNC server, and GTK app. They check input through
the demo's output, compare screenshot regions, decode recordings, and verify
managed-process cleanup.

After changing Python dependencies or script entry points, refresh the existing
virtual environment with `python -m pip install -e '.[dev]'` inside `nix develop`.

Run `nix flake check` to test the installed package in an empty environment,
including real input, screenshots, and recording. This checks that it works
without the development shell.

See [DESIGN.md](DESIGN.md) for the implementation and
[GitHub issues](https://github.com/krscott/framewisp/issues) for deferred work.

## License

Framewisp is licensed under the GNU General Public License, version 3 only
(`GPL-3.0-only`). See [LICENSE](LICENSE) for the full text.

## Inspect accessible controls

Headless GTK and Qt apps can expose text and state without a screenshot:

```sh
framewisp demo inspect --json --role button --name 'Apply text'
framewisp demo inspect --json --role 'text box'
framewisp demo inspect --json --role label --text 'Applied:'
```

This is a read-only prototype. It returns roles, names, text, state flags,
action names, numeric values, and window-relative bounds where the app exposes
them. Filters combine with AND. Role names come from the toolkit; name/text
filters match substrings without regard to case. Add `--limit`, `--max-depth`,
`--max-nodes`, or `--timeout` to change the bounded defaults shown in `inspect --help`.

Check `status` in the JSON. Only `ok` exits zero; an empty complete result means
no match in the exposed tree. `partial`, `timeout`, `unsupported`, and `unavailable`
exit 1 and include reasons. No registered accessible app can mean either an
unsupported app or an app still starting. Duplicate matches remain separate.
IDs belong to one observation and cannot be reused as selectors. Bounds are
window-relative toolkit coordinates, not guaranteed screenshot pixels.

Keep screenshots for appearance, canvases, omitted controls, and pointer targeting.
Desktop attachment does not support inspection. See the [support matrix and
measurements](docs/ui-inspection.md) before relying on a particular toolkit.
