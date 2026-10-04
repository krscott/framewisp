# UI inspection prototype

Issue [#56](https://github.com/krscott/framewisp/issues/56) adds read-only
`inspect --json` observations. Inspection supplies observations for [conditional batch checks](conditional-checks.md).
It does not add selectors for input. [DESIGN.md](../DESIGN.md#structured-ui-inspection) defines
the response and its bounds.

## Toolkit coverage

Checked on NixOS x86_64, Linux 6.18.52, Python 3.14.7, GTK 4.22.4,
AT-SPI 2.60.6, and the repository's pinned Qt 6. The display is Sway 1.12 with
wlroots 0.20.2 and Pixman at 1280x720, without recording.

| Application/toolkit | Wayland | Private Xwayland | Observations and limits |
| --- | --- | --- | --- |
| Bundled GTK 4 demo | Tested | Tested | Entry/button discovery, changed entry/result text, checkbox state, slider value, action names, window-relative bounds, and display bounds. |
| Qt Quick QML probe | Tested | Tested | Ordinary button name and state work. The separate `Popup.Window` menu item is absent, even while visible. |
| KolourPaint 26.04.3, Qt Widgets Flatpak | Tested | Not tested for inspection | Menus expose names, states, actions, and bounds. The default 256-node budget is partial; `--max-nodes 1024 --max-depth 16 --timeout 5` traversed 379 objects. Painting content still needs images. |
| LibreOffice Writer 26.8.1.1, GTK 3 Flatpak | Tested | Tested | Paragraph text verifies an edit with raised traversal budgets. Default queries are partial; display-coordinate conversion is unavailable. See the real-app runs below. |
| Blender 5.2.0 LTS, custom canvas Flatpak | Tested for images/input | Tested for images/input | No accessible app registers. Conditional checks cannot verify visible geometry or console text. See the real-app runs below. |
| App without AT-SPI (`sleep` test) | Tested | Not applicable | Explicit `unsupported`, not a successful empty search. |
| GTK 4 Flatpak and other toolkits/custom canvases | Not tested | Not tested | Writer GTK 3 and Blender coverage does not establish support for these targets. Use screenshots until useful accessible content is demonstrated. |
| Attached desktop | Unsupported | Unsupported | No connection to the user's desktop accessibility bus. |

The matrix describes these applications, not all controls from each toolkit.
An `ok` result means the exposed tree was traversed within the limits. It cannot
detect widgets omitted by a toolkit. For example, the Qt popup search returns
an empty complete result while its window remains visible. The prototype must
not be used to prove that such a popup closed.

State flags remain toolkit-specific. GTK exposes `sensitive` for the tested
enabled button without setting `enabled`; Qt sets both. Action names are
advertised capabilities, not performed actions. GTK exposes label copy/edit
commands even when they are not useful for the current label.

## Verification

Real GUI tests locate the demo's entry/button, type and apply text, read the
result and changed checkbox state, and inspect the slider value without opening
a screenshot. They run on Wayland and Xwayland. Additional tests cover duplicate
names, complete missing matches, all traversal caps, 1024-character truncation,
a paused application (SIGSTOP), fresh queries after state changes, and Qt
coverage. A D-Bus I/O test removes an object between enumeration and its read
and requires a partial response.

Two simultaneous headless sessions start with deliberately conflicting inherited
bus addresses. Inspection stays within each session, and stopping one removes
its buses, registry, children, and sockets while the other remains usable.
An app without accessibility returns unsupported. A real private-bus disconnect test checks cleanup races. Existing app-exit, startup
failure, SIGINT, SIGTERM, and recording tests also exercise the new bus lifetime.

## Timeout diagnostics and Qt tree check

The standalone timeout defaults to 5 seconds. Depth (8) and node count (256)
remain conservative because deeper traversals have crashed some Qt apps. Batch
checks default to 2 seconds per observation. Their optional `observation.timeout`
can raise that budget to 10 seconds, within the overall check deadline; see
[check traversal settings](conditional-checks.md#conditions-and-deadlines).

`tests/inspection_probe.qml` exposes 800 buttons on Qt Quick. On September 30,
2026, the pinned Nix environment on Xwayland traversed 804 objects in 2434 ms
with `--name 'Absent control' --max-nodes 1024`. The longest call took 3.8 ms.
This exceeds the old 2-second timeout. A 0.2-second query returned `timeout`
without `app-unresponsive`; clicking the probe's button blocks its main loop for
3 seconds, and a 0.5-second query reported `app-unresponsive`. Inspection
succeeded again after the main loop resumed. This synthetic tree exercises
hundreds of real Qt accessible objects; it does not reproduce every Qt Widgets
item-view behavior from the issue.

`hints` names limiting flags and their maxima. `longest_call_ms` includes failed
calls; bounded `errors` identify the target and method. The unresponsive reason
requires a failed app call consuming at least 80% of the whole budget and
reaching its deadline. A late stall may therefore report only `timeout`.

## Measurements

The reproducible script is [benchmark_inspection.py](benchmark_inspection.py).
It performs one warm-up per case, then 40 sequential samples against the same
running GTK demo with the inspection implementation at `03efe67`, before
integrating the batch command from #61. These are shared-host measurements;
the end of the regression run and package checks overlapped part of sampling. Setup types `HelloGUI` and applies it before timing. A button
query discovers the Apply control, a label query reads `Applied: HelloGUI`, and
a screenshot contains both. Complete CLI timings include Python/Gio startup,
D-Bus connection, traversal, JSON serialization, and process exit. They exclude
Nix environment startup, application startup, model inference, and tool scheduling.
The raw samples are in the [PR benchmark comment](https://github.com/krscott/framewisp/pull/62#issuecomment-5751650842).

```sh
framewisp /tmp/fw-inspection-bench run -- framewisp-demo
# In another terminal inside nix develop:
python docs/benchmark_inspection.py /tmp/fw-inspection-bench /tmp/inspection.json
framewisp /tmp/fw-inspection-bench stop
```

| Complete CLI operation | p50 | p95 | Median response bytes |
| --- | ---: | ---: | ---: |
| Find Apply button | 570 ms | 624 ms | 516 |
| Read applied result | 750 ms | 900 ms | 642 |
| Capture PNG containing both | 154 ms | 171 ms | 62,670 |

Queries currently read each object through several D-Bus calls. Inspection is
slower than capture in this small demo. It exchanges much less data and lets
an agent read text/state without an image-viewing step. Neither response size
nor these CLI measurements establish a model-latency speedup.

One end-to-end agent trial at revision `bfb5c75` on September 20, 2026 used
Codex (GPT-6), its shell
and image-viewing tools, and the same demo state. The task was to locate the Apply
button and read the applied result. Both paths correctly found `Apply text` and
`Applied: HelloGUI` without retries.

| Agent path | Wall time to interpreted result | CLI calls | Task tool operations | Tool-result/model cycles | Images inspected |
| --- | ---: | ---: | ---: | ---: | ---: |
| Screenshot, then image viewer | 15.456 s | 1 | 3 (launch, completion poll, image view) | 2 | 1 (62,670 bytes) |
| Two filtered queries in one shell call | 15.795 s | 2 | 2 (launch, completion poll) | 2 | 0 (about 1.2 KB JSON) |

Timing starts immediately before the shell tool call and ends at the first
instrumentation call after the model reads the result. It includes Nix shell
startup, sandbox/tool dispatch, completion polling, and model processing. It
excludes session setup and input because this comparison is a discovery/read
task. No separate model-inference or upload timer is available. This is one
illustrative trial, affected by scheduling and prior knowledge of the demo,
not a latency distribution or evidence of an agent speedup. Both paths took
about 15.5 seconds despite the large difference in response size.

## Decision for conditional waits

The [conditional checks](conditional-checks.md) use this coverage for positive
text/state assertions. They re-run bounded queries, reject ambiguity, and only
accept complete observations. No persistent object handles are needed. Each
observation opens and closes a private connection. Traversal dominates larger
trees, so a 50 ms polling pause does not promise 50 ms detection or subsecond
workflows. The small delayed-response probe measures the combined input/check
path separately from the larger demo's traversal cost.

It is not enough for general assertions of widget absence or a toolkit-independent
selector/action API. Omitted Qt popup content, unknown canvas coverage,
non-atomic reads, and different state flags remain real limitations. A wait must
not convert a timeout, partial result, or unsupported app into a successful
negative assertion. Screenshot fallback stays part of the workflow.

Primary references: [AT-SPI Accessible protocol](https://github.com/GNOME/at-spi2-core/blob/main/xml/Accessible.xml),
[Component coordinates](https://github.com/GNOME/at-spi2-core/blob/main/xml/Component.xml),
[GTK accessibility](https://docs.gtk.org/gtk4/section-accessibility.html), and
[Qt's explicit accessibility bus connection](https://github.com/qt/qtbase/blob/dev/src/gui/accessible/linux/dbusconnection.cpp).

## Display bounds

`inspect` retains `bounds` in window coordinates and adds `display_bounds` in
input-command coordinates. The center of the latter is a pointer target without
an image-based offset calculation. `display_bounds_reason` explains null results.
The conversion uses the private compositor's client rectangle, PID, and exact
window title. Ambiguous windows and window changes between the compositor
snapshots produce no target. See [DESIGN.md](../DESIGN.md#structured-ui-inspection)
for geometry checks and race limitations.

`test_inspect_display_bounds_activate_offset_controls` clicks reported centers
for GTK 4 and Qt Quick on native Wayland and private Xwayland. It moves the GTK
window and a Qt modal dialog away from the origin and verifies activation in
each app's log. Xwayland cases also require a server-side title bar offset; native
Wayland clients can negotiate their own decorations. The Qt dialog shares a PID with its
main window. Unit tests cover duplicate titles, absent windows, movement,
resizing, hidden windows, unsupported transforms, and bounded private IPC reads.

## Real-app compatibility runs

Issue [#79](https://github.com/krscott/framewisp/issues/79) extends the matrix
with LibreOffice Writer's GTK 3 Flatpak and Blender's custom canvas. These are
opt-in host tests, separate from the bundled GTK/Qt probes and package checks.
No additional architecture, OS, compositor, or desktop-attachment support is
claimed.

The October 3, 2026 runs used NixOS x86_64, Linux 6.18.54, Python 3.14.7,
Sway 1.12, Xwayland 24.1.13, Flatpak 1.18.4, and 1280x720 private displays. Both apps use
`org.freedesktop.Platform/x86_64/25.08` (freedesktop-sdk 25.08.17).

| Target | App/toolkit version | Flatpak commit |
| --- | --- | --- |
| LibreOffice Writer | 26.8.1.1, GTK 3.24.52 (`SAL_USE_VCLPLUGIN=gtk3`) | `c3110993f45de72735bea42838653772abc561295e447333ff2025b0825e8837` |
| Blender | 5.2.0 LTS, custom UI/GHOST, build `fbe6228777e7`, software OpenGL | `f97247d9e87dca0bc28c6a01e51cd6425cf8b21c636d28514898b7315b21d521` |

The test reads Sway's tree to confirm `xdg_shell` on Wayland and `xwayland` on
private Xwayland. A launch option alone is not backend evidence. It waits for a
rendered window before input: runner readiness and even a mapped window can
precede the first useful frame.

Writer's first-run welcome required three Next clicks despite
`--nofirststartwizard`. The run clicked into the document, typed
`Framewisp compatibility café`, replaced it with `Verified Writer café`, and
verified that exact paragraph through a conditional check. Both backends needed
`max_nodes=4096`, `max_depth=32`, and a 10-second observation/check budget.
Default inspection was partial. Complete observations visited 2,106 nodes on
Wayland and 2,097 on Xwayland; the repeated-run edit checks took about 7 seconds
on this shared host. This is not a latency distribution or a general Writer
responsiveness guarantee.

Writer exposes the document paragraph, but the tested paragraph had no actions
or numeric value. Its window-relative bounds were available; `display_bounds`
was null with `window-not-found`. Use screenshot coordinates for input rather
than assuming Flatpak accessibility objects map to compositor windows. Menus,
formatting controls, file dialogs, multi-paragraph documents, and other
LibreOffice components are untested. Settings-portal warnings did not prevent
this workflow; portal-dependent features remain untested.

Blender's run dismissed the splash, used `g`, `x`, `2`, Return to translate the
selected cube, and sent two upward wheel steps over the viewport. Screenshots
recorded the viewport and editor menu. Selecting Python Console through the
menu and typing a Python expression wrote the actual cube location to a file;
`(2.0, 0.0, 0.0)` verified the translation independently of successful input
return codes. The console also exercises ASCII `type --interval 0.02`. No AT-SPI application
registered on either backend: queries returned `unsupported`, not an `ok`
empty match. An assertion for a visible Cube label failed with
`verified: false`. Accessibility cannot verify the geometry or console output.
Function-key shortcuts such as Shift+F4 are outside framewisp's supported key
vocabulary, so this workflow uses the editor menu. Dragging, Unicode console
input, GPU rendering, saved projects, and other Blender editors are untested.

Two zero-delay Blender console trials on Xwayland left an incomplete Python
expression and produced no output file. Paced typing is required for the tested
workflow; zero-delay console input is not supported by this coverage.

The suite pauses after focus changes and between Blender shortcuts. Before
Unicode replacement, it lets Writer consume Ctrl+A; input command completion
alone does not establish that the app handled the shortcut. A first Wayland
trial without that pause timed out rather than verifying the requested text.
The suite does not promise reliable back-to-back shortcuts and Unicode input
in every app.

### Reproduce

Install the listed Flatpaks separately. The tests do not install apps or change
Flatpak permissions persistently. Run outside an agent sandbox that blocks
Bubblewrap namespaces or private display sockets:

```sh
nix develop
FRAMEWISP_REAL_APPS=1 python -m pytest tests/test_real_apps.py -v \
  --basetemp /tmp/framewisp-real-apps
```

`--basetemp` is pytest-owned and is cleared on reuse. Use a fresh directory to
preserve earlier evidence. Each case saves the exact launch argv, Flatpak info,
Sway tree, command responses, screenshots, check results, and cleanup evidence.
Writer uses a temporary `UserInstallation`; Blender uses temporary
`BLENDER_USER_CONFIG` and factory startup. Flatpak receives filesystem access
only to that case's artifact directory in addition to its installed permissions.
These tests are tied to the listed English UI layouts; inspect the retained
screenshots before adjusting coordinates for another version or locale.

Equivalent foreground launch commands, with an existing private artifact
directory `/tmp/fw-real-app`:

```sh
framewisp /tmp/fw-writer run -- flatpak run \
  --filesystem=/tmp/fw-real-app --socket=wayland --nosocket=x11 \
  --env=SAL_USE_VCLPLUGIN=gtk3 org.libreoffice.LibreOffice \
  -env:UserInstallation=file:///tmp/fw-real-app/writer-profile \
  --writer --norestore --nofirststartwizard

framewisp /tmp/fw-blender run -- flatpak run \
  --filesystem=/tmp/fw-real-app --socket=wayland --nosocket=x11 \
  --env=LIBGL_ALWAYS_SOFTWARE=1 \
  --env=BLENDER_USER_CONFIG=/tmp/fw-real-app/blender-profile \
  org.blender.Blender --factory-startup
```

For private Xwayland, add `run --x11`, replace `--socket=wayland` with
`--socket=x11`, and replace `--nosocket=x11` with `--nosocket=wayland`.
Stop using `framewisp SESSION stop`. Each automated case requires the runner,
managed children, observed app PIDs, and sampled descendants to exit; the private
runtime directory, metadata, and X11 socket must disappear. These checks cover
normal stop for these workflows, not every possible app crash or background job.
