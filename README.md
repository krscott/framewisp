# framewisp

Run a native Wayland app without a physical display, save PNG screenshots or MP4 recordings, and
send clicks, drags, and keystrokes from the CLI. The MVP targets this repo's NixOS
development environment and includes a small GTK demo.

## Stack

Framewisp's Python CLI manages the session and calls these tools:

| Component | Role |
| --- | --- |
| Sway / wlroots | Runs the headless Wayland display with Pixman software rendering. |
| wayvnc | Provides virtual pointer and keyboard devices through a VNC server on a private Unix socket. |
| vncdotool (`vncdo`) | Sends clicks, drags, scrolling, and keyboard input to wayvnc. |
| grim | Captures the Wayland display as a PNG. Screenshots do not use VNC. |
| wf-recorder | Records the display as silent H.264 MP4 when requested, using its FFmpeg libraries. |
| GTK 4, GLib, and PyGObject | Provide the bundled demo and test apps, their event loops, and Python bindings. |

The Nix flake pins and packages these dependencies and wraps the installed
commands with their runtime environment. Target applications, including Flatpaks,
are installed separately.

Development uses pytest and Pillow to run real GUI tests and compare screenshots.
FFmpeg's `ffmpeg` and `ffprobe` commands decode and inspect recordings in tests;
they are supplied by the development shell and package check, not exposed by the
installed framewisp package. Mypy and Pyright check types; Black, isort, and
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

For this private repository, use an SSH flake URL with an authorized GitHub key:

```sh
nix run 'git+ssh://git@github.com/krscott/framewisp' -- \
  --session /tmp/framewisp-demo run -- framewisp-demo
```

Repeat the same `nix run ... --` prefix for input and screenshot commands.
The first invocation builds or downloads the package and its dependencies.

To put `framewisp` and `framewisp-demo` on PATH, add the flake to your NixOS or
Home Manager configuration's inputs:

