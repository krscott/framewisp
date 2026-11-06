# framewisp requirements

Each requirement is an observable behaviour or invariant that framewisp must
keep. `DESIGN.md` describes how the requirements are met.

## Format

Write one requirement per bullet, as `- **R-AREA-N** [tag] Statement.` State
observable behaviour, not implementation, in short sentences.

- The ID is stable. Never renumber or reuse an ID. To drop a requirement, keep
  its bullet and change its tag to `[retired]`.
- `[test]`: a test that `python -m pytest` or `nix flake check` runs covers it.
  Each covering test carries the ID in a comment.
- `[manual: how]`: no automated test yet. The text says how an agent checks it.
- `[retired]`: no longer required. Kept so the ID stays taken.

## Session naming and directories (SESSION)

- **R-SESSION-1** [test] A bare session name resolves to the same session
  directory from every subdirectory and symlink alias of one Git worktree.
- **R-SESSION-2** [test] Separate Git worktrees resolve one bare name to
  separate session directories.
- **R-SESSION-3** [test] A bare name never resolves to a local or legacy
  session directory.
- **R-SESSION-4** [test] Framewisp rejects a bare name that is empty, contains
  NUL, is `.` or `..`, or is longer than 255 bytes.
- **R-SESSION-5** [test] When `TMPDIR` names a writable directory, new project
  session directories and runtime directories are under it.
- **R-SESSION-6** [test] `run` refuses a session directory that the caller
  does not own or that group or others can write. It creates no file in that
  directory.

## Target app environment (ENV)

- **R-ENV-1** [test] The target app receives the caller's environment with the
  private session settings applied. Variables from framewisp's Nix wrapper do
  not reach the app.
- **R-ENV-2** [test] The caller's Python settings, such as `PYTHONPATH`,
  `PYTHONHOME`, and `sitecustomize`, do not change how the installed Nix package
  starts.
- **R-ENV-3** [test] The target app receives the caller's `PYTHONPATH`
  unchanged.

## Display (DISPLAY)

- **R-DISPLAY-1** [manual: In `nix develop`, pin a shell to two CPUs with
  `taskset -c 0,1`. In it, start `stress-ng --cpu 8` from
  `nix shell nixpkgs#stress-ng`, then run 300
  `framewisp SESSION run --x11 -- python tests/input_probe.py` sessions, four at
  a time. After `app.log` shows `"event": "ready"`, read `runtime_directory`
  from `session.json` as RUNTIME and run
  `SWAYSOCK=$(ls RUNTIME/sway-ipc.*.sock) swaymsg -t get_tree`. Every session
  must list a window named `Input probe` within 10 seconds.] An X11 app window
  always appears in Sway, even when the app never redraws after its first frame.

## Input (INPUT)

- **R-INPUT-1** [test] The app's seat keeps one pointer and one keyboard
  between CLI commands.
- **R-INPUT-2** [test] `key` sends punctuation shortcuts, such as `Ctrl+,`,
  with exactly the requested modifiers.
- **R-INPUT-3** [test] The CLI and the runner reject invalid command arguments
  before they send any input.
- **R-INPUT-4** [test] A rejected input command does not affect later input.
- **R-INPUT-5** [test] A cancelled, disconnected, or stopped command releases
  its held keys and buttons.
- **R-INPUT-6** [test] Clipboard copy and paste in the app work across input
  commands.

## Batches (BATCH)

- **R-BATCH-1** [test] A batch validates all its actions and its capture
  before it sends any input.
- **R-BATCH-2** [test] No other input runs during a batch, including during
  its final capture.

## Conditional checks (CHECK)

- **R-CHECK-1** [test] A check passes only on a complete observation with
  exactly one readable match. A partial observation never passes a check.
- **R-CHECK-2** [test] A transition wait runs only after its baseline observed
  a nonmatching value.
- **R-CHECK-3** [test] Input completion and PNG capture alone never set a
  batch's `verified` to true.
- **R-CHECK-4** [test] With observation overrides, checks verify controls
  deeper than 32 levels in the accessibility tree.

## Inspection (INSPECT)

- **R-INSPECT-1** [test] `inspect` uses only the session's private buses and
  Sway socket. It never uses the caller's desktop bus or `SWAYSOCK`.
- **R-INSPECT-2** [test] A pointer action at the centre of a control's
  `display_bounds` activates that control. This includes controls in offset
  windows and embedded dialogs.
- **R-INSPECT-3** [test] When the window mapping or geometry is uncertain,
  `display_bounds` is null and `display_bounds_reason` gives the cause.
- **R-INSPECT-4** [test] A vanished or defunct object makes an observation
  partial. It never yields a complete observation with no match.
- **R-INSPECT-5** [test] When a stalled app call uses most of the query budget,
  `inspect` reports `app-unresponsive`.
