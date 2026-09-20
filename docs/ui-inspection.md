# UI inspection prototype

Issue [#56](https://github.com/krscott/framewisp/issues/56) adds read-only
`inspect --json` observations. It does not add selectors for input, assertions,
or conditional waits. [DESIGN.md](../DESIGN.md#structured-ui-inspection) defines
the response and its bounds.

## Toolkit coverage

Checked on NixOS x86_64, Linux 6.18.52, Python 3.14.7, GTK 4.22.4,
AT-SPI 2.60.6, and the repository's pinned Qt 6. The display is Sway 1.12 with
wlroots 0.20.2 and Pixman at 1280x720, without recording.

| Application/toolkit | Wayland | Private Xwayland | Observations and limits |
| --- | --- | --- | --- |
| Bundled GTK 4 demo | Tested | Tested | Entry/button discovery, changed entry/result text, checkbox state, slider value, action names, and window-relative bounds. |
| Qt Quick QML probe | Tested | Tested | Ordinary button name and state work. The separate `Popup.Window` menu item is absent, even while visible. |
| KolourPaint 26.04.3, Qt Widgets Flatpak | Tested | Not tested for inspection | Menus expose names, states, actions, and bounds. The default 256-node budget is partial; `--max-nodes 1024 --max-depth 16 --timeout 5` traversed 379 objects. Painting content still needs images. |
| GTK Flatpak | Not tested | Not tested | Native GTK coverage does not establish Flatpak coverage. |
| App without AT-SPI (`sleep` test) | Tested | Not applicable | Explicit `unsupported`, not a successful empty search. |
| Other toolkits/custom canvases | Unknown | Unknown | Use screenshots until useful accessible content is demonstrated. |
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

This is enough to prototype positive text/state waits on the demonstrated GTK
controls, provided a later caller re-runs bounded queries, handles ambiguity,
and only accepts complete observations. No persistent object handles are needed.
The serial traversal cost is too high for fast polling as-is. A later waits
implementation should reuse a private query connection and measure its budget
before promising subsecond workflows.

It is not enough for general assertions of widget absence or a toolkit-independent
selector/action API. Omitted Qt popup content, unknown canvas coverage,
non-atomic reads, and different state flags remain real limitations. A wait must
not convert a timeout, partial result, or unsupported app into a successful
negative assertion. Screenshot fallback stays part of the workflow.

Primary references: [AT-SPI Accessible protocol](https://github.com/GNOME/at-spi2-core/blob/main/xml/Accessible.xml),
[Component coordinates](https://github.com/GNOME/at-spi2-core/blob/main/xml/Component.xml),
[GTK accessibility](https://docs.gtk.org/gtk4/section-accessibility.html), and
[Qt's explicit accessibility bus connection](https://github.com/qt/qtbase/blob/dev/src/gui/accessible/linux/dbusconnection.cpp).
