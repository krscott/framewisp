# framewisp

framewisp lets an agent drive a GUI app on a display of its own or, with the
user's consent, use the user's desktop. This glossary names the concepts that
the code, the CLI, the requirements, and the documentation share.

## Language

### Sessions

**Session**:
One continuous period in which framewisp holds a display, from when `run`,
`attach`, or `teach` starts it until it stops.
_Avoid_: run (as a noun), instance

**Session directory**:
The lasting directory a session works in. It keeps the logs of its most recent
session and is reused by later ones.
_Avoid_: session (for the directory), session path

**Session name**:
The short name a caller gives in place of a session directory's path, scoped by
the project.

**Project**:
The Git worktree, or the working directory outside Git, that scopes session
names.

**Headless session**:
A session on a display that framewisp creates for it, running one app.
_Avoid_: `run` session

**Attached session**:
A session on the user's desktop that can take screenshots and send input.
_Avoid_: attachment, desktop attachment, attached desktop

**Teaching session**:
A session on the user's desktop that can only watch, and that produces a lesson.
_Avoid_: attached session (for teaching)

**App**:
The one program a headless session launches and drives.
_Avoid_: application, target app

**Demo app**:
The small GTK program bundled with framewisp for trying it out and for tests.
_Avoid_: demo (alone)

**Display**:
The area a session can see: a headless session's own, or the one monitor of the
desktop that the user shared.
_Avoid_: output, screen

**Backend**:
The display protocol the app speaks in a headless session: Wayland or X11.
_Avoid_: X11 mode; backend for the processes that provide the display, or for
the kind of session

**Session command**:
A CLI command that asks something of a live session, such as a click, a
screenshot, or a stop. The commands that start a session or recover its
directory are not session commands.
_Avoid_: control request, action (for the name of a session command)

### Desktop sharing

**Desktop**:
The user's own monitors and input devices, as opposed to a headless session's
display.
_Avoid_: host desktop, real desktop

**Desktop share**:
The user's grant of their desktop to one attached or teaching session. It ends
when that session stops.
_Avoid_: attachment, desktop access, desktop sharing (for the grant)

**Desktop lock**:
What limits a user to one desktop share at a time.
_Avoid_: desktop lease, desktop reservation

**Sharing indicator**:
The tray item that shows a desktop share is active and can end it.
_Avoid_: tray item, tray icon

### Input and batches

**Action**:
One input sent to the display: a pointer move, click, drag, scroll, typed text,
or key.
_Avoid_: action (for a check, or for the name of a session command)

**Batch**:
An ordered list of steps that runs as one unit, with no other input in between.

**Step**:
One entry in a batch: an action or a check.
_Avoid_: action (for any batch entry), entry

**Control**:
One element of the app's interface that the app exposes to accessibility tools,
such as a button or a text field.
_Avoid_: control (for a session command or the channel that carries it)

**Check**:
A step that verifies, or waits for, the state of one control.
_Avoid_: conditional check, conditional wait

**Condition**:
What a check requires of one control.

**Inspection**:
Querying the app's controls in a headless session. Each query yields one
observation.

**Observation**:
The result of one inspection query: its status and the controls that matched.
Only a complete observation shows that the app exposes no matching control.
_Avoid_: snapshot, response; observation for a check's limits or a lesson's
frames

**Input log**:
A session's record of the actions it sent.

**Private input**:
Typed text or a key that the caller marked so that it gets no caption and the
input log omits its value.

### Recording

**Screenshot**:
One PNG of the display at a moment.
_Avoid_: capture

**Recording**:
The activity of capturing a headless session's display as video.
_Avoid_: recording (for the resulting file)

**Clip**:
One video file of a headless session, from when recording starts to when it
stops.
_Avoid_: recording (for the file), video

**Clip destination**:
The path where the caller asked for a clip to appear.
_Avoid_: output path, requested path

**Finalization**:
Turning what was recorded into the finished clip at its destination.

**Overlay**:
Anything framewisp draws over the display area of a clip. The kinds are
caption, click ping, and hold disc.
_Avoid_: captions (for all overlays)

**Caption**:
An overlay of text that names an action.

**Click ping**:
An overlay ring that marks where a pointer button was pressed.
_Avoid_: ping (alone), ring

**Hold disc**:
An overlay disc that follows the pointer while a drag holds its button.
_Avoid_: disc

**Console panel**:
A strip beside the display area of a clip that shows the app's output. It is
not an overlay.
_Avoid_: console, panel

**Compaction**:
Trimming idle gaps from a clip. Actions, checks, and batch screenshots are kept
whole.

### Teaching

**Lesson**:
The directory a teaching session produces: a video of the user performing a
task, frames, the goal, and notes.
_Avoid_: demonstration, bundle, teaching recording, evidence

**Frame**:
One still of the display that a teaching session sampled and saved in a lesson.
Elsewhere, "frame" keeps its usual meaning of one image in a video or capture
stream.
_Avoid_: observation, image

### Lifetime

**Runner**:
The foreground process whose lifetime is the session.
_Avoid_: owner, attach process, lifetime owner

**Managed process**:
A process of a headless session that must not outlive it, such as the app or a
process that provides its display.

**Supervisor**:
A helper that ends one managed process and all its descendants, even when the
runner has died.
_Avoid_: subreaper (as its name), owner

**Runtime directory**:
A headless session's private temporary directory, removed by a normal stop or
by recovery.
_Avoid_: runtime (alone)

**Session metadata**:
The description of a live session that a session command reads to reach it. A
normal stop removes it.
_Avoid_: state, session JSON

**Session lock**:
What shows that a session directory is in use, by a session or by supervisors
that outlived one.
_Avoid_: lease, ownership lock, metadata lock

**Recovery**:
Making a session directory usable again after the runner of a headless session
died.

**Recovery journal**:
The record a headless session keeps so that recovery can tell which runtime
directory was its own.
_Avoid_: ownership journal, ownership files
