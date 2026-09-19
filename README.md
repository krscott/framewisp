# framewisp

Run a native Wayland app without a physical display, save PNG screenshots, and
send clicks and keystrokes from the CLI. The MVP targets this repo's NixOS
development environment and includes a small GTK demo.

## Try it

In the first terminal:

```sh
nix develop
framewisp --session /tmp/framewisp-demo run -- framewisp-demo
```

Wait for `Session ready:`. Keep that process running. In a second terminal,
enter the same repo and run:

```sh
nix develop
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

The Nix development shell supplies the display and input tools. No manual system
package installation or sudo is needed. Its first run downloads dependencies and
creates the Python virtual environment.

## Commands

Every command takes `--session DIRECTORY` before the subcommand.

| Command | Behavior |
| --- | --- |
| `run -- APP [ARGS...]` | Start Sway, wayvnc, and the application; stay in the foreground. |
| `screenshot PATH` | Write a PNG of the 1280 by 720 display. |
| `click X Y` | Move the pointer and press/release the left button. Coordinates start at the top left. |
| `type TEXT` | Send printable ASCII characters to the focused widget. |
| `key NAME` | Press/release `Return`, `Tab`, or `BackSpace`. |

The runner accepts another application command, but only the bundled native
Wayland demo is tested. X11 and GPU-dependent apps are outside this MVP.

`Session ready:` means the display and input sockets exist and the application
process has started. The app may still be drawing its first frame. A screenshot
captures the current display; it does not wait for the app to finish responding
to input. Capture again when necessary.

## Logs and cleanup

The session directory contains `sway.log`, `wayvnc.log`, and `app.log`. During a
run, `session.json` records the private runtime directory, Wayland socket name,
and managed process IDs. Normal shutdown removes the runtime sockets and
`session.json`, and keeps the logs. Reusing the directory replaces its logs.

The runner stops its three managed processes on Ctrl+C, SIGTERM, or application
exit. It returns the application's exit code when the app exits on its own.
It does not contain arbitrary descendants or recover from SIGKILL. If a stale
`session.json` remains after a crash, use a fresh session directory.

GTK currently logs a warning about the missing session bus. The demo works
without it. No private D-Bus service is started.

## Development

Inside `nix develop`:

```sh
python -m pytest
python -m mypy .
python -m pyright
```

Tests run a real compositor, VNC server, and GTK app. They check input through
the demo's output, compare screenshot regions, and verify managed-process cleanup.

After changing Python dependencies or script entry points, refresh the existing
virtual environment with `python -m pip install -e '.[dev]'` inside `nix develop`.

See [DESIGN.md](DESIGN.md) for the implementation and [FOLLOWUPS.md](FOLLOWUPS.md)
for deferred work. The standalone Nix package is not a supported launch method
yet; use the development shell.
