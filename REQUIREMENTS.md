# framewisp requirements

Each requirement is an observable behaviour or invariant that framewisp must
keep. `DESIGN.md` describes how the requirements are met.

## Format

Write one requirement per bullet, as `- **R-AREA-N** [tag] Statement.` State
observable behaviour, not implementation, in short sentences.

- The ID is stable. Never renumber or reuse an ID. To drop a requirement, keep
  its bullet and change its tag to `[retired]`.
- `[test]`: a test that the verify recipe runs covers it. Each covering test
  carries the ID in a comment.
- `[manual: how]`: no automated test yet. The text says how an agent checks it.
- `[retired]`: no longer required. Kept so the ID stays taken.

## Session naming and directories (SESSION)

- **R-SESSION-1** [test] A bare session name resolves to the same session
  directory from every subdirectory and symlink alias of one Git worktree.
- **R-SESSION-2** [test] Separate Git worktrees resolve one bare name to
  separate session directories.
- **R-SESSION-3** [test] A bare name never resolves to a local or legacy
  session directory.
- **R-SESSION-4** [test] The CLI rejects a bare name that is empty, contains
  NUL, is `.` or `..`, or is longer than 255 bytes.
- **R-SESSION-5** [test] Project session directories and runtime directories
  are under `TMPDIR`.
- **R-SESSION-6** [test] `run` refuses a session directory that the caller
  does not own or that group or others can write. It creates no file in that
  directory.

## Target app environment (ENV)

- **R-ENV-1** [test] The target app receives the caller's environment with the
  private session settings applied. Variables from framewisp's Nix wrapper do
  not reach the app.
- **R-ENV-2** [test] The caller's Python settings, such as `PYTHONPATH`,
  `PYTHONHOME`, and `sitecustomize`, do not change framewisp's own startup.
- **R-ENV-3** [test] The target app receives the caller's `PYTHONPATH`
  unchanged.

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
  partial. It never produces an `ok` result without that match.
- **R-INSPECT-5** [test] When the app stalls a call, `inspect` reports
  `app-unresponsive`, separately from budget limits.
- **R-INSPECT-6** [test] When the app is unresponsive, `inspect` returns within
  its time budget and runner `status` still responds.

## Input logs and captions (LOG)

- **R-LOG-1** [test] By default, `inputs.jsonl` and captions omit typed text
  and literal key characters.
- **R-LOG-2** [test] Captions appear only in recordings, never in screenshots.

## Recording (REC)

- **R-REC-1** [test] Recordings and captures never overwrite an existing file.
- **R-REC-2** [test] Recording rejects an odd display width or height.
- **R-REC-3** [test] An active recording is finalized as a playable MP4 when
  the app exits, on SIGINT, on SIGTERM, and on `stop`.
- **R-REC-4** [test] Recorded colours match screenshot colours.

## Lifetime and recovery (LIFE)

- **R-LIFE-1** [test] Stopping a session ends every app descendant, including
  detached and double-forked descendants.
- **R-LIFE-2** [test] After a normal stop, no session process, private socket,
  or runtime directory remains.
- **R-LIFE-3** [test] After the app exits, later session commands report its
  exit code and the `app.log` path.
- **R-LIFE-4** [test] After the runner is killed, `recover` makes the session
  directory usable again.
- **R-LIFE-5** [test] Recovery refuses copied, tampered, or symlinked ownership
  files and replaced runtime directories.
- **R-LIFE-6** [test] Session metadata copied from another session cannot
  control that session.

## Attached desktops (ATTACH)

- **R-ATTACH-1** [test] `attach` starts only from a foreground interactive
  terminal after the user types `ATTACH` exactly.
- **R-ATTACH-2** [test] `attach` refuses a desktop runtime directory that group
  or others can access.
