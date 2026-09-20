# framewisp

Use framewisp to smoke-test GUI interactions, investigate visual bugs, or record
a short demonstration using screenshots and mouse/keyboard input.

Prefer `framewisp` on PATH. Otherwise replace `framewisp` in the examples with
`nix run github:krscott/framewisp --`. No checkout or development shell is needed.
The package includes `framewisp-demo`; other target apps must be installed separately.

## Command reference

Use `framewisp SESSION COMMAND ...`, where SESSION is a directory such as
`/tmp/framewisp-demo`. In the syntax below, brackets mark optional arguments;
uppercase names are placeholders. Put all `run` options before `-- APP ...`.

| Command syntax | Behavior and defaults |
| --- | --- |
| `run [--x11] [--width W] [--height H] [--record FILE] [--no-captions] -- APP [ARGS...]` | Start an isolated app and stay in the foreground. Default display: Wayland, 1280x720. `--x11` uses private Xwayland. Width and height must be positive integers. `--record` starts an MP4 before launching the app. |
| `screenshot [--delay SECONDS] PATH` | Save the current display as a PNG. Default delay: 0. |
| `move X Y` | Move immediately without pressing a button; useful for hover tooltips. |
| `click X Y [--button BUTTON] [--count N] [--modifier MOD]...` | Move and click. BUTTON: `left` (default) or `right`. N: `1` (default) or `2`, with 0.1 seconds between clicks. |
| `drag X1 Y1 X2 Y2 [--button BUTTON] [--duration SECONDS] [--modifier MOD]...` | Move immediately to the start, then drag along a straight line. BUTTON: `left` (default) or `right`. Default duration: 0.4 seconds. |
| `scroll X Y DIRECTION [--steps N]` | Send wheel ticks to the pane under X,Y. DIRECTION: `up`, `down`, `left`, or `right`. N is a positive integer, default 1; ticks are not pixels. |
| `type [--interval SECONDS] TEXT` | Type printable Unicode into the focused control. Default interval: 0.08 seconds between characters. Quote TEXT as one shell argument; use `type -- '-text'` for text starting with a hyphen. |
| `key CHORD` | Press and release a key or shortcut, such as `Return`, `Ctrl+a`, or `Ctrl+Shift+z`. Supported keys and modifiers are listed below. |
| `record-start [--no-captions] FILE` | Start one MP4 clip in an existing headless session; return when capture is ready. Use a new filename. |
| `record-stop` | Finalize the active clip, including one started with `run --record`, and leave the app running. Wait for this command to finish before using the MP4. |
| `status` | Query the live headless runner. Print JSON with backend, display dimensions, app PID/running state, and active recording path/PID/running state (or null). |
| `stop` | Stop a headless session, finalize any recording, and wait for managed processes and runtime sockets to be cleaned up. Print JSON with stopped status and the last recording summary, if any. |
| `attach` | User-only: share an existing Wayland desktop through its permission dialog. See the attachment instructions below. |

Standalone commands take no SESSION: `framewisp --detach` stops the active
desktop attachment, leaving the user's apps running; `framewisp --agent-skill`
prints this guide. `framewisp --help` and `framewisp SESSION COMMAND --help`
(also `-h`) show the installed version's help.

Coordinates are integer pixels in the screenshot, with `(0, 0)` at the top left.
Inspect the screenshot to choose targets. Keyboard input goes to the focused
control; click it first when needed. Commands release their keys and buttons
before returning, so modifiers do not carry over to the next command.

For `click` and `drag`, MOD is `ctrl`, `shift`, or `alt`, case-insensitive.
Repeat `--modifier` for combinations, for example `--modifier ctrl --modifier shift`;
each modifier may appear once. For `key`, join modifiers and a key with `+`.
Supported keys are `a` through `z`, `0` through `9`, `Space`, `Return`, `Tab`,
`BackSpace`, `Escape`, `Delete`, `Left`, `Right`, `Up`, and `Down`.
Names are case-insensitive; `A` does not imply Shift. Use `Shift+a` for that chord.
`type` rejects newlines, tabs, and other nonprintable characters; send `key Return`
or `key Tab` separately. X11 sessions support 128 distinct non-ASCII characters
across text commands; start a fresh session if that limit is reached.

All timing values are finite, nonnegative seconds, including fractions. Zero
disables the requested delay. Input completion does not mean animation completion;
use `screenshot --delay 0.5 PATH` when the app needs time to respond.

## Example workflow

Start an isolated demo in a terminal or background tool session. Use a fresh
session directory, wait for `Session ready:`, and keep the runner alive. The app
may still be drawing its first frame:

```sh
framewisp /tmp/framewisp-demo run -- framewisp-demo
```

From another command session, capture and inspect the PNG, then interact. These
coordinates target the bundled demo's text field:

```sh
framewisp /tmp/framewisp-demo screenshot /tmp/before.png
framewisp /tmp/framewisp-demo click 120 100
framewisp /tmp/framewisp-demo type 'Hello GUI!'
framewisp /tmp/framewisp-demo key Return
framewisp /tmp/framewisp-demo screenshot --delay 0.3 /tmp/after.png
```

Inspect the result before choosing the next action. Use
`framewisp /tmp/framewisp-demo status` to check the session and
`framewisp /tmp/framewisp-demo stop` when finished. Ctrl+C or SIGTERM to the runner
also stops it. `status` and `stop` require a connected headless session;
`--detach` is for desktop attachments only.

## Recording

To record just a demonstration, start recording after setup, perform the inputs,
then stop recording. Use a new output filename:

```sh
framewisp /tmp/framewisp-demo record-start /tmp/demo.mp4
# Perform the interactions to demonstrate.
framewisp /tmp/framewisp-demo record-stop
```

Recording works only in headless sessions, requires even display dimensions,
and produces silent MP4 video. Only one clip may be active. Normal runner
shutdown also finalizes it. Input captions are embedded by default;
`--no-captions` disables them for that clip. Every input is still logged to
`SESSION/inputs.jsonl`, including full typed text. Review logs and videos for
sensitive content before sharing them. Caption rendering adds time to stopping.
On success, `record-stop` prints a JSON summary with `path`, `duration_seconds`,
`width`, `height`, and `size_bytes`, measured from the finished MP4.

## Desktop attachment

For an existing desktop, ask the user to run `framewisp SESSION attach` in their
own foreground terminal and follow its instructions. Never start attachment
yourself or allocate a terminal to bypass its user confirmation. The user must
configure stop shortcuts, type `ATTACH`, approve the desktop dialog, and keep
the terminal open. Wait for `Attached:` before sending commands.

The stop command is `framewisp --detach`, independent of SESSION. The documented
COSMIC bindings are Ctrl+Alt+Escape and Ctrl+Alt+Shift+Escape, so the shortcut also
works during a Shift gesture. Attachment shares the real pointer and keyboard;
input can reach any focused app, including one outside the captured monitor.
Screenshots cover the selected monitor. Recording attached sessions is unsupported.

## Problems

Inspect the session's `app.log`, `sway.log`, and `wayvnc.log` for headless launch
failures, or `recorder.log` and `captions.log` for recording failures.
The runner and every screenshot/input/control invocation need access to the
private display sockets. Launching outside a sandbox does not grant later
sandboxed commands access. If the environment denies socket access, use its
normal permission process for both launch and control commands. Connection
errors can also mean the session exited; check the error and logs first.
If something goes wrong, ask the user for permission before filing an issue at
https://github.com/krscott/framewisp/issues. Agree on the report and any logs or
screenshots to include before submitting it.