```nix
inputs.framewisp.url = "git+ssh://git@github.com/krscott/framewisp";
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

## Try it

After installation, in the first terminal:

```sh
framewisp --session /tmp/framewisp-demo run -- framewisp-demo
```

Wait for `Session ready:`. Keep that process running. In a second terminal, run:

```sh
framewisp --session /tmp/framewisp-demo screenshot /tmp/before.png
```

Open the PNG, or have the agent inspect it with its image-viewing tool. The demo
has a text field near `(120, 100)` and an "Apply text" button near `(120, 170)`.

```sh
framewisp --session /tmp/framewisp-demo click 120 100
framewisp --session /tmp/framewisp-demo type 'Hello Wayland!'
framewisp --session /tmp/framewisp-demo key BackSpace
framewisp --session /tmp/framewisp-demo key Return
framewisp --session /tmp/framewisp-demo click 120 170
framewisp --session /tmp/framewisp-demo screenshot /tmp/after.png
```

The final screenshot should show `Hello Wayland` in the field and
`Applied: Hello Wayland` below the button. Stop the runner with Ctrl+C in the
first terminal, or send SIGTERM to the `framewisp ... run` process. An agent can
keep that foreground process running through its shell tool while it makes
separate CLI calls.

The installed package supplies the display and input tools. You can also use
these commands inside `nix develop` when working on the source.

## Commands

Every command takes `--session DIRECTORY` before the subcommand.

| Command | Behavior |
| --- | --- |
| `run [--record FILE] -- APP [ARGS...]` | Start the display, optional recording, and application; stay in the foreground. |
| `screenshot [--delay SECONDS] PATH` | Wait the requested seconds (default: 0), then write a PNG of the 1280 by 720 display. |
| `scroll X Y DIRECTION [--steps N]` | Send wheel steps to the pane at these coordinates; directions: up, down, left, right. |
| `click X Y` | Move the pointer and press/release the left button. Coordinates start at the top left. |
| `drag [--duration SECONDS] X1 Y1 X2 Y2` | Hold the left button while moving along a straight path (default: 0.4 seconds). |
| `type [--interval SECONDS] TEXT` | Send printable ASCII with a pause between characters (default: 0.08 seconds). |
| `key CHORD` | Press/release a key with optional Ctrl, Shift, and Alt modifiers. |

The automated tests cover the bundled native Wayland demo. Swell Foop 50.0 and
KolourPaint 26.04.3 have also been tested manually as Flatpaks (see below). X11 and GPU-dependent apps
are outside this MVP.

`Session ready:` means the display and input sockets exist and the application
process has started. The app may still be drawing its first frame. A screenshot
captures the current display; it does not wait for the app to finish responding
to input automatically. To allow time for an animation, choose a delay before
capture:

```sh
framewisp --session /tmp/framewisp-demo screenshot --delay 0.5 /tmp/after.png
```

The delay accepts finite, nonnegative seconds, including fractions. It defaults
to zero and does not count toward the capture process's ten-second timeout.
Capture again when necessary.

## Scrolling

Move the pointer to the pane you want to scroll, then send wheel steps:

```sh
framewisp --session /tmp/framewisp-paint scroll 500 400 down --steps 3
framewisp --session /tmp/framewisp-paint scroll 500 400 up --steps 3
```

Directions are `up`, `down`, `left`, and `right`. The step count must be a positive
integer and defaults to one. Steps are discrete wheel ticks, not pixels; the
widget under the pointer determines how far content moves. Each step presses and
releases its wheel button. All steps use one connection, with no buttons left
held. There is no smooth scrolling or momentum control.

## Keyboard shortcuts

Use `key` for a key or modifier combination:

```sh
framewisp --session /tmp/framewisp-demo key Ctrl+a
framewisp --session /tmp/framewisp-demo type 'Replacement text'
framewisp --session /tmp/framewisp-demo key Shift+Left
framewisp --session /tmp/framewisp-demo key Escape
framewisp --session /tmp/framewisp-paint key Ctrl+z
framewisp --session /tmp/framewisp-paint key Ctrl+Shift+z
framewisp --session /tmp/framewisp-paint key Ctrl+s
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
framewisp --session /tmp/framewisp-demo run --record /tmp/demo.mp4 -- framewisp-demo
```

Recording starts before the app launches. Continue using the normal input and
screenshot commands. Stop the runner with Ctrl+C or SIGTERM, or close the app,
and wait for the runner to exit before playing the file. It finalizes the video
before stopping the display. The recording uses H.264 at 1280 by 720 and 30 fps.

Choose a new output path in an existing directory; existing files are not
overwritten. Session log and metadata paths are reserved. Recorder startup,
capture, or finalization failures fail the session
and point to `recorder.log`. A forced kill may leave an incomplete MP4. The package supplies
the recorder; recording is disabled unless requested. The development shell also
includes FFmpeg for video inspection.

## Flatpak game

With the `org.gnome.SwellFoop` Flatpak installed, run:

```sh
framewisp --session /tmp/framewisp-swell run -- \
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
framewisp --session /tmp/framewisp-paint run -- \
  flatpak run --socket=wayland --env=QT_QPA_PLATFORM=wayland org.kde.kolourpaint
```

In the tested default layout, select the Rectangle tool, drag across the blank
canvas, and capture the result:

```sh
framewisp --session /tmp/framewisp-paint click 57 301
framewisp --session /tmp/framewisp-paint drag 150 130 400 300
framewisp --session /tmp/framewisp-paint screenshot --delay 0.5 /tmp/rectangle.png
framewisp --session /tmp/framewisp-paint click 310 50  # Undo
framewisp --session /tmp/framewisp-paint screenshot --delay 0.5 /tmp/undone.png
```

Inspect a screenshot first if your toolbar or canvas layout differs. The manual
test used KolourPaint 26.04.3 with KDE runtime 6.10. The gesture sends intermediate positions along a straight path while holding the
left button. By default it takes about 0.4 seconds. Choose a duration for slower
or faster gestures:

```sh
framewisp --session /tmp/framewisp-paint drag --duration 1.2 150 130 400 300
framewisp --session /tmp/framewisp-demo type --interval 0.15 'Slower typing'
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

## Logs and cleanup

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
