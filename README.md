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
  /tmp/framewisp-demo run -- framewisp-demo
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

After installation, in the first terminal:

```sh
framewisp /tmp/framewisp-demo run -- framewisp-demo
```

Wait for `Session ready:`. Keep that process running. In a second terminal, run:

```sh
framewisp /tmp/framewisp-demo screenshot /tmp/before.png
```

Open the PNG, or have the agent inspect it with its image-viewing tool. The demo
has a text field near `(120, 100)` and an "Apply text" button near `(120, 170)`.

```sh
framewisp /tmp/framewisp-demo click 120 100
framewisp /tmp/framewisp-demo type 'Hello Wayland!'
framewisp /tmp/framewisp-demo key BackSpace
framewisp /tmp/framewisp-demo key Return
framewisp /tmp/framewisp-demo click 120 170
framewisp /tmp/framewisp-demo screenshot /tmp/after.png
```

The final screenshot should show `Hello Wayland` in the field and
`Applied: Hello Wayland` below the button. Stop the runner with Ctrl+C in the
first terminal, or send SIGTERM to the `framewisp ... run` process. An agent can
keep that foreground process running through its shell tool while it makes
separate CLI calls.

The installed package supplies the display and input tools. You can also use
these commands inside `nix develop` when working on the source.

## Commands

Every command takes a session directory before the subcommand: `framewisp SESSION COMMAND ...`.
The session directory is required; the former `--session DIRECTORY` spelling is no longer supported.

| Command | Behavior |
| --- | --- |
| `run [--width W] [--height H] [--record FILE] -- APP [ARGS...]` | Start the display, optional recording, and application; stay in the foreground. |
| `attach` | Request capture and input access to your existing desktop; stay in the foreground. |
| `--detach` (no session) | Stop the active desktop attachment and leave your apps running. |
| `record-start [--no-captions] FILE` | Start a clip in the running session. |
| `record-stop` | Finalize the clip without stopping the app; print its measured summary as JSON. |
| `status` | Query the live headless runner and print app, display, and recording state as JSON. |
| `inspect --json [--role ROLE] [--name TEXT] [--text TEXT]` | Query a bounded set of accessible controls in a headless session. |
| `stop` | Stop the headless session and wait for cleanup and recording finalization. |
| `screenshot [--delay SECONDS] PATH` | Wait the requested seconds (default: 0), then write a PNG of the display. |
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
to input automatically. To allow time for an animation, choose a delay before
capture:

```sh
framewisp /tmp/framewisp-demo screenshot --delay 0.5 /tmp/after.png
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

Ctrl+Alt+Escape runs `framewisp --detach`. Ctrl+C, loss of the controlling terminal,
pressing Ctrl+Z, or portal revocation also ends access. Detaching from tmux
or screen can leave that terminal and the attach process running. Use `--detach`
before disconnecting from a terminal multiplexer. If the attach
process crashes or is killed, its private portal connection and capture handles
close with it. The stop command escalates to killing an unresponsive attach process
after half a second. It uses Linux 6.5 or newer to identify the socket owner safely. No separate stop-sharing control was visible
on the tested COSMIC desktop; use the configured shortcut or Ctrl+C.
Disconnecting cancels ongoing input and releases framewisp's held keys and buttons. Your app stays
open in its current state. Reconnecting requires running `attach` and approving
the dialog again. Test your binding before handing control to an agent.

Attached sessions support screenshots and input commands, including input logs.
Keyboard shortcuts currently assume a US keyboard layout.
Recording attached sessions is not implemented yet.

## Hover feedback

Move over a control, then allow time for its tooltip to appear:

```sh
framewisp /tmp/framewisp-paint move 510 50
framewisp /tmp/framewisp-paint screenshot --delay 1 /tmp/tooltip.png
```

`move` sends no button presses. Coordinates start at the display's top left.
Movement is immediate; the app decides what hover feedback to show and when.

## Right-click and double-click

Choose the mouse button and click count:

```sh
framewisp /tmp/framewisp-paint click --button right 500 400
framewisp /tmp/framewisp-paint click --count 2 500 400
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

## Pointer gestures with modifiers

Clicks and drags accept `--modifier ctrl`, `--modifier shift`, or
`--modifier alt`. Names are case-insensitive. Repeat the option to combine
different modifiers; duplicates are rejected.

```sh
framewisp /tmp/framewisp-drawing click --modifier shift 480 330
framewisp /tmp/framewisp-drawing drag --modifier ctrl 310 330 410 330
framewisp /tmp/framewisp-drawing drag --button right 300 300 500 300
```

Modifiers stay pressed for the entire gesture, then release in reverse order
on the same connection. They do not remain held for the next command. Drags
accept the same left/right button choices as clicks. The app decides what each
combination does.

## Scrolling

Move the pointer to the pane you want to scroll, then send wheel steps:

```sh
framewisp /tmp/framewisp-paint scroll 500 400 down --steps 3
framewisp /tmp/framewisp-paint scroll 500 400 up --steps 3
```

Directions are `up`, `down`, `left`, and `right`. The step count must be a positive
integer and defaults to one. Steps are discrete wheel ticks, not pixels; the
widget under the pointer determines how far content moves. Each step presses and
releases its wheel button. All steps use one connection, with no buttons left
held. There is no smooth scrolling or momentum control.

## Keyboard shortcuts

Use `key` for a key or modifier combination:

