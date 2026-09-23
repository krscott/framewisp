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
| `screenshot [--delay SECONDS] [--region X Y WIDTH HEIGHT] [--json] PATH` | Save a full-resolution PNG. Crops and JSON metadata are headless-only. Default delay: 0. |
| `inspect --json [--role ROLE] [--name TEXT] [--text TEXT] [--max-depth N] [--limit N] [--max-nodes N] [--timeout SECONDS]` | Read accessible controls in a headless session. Defaults/maxima: depth 8/32, results 20/100, nodes 256/4096, duration 2/10 seconds. All limits must be positive. |
| `batch --file FILE` | Execute a JSON sequence in a headless session, optionally capture a PNG, and print ordered results and timing. |
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

Input coordinates are integer display pixels, with `(0, 0)` at the top left.
Use full screenshots for discovery or when surrounding context matters. When
the target region is known, use `screenshot --region 100 80 400 160 --json /tmp/crop.png`.
The region must fit inside the display and have positive dimensions. JSON reports
`path`, crop `x`, `y`, `width`, `height`, `display_width`, `display_height`, and
`capture_seconds` (excluding delay and CLI startup). Add the crop origin to local
image coordinates before sending input: local `(20, 30)` in this crop means
`click 120 110`. Cropping does not resize pixels or change input coordinates.
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

Timing values are finite seconds, including fractions. Input/capture delays may
be zero; inspection requires a positive timeout. Input completion does not mean animation completion;
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

## Accessible text and state

For supported headless apps, use a bounded query to discover controls or read
state without interpreting an image:

```sh
framewisp /tmp/framewisp-demo inspect --json --role button --name 'Apply text'
framewisp /tmp/framewisp-demo inspect --json --role 'text box'
framewisp /tmp/framewisp-demo inspect --json --role label --text 'Applied:'
```

Role is a case-insensitive exact toolkit name. Name/text are case-insensitive
substrings; filters combine with AND. GTK demo roles include `button`, `text box`,
`checkbox`, and `slider`. Qt may use different names.

Read JSON even when the command exits 1. Only `status: "ok"` exits zero. A complete
empty result means no match in the exposed tree. `partial` reports traversal or
text limits and failed objects; `timeout` reports an exhausted budget;
`unsupported` means no app has registered yet; `unavailable` means the bus or
registry could not be reached. Startup can temporarily appear unsupported.
Do not treat incomplete results as proof of absence. Narrow filters or raise
explicit limits when appropriate; use a screenshot when accessibility is missing.

Check all matches. A button and its child label can share a name; add a role
filter instead of choosing the first. IDs are scoped to that snapshot and cannot
be reused. Re-query after input. Reads are fresh but not atomic. State flags and
listed actions are what the app advertises, not proof that an action will succeed.
GTK may expose `sensitive` without `enabled` for an enabled control.

Bounds use window-relative toolkit coordinates. Use a screenshot for pointer
targets unless the window-to-screenshot transform is known. Native GTK demo
controls work on Wayland and Xwayland. Qt Quick buttons work, but the tested
separate popup menu does not appear in its accessible tree. KolourPaint Flatpak
menus expose names/state, but its canvas still needs screenshots. Keep images
for appearance, layout, custom drawing, and omitted controls. Inspection is
unavailable on attached desktops and does not supply semantic clicks.

## Batch known actions

When targets are already known, use one batch for the inputs and final capture.
Save this JSON to `check.json`, then run
`framewisp /tmp/framewisp-demo batch --file check.json`:

```json
{
  "actions": [
    {"action": "click", "x": 120, "y": 100},
    {"action": "type", "text": "HelloGUI", "interval": 0},
    {"action": "key", "chord": "Return"}
  ],
  "capture": {"path": "/tmp/result.png", "delay": 0.1}
}
```

Batch supports `move`, `click`, `drag`, `scroll`, `type`, `key`, and the checks
described below on headless
Wayland and Xwayland. Parameters use the CLI names without `--`; `modifier` is
an array of lowercase names, such as `["ctrl", "shift"]`. Defaults match the
individual commands. Set `interval: 0` for unpaced checks; omit it for the usual
0.08 seconds between characters. Explicit pacing and capture delay stay in effect.