- **R-INSPECT-6** [test] An unresponsive app does not block runner `status` or
  `stop`.

## Input logs and captions (LOG)

- **R-LOG-1** [test] By default, `inputs.jsonl` and captions omit typed text
  and literal key characters.
- **R-LOG-2** [test] Captions appear only in recordings, never in screenshots.
- **R-LOG-3** [test] Click pings appear only in recordings, never in screenshots.
- **R-LOG-4** [test] By default, a recording captions only `key` and `type`
  inputs.
- **R-LOG-5** [test] The `--captions` and `--no-captions` options do not change
  `inputs.jsonl`.
- **R-LOG-6** [test] Framewisp rejects `--captions` combined with `--no-captions`
  before it starts a session or a clip.
- **R-LOG-7** [test] An incomplete last line in `inputs.jsonl`, as left by an
  interrupted write, does not fail recording rendering.
- **R-LOG-8** [test] A newline-terminated line in `inputs.jsonl` that is not
  valid JSON fails any recording rendering that reads the log (R-REC-10).

## Recording (REC)

- **R-REC-1** [test] Recording rejects a destination that already exists. A
  recording or batch capture never replaces an existing file, including one
  created after the destination was checked.
- **R-REC-2** [test] Recording rejects an odd display width or height.
- **R-REC-3** [test] An active recording is finalized as a playable MP4 when
  the app exits, on SIGINT, on SIGTERM, and on `stop`.
- **R-REC-4** [test] Recorded colours match screenshot colours.
- **R-REC-5** [test] By default, a recording shows a click ping at each press
  of a `click` and at each press and release of a completed `drag`. Each press of
  a double click has its own ping. `scroll` and `move` show no click ping.
- **R-REC-6** [test] A click ping is centred on the pointer position of its
  event. It starts within 0.05 seconds of that event.
- **R-REC-7** [test] `--no-click-pings` removes click pings from that clip only.
- **R-REC-8** [test] In a compacted clip, a click ping stays at the retained
  position of its event. It keeps its full duration unless the clip ends first.
- **R-REC-9** [test] With `--console`, a click ping keeps its GUI position. It
  never draws on the console panel.
- **R-REC-10** [test] When recording rendering fails, a playable MP4 of the raw
  capture remains at the requested path. The error names that path and
  `captions.log`.
- **R-REC-11** [test] `--no-captions` does not change click pings.
- **R-REC-12** [test] `--console-position` places the console panel on the left,
  right, top, or bottom edge of the video. The default is right.
- **R-REC-13** [test] `--console-size` sets the panel width for `left` and
  `right`, and the panel height for `top` and `bottom`. The default is 640
  pixels.
- **R-REC-14** [test] A console panel does not scale or crop the GUI in the
  video. It does not change input coordinates or screenshots.
- **R-REC-15** [test] With `--console`, a caption stays centred on the GUI. It
  never draws on the console panel.
- **R-REC-16** [test] Framewisp rejects a console panel that it cannot encode or
  that cannot show readable text, before it starts a clip.
- **R-REC-17** [test] A rejected console panel leaves the app running.
- **R-REC-18** [test] When a file takes a recording's destination before the
  clip is finalized, the finished clip remains at another path, and the error
  names that path.
- **R-REC-19** [test] Framewisp rejects a recording on a filesystem without hard
  links before it starts the clip, and the error names that limit.
- **R-REC-20** [test] After the runner is killed during a clip, `recover` reports
  the path of the interrupted clip. That clip does not block later recordings.

## Lifetime and recovery (LIFE)

- **R-LIFE-1** [test] Stopping a session ends every app descendant, including
  detached and double-forked descendants.
- **R-LIFE-2** [test] After a normal stop, no session process, private socket,
  or runtime directory remains.
- **R-LIFE-3** [test] After the app exits, later session commands report its
  exit code and the `app.log` path.
- **R-LIFE-4** [test] After the runner is killed, `recover` makes the session
  directory usable again.
- **R-LIFE-5** [test] Recovery refuses copied, tampered, symlinked, or
  replaced ownership files and runtime directories.
- **R-LIFE-6** [test] Session metadata copied to another directory cannot
  control the original session.
- **R-LIFE-7** [test] When recovery cannot verify ownership, it removes nothing
  and its error gives manual cleanup steps.

## Attached desktops (ATTACH)

- **R-ATTACH-1** [test] `attach` starts only from a foreground interactive
  terminal after the user types `ATTACH` exactly.
- **R-ATTACH-2** [test] `attach` refuses a desktop runtime directory that is
  not private or not owned by the user.

## Control requests (CONTROL)

- **R-CONTROL-1** [test] A malformed control request gets an error reply.
- **R-CONTROL-2** [test] A malformed control request leaves the session and its
  app running.