```sh
framewisp /tmp/framewisp-demo key Ctrl+a
framewisp /tmp/framewisp-demo type 'Replacement text'
framewisp /tmp/framewisp-demo key Shift+Left
framewisp /tmp/framewisp-demo key Escape
framewisp /tmp/framewisp-paint key Ctrl+z
framewisp /tmp/framewisp-paint key Ctrl+Shift+z
framewisp /tmp/framewisp-paint key Ctrl+s
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
framewisp /tmp/framewisp-demo run --record /tmp/demo.mp4 -- framewisp-demo
```

Recording starts before the app launches. You can also start and stop individual
clips after setting up the app:

```sh
framewisp /tmp/framewisp-demo record-start /tmp/first.mp4
framewisp /tmp/framewisp-demo type 'First demonstration'
framewisp /tmp/framewisp-demo record-stop
# Change the app's state, then record another clip.
framewisp /tmp/framewisp-demo record-start /tmp/second.mp4
framewisp /tmp/framewisp-demo type 'Second demonstration'
framewisp /tmp/framewisp-demo record-stop
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
seconds, the command and its parameters, and its return code or exception. They
record what framewisp attempted and whether the input command completed, not
whether the app responded as intended. A start without an end indicates an
unfinished command. Reusing a session directory starts a fresh log.

Recordings show input captions by default, including shortcuts, pointer gestures,
and typed text. Captions stay visible during paced input and briefly after quick
commands, until the next action. Long text is abbreviated in the video; the log
keeps the full text. Inputs between clips are excluded from the next clip.
Captions appear only in recordings, never in app screenshots.

Disable captions for a clip with:

```sh
framewisp /tmp/framewisp-demo record-start --no-captions /tmp/plain.mp4
# Or start the session with an uncaptioned recording:
framewisp /tmp/framewisp-demo run --record /tmp/plain.mp4 --no-captions -- framewisp-demo
```

Input logging remains enabled. Captions are rendered into the video frames so
GitHub's inline player displays them; viewers cannot toggle them off afterward.
`record-stop` and normal session shutdown wait for caption rendering to finish.
This adds encoding time when stopping a captioned clip. If rendering fails, the
command fails and leaves the uncaptioned MP4 at the requested path; see
`captions.log` for details.

The recorder log retains diagnostics and the first frame's timestamp used for
caption alignment. Framewisp filters the remaining Wayland protocol trace as it
arrives instead of retaining it for every frame.

The Nix package includes FFmpeg and caption fonts for Latin, Greek, Cyrillic, CJK,
and monochrome emoji. The app keeps its own font configuration. Caption text uses
fullwidth equivalents for braces and backslashes to prevent subtitle formatting;
the input log preserves the original characters. Typed text is stored verbatim in
the log and can appear in recordings, so review these artifacts before sharing.

## Flatpak game

With the `org.gnome.SwellFoop` Flatpak installed, run:

```sh
framewisp /tmp/framewisp-swell run -- \
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
framewisp /tmp/framewisp-paint run -- \
  flatpak run --socket=wayland --env=QT_QPA_PLATFORM=wayland org.kde.kolourpaint
```

In the tested default layout, select the Rectangle tool, drag across the blank
canvas, and capture the result:

```sh
framewisp /tmp/framewisp-paint click 57 301
framewisp /tmp/framewisp-paint drag 150 130 400 300
framewisp /tmp/framewisp-paint screenshot --delay 0.5 /tmp/rectangle.png
framewisp /tmp/framewisp-paint click 310 50  # Undo
framewisp /tmp/framewisp-paint screenshot --delay 0.5 /tmp/undone.png
```

Inspect a screenshot first if your toolbar or canvas layout differs. The manual
test used KolourPaint 26.04.3 with KDE runtime 6.10. The gesture sends intermediate positions along a straight path while holding the
left button. By default it takes about 0.4 seconds. Choose a duration for slower
or faster gestures:

```sh
framewisp /tmp/framewisp-paint drag --duration 1.2 150 130 400 300
framewisp /tmp/framewisp-demo type --interval 0.15 'Slower typing'
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
framewisp /tmp/framewisp-x11 run --x11 -- framewisp-demo
# In another terminal:
framewisp /tmp/framewisp-x11 type 'Hello X11! café 日本語 😀'
framewisp /tmp/framewisp-x11 key Return
framewisp /tmp/framewisp-x11 screenshot /tmp/x11.png
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
framewisp /tmp/framewisp-writer type 'café Ελληνικά Русский 日本語 😀'
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
framewisp /tmp/framewisp-writer run -- \
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
It does not contain arbitrary descendants or recover from SIGKILL. If a stale
`session.json` remains after a crash, use a fresh session directory.

GTK currently logs a warning about the missing session bus. The demo works
without it. No private D-Bus service is started.

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

See [DESIGN.md](DESIGN.md) for the implementation and [FOLLOWUPS.md](FOLLOWUPS.md)
for deferred work.

## License

Framewisp is licensed under the GNU General Public License, version 3 only
(`GPL-3.0-only`). See [LICENSE](LICENSE) for the full text.

## Inspect accessible controls

Headless GTK and Qt apps can expose text and state without a screenshot:

```sh
framewisp /tmp/framewisp-demo inspect --json --role button --name 'Apply text'
framewisp /tmp/framewisp-demo inspect --json --role 'text box'
framewisp /tmp/framewisp-demo inspect --json --role label --text 'Applied:'
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