The whole request is validated before input. Unknown fields are errors. Limits
per batch are 256 actions, 16,384 typed characters, 10,000 scroll steps, and 300
seconds of requested typing/drag/capture pacing and check deadlines. The file and serialized request
must each fit in 1 MiB. At least one action is required. Capture is optional;
its delay defaults to zero. Relative capture paths use the CLI's working directory.
Use a new PNG filename in an existing directory; session files and existing files
are rejected. Recording, lifecycle commands, and loops cannot go inside a batch. Attached desktops do not support batches.

The runner serializes the whole batch, including capture, with other input jobs.
JSON on stdout includes `status`, `results` (zero-based index, action, status,
error, duration), `completed_actions`, `failed_index`, `failed_phase`,
`duration_seconds`, `capture_seconds`, `artifacts` (absolute PNG paths), and `error`.
Per-action durations use the key `duration_seconds`. Times exclude queue wait;
capture time includes its requested delay. Exit code is 0 for completion, 1 for
runtime/session failure, or 2 for invalid CLI/file input. Rejection before execution
may report only an error on stderr, without result JSON.

Execution stops at the first runtime failure. Inspect completed results and
`failed_index`; the failed action may itself have sent some input. Capture failure
has `failed_phase: "capture"` and a null failed index. No action is rolled back or
retried. If the connection dies before a reply, the outcome may be partial: inspect
the app and `inputs.jsonl` before deciding what to do next. Disconnecting cancels
queued work or the remaining running batch and releases held keys/buttons. Session
stop does the same. The completed inputs remain in the app.

A successful capture is an artifact, not proof that the app finished processing.
Inspect it or check an app-specific acknowledgement before claiming success.
Each input retains its own log records and recording captions.

Before an X11 batch sends input, the worker checks all its Unicode characters
against the session's 128-character mapping limit, including mappings allocated
by earlier queued jobs. A capacity failure returns no action results, zero completed
actions, `failed_phase: "validation"`, and the index that exceeds the limit.

## Verify known outcomes

When the expected accessible result is known, include a `wait` after input instead
of choosing a fixed sleep and interpreting a screenshot. Use `assert` for a single
observation. Both require an explicit timeout greater than 0 and at most 10 seconds:

```json
{
  "actions": [
    {"action": "type", "text": "HelloGUI", "interval": 0},
    {"action": "key", "chord": "Return"},
    {"action": "wait", "timeout": 5, "condition": {"role": "label", "name": "Entered:", "field": "text", "equals": "Entered: HelloGUI"}}
  ],
  "failure_capture": {"path": "/tmp/failed.png"}
}
```

Conditions select by role and/or name using inspection's matching rules. Exactly
one match in a complete observation is required. Duplicate matches fail; narrow
the selector rather than selecting the first. Text/name equality is exact and
case-sensitive. `value` compares a finite number. `checked` compares a boolean on
a checkable control; indeterminate is unknown. `enabled` compares a boolean using
either AT-SPI sensitive or enabled. This rule covers the demonstrated GTK/Qt
controls, not every toolkit. Missing or stale objects, unsupported apps, partial
observations, and timeouts cannot satisfy a check. There is no absence assertion.
Wait retries incomplete/missing observations within its deadline; assert does not.
Queries use depth 8, 256 nodes, two matches, and at most two seconds per observation.

A state check may pass immediately on an existing value. To require a transition,
put `{"action": "baseline", "timeout": 2, "condition": ...}` before the input,
then add `"after": INDEX` to the wait, referencing that baseline's zero-based index.
Use the identical condition in both. The baseline must read a unique nonmatching
value, or the batch stops before input. The wait resolves the selector afresh,
allowing a replacement control. Snapshot IDs are diagnostic, not reusable handles.
This proves observed nonmatching then matching state, not input causation.

Read `verified` as well as `status`. True means all requested checks passed; null
means no wait/assert verified app state. Failure stops the batch and sets verified
to false. Each check returns its condition, last observation, observation count,
and duration. `failed_phase: "check"` identifies failed checks. Optional
`failure_capture` uses capture's path/delay schema, runs only on failure, and is
skipped on cancellation. Its errors appear separately in `failure_capture_error`.
Checks retain per-input results and do not create captions. Keep screenshots for
visual-only state and accessibility gaps.

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
