# Checks

A headless batch can send input and verify known accessible state in one request.
Its steps are actions (`"action": ...`) and checks (`"check": ...`).
Checks use the same private AT-SPI tree as `inspect`. The demo app and the test
probe work on Wayland and Xwayland. Toolkit omissions still apply; see
[inspection coverage](ui-inspection.md). Visual-only state needs screenshots.

Save this example as `check.json` and run
`framewisp /tmp/framewisp-demo batch --file check.json` after starting the demo app:

```json
{
  "steps": [
    {"action": "click", "x": 120, "y": 100},
    {"action": "key", "chord": "Ctrl+a"},
    {"action": "type", "text": "HelloGUI", "interval": 0},
    {"action": "key", "chord": "Return"},
    {
      "check": "wait", "timeout": 5,
      "condition": {"role": "label", "name": "Entered:", "field": "text", "equals": "Entered: HelloGUI"}
    },
    {
      "check": "assert", "timeout": 2,
      "condition": {"role": "text box", "field": "text", "equals": "HelloGUI"}
    }
  ],
  "failure_screenshot": {"path": "/tmp/check-failed.png"}
}
```

## Conditions and deadlines

Each check requires `condition` and a finite `timeout` greater than zero and at
most 10 seconds. `wait` observes immediately, then polls until success or its
monotonic deadline. It pauses up to 50 ms between observations; traversal adds to
that interval. `assert` makes one observation within the deadline and fails if
it cannot establish the condition. Neither retries input. Deadlines begin when
the worker reaches the step, excluding queue time. Requested check deadlines
count toward the batch's shared 300-second pacing/deadline limit.

A condition selects by `role` and/or `name`, with the same case-insensitive exact
role and name substring rules as inspection. Only a complete observation with
exactly one readable match can satisfy it (R-CHECK-1). Duplicate matches fail immediately, even if one has
the desired value. Missing objects, stale references, truncation, unsupported
apps, and timeouts never count as success. A wait can retry those observations;
an unavailable bus fails immediately. There is no absence condition and no
observation-ID selector.

| Field | Equality semantics |
| --- | --- |
| `text` | Exact, case-sensitive accessible text. Missing Text interface is unknown. |
| `name` | Exact, case-sensitive accessible name. |
| `value` | Exact finite numeric CurrentValue. Missing Value interface is unknown. |
| `checked` | Boolean checked flag on a checkable role or object with the checkable flag. Indeterminate state is unknown. |
| `enabled` | Boolean: either sensitive or enabled AT-SPI flag. This accommodates the demonstrated GTK and Qt controls, but does not prove that input will succeed. |

Role/name strings must be nonempty and at most 1024 characters. Expected text/name
can be empty and is capped at 1024 characters. Every check accepts an optional
`limits` object, separate from its `condition`, that bounds each inspection query:

| Limit | Default | Allowed values |
| --- | ---: | --- |
| `max_nodes` | 256 | Integer, 1 to 4096 |
| `max_depth` | 8 | Integer, 1 to 64 |
| `timeout` | 2 seconds | Finite number, greater than 0 and at most 10 seconds |

Unknown settings, booleans in numeric fields, and out-of-range values fail batch
validation before any input. Omitted fields keep their defaults. The match limit
stays fixed at two, sufficient to reject ambiguity. Each query gets the
smaller of `limits.timeout` and the remaining step deadline. The outer
`timeout` covers all queries and polling pauses, even when the query
budget is larger. Query budgets do not add to the batch's 300-second limit;
the enclosing check deadlines already bound that work.

For a larger/deeper accessible tree, use explicit limits on each needed step:

```json
{
  "steps": [
    {
      "check": "assert", "timeout": 5,
      "limits": {"max_nodes": 1024, "max_depth": 16, "timeout": 5},
      "condition": {"role": "label", "name": "Result", "field": "text", "equals": "Done"}
    }
  ]
}
```

Incomplete observations still cannot verify a condition. Failure messages name
`limits.max_nodes` or `limits.max_depth` when those caps prevent a
complete traversal. A longer check timeout alone does not fix those caps. Query
timeouts identify both time budgets; stalled app calls recommend retrying or
taking a screenshot. At hard traversal maxima, screenshots remain the fallback.

If any branch exceeds the configured depth, even a shallow match cannot verify
a check. For deeply nested apps, set `"limits": {"max_depth": 64}` on each
needed check. `inspect --max-depth 64` uses the same depth limit. Node and time
budgets still apply.

## States and transitions

Ordinary `wait` and `assert` check state. A value that already matches is valid.
To require a transition, insert a `baseline` check before the triggering input,
then set the wait's `after` to that baseline's zero-based step index. Both
steps must have the identical condition. Their limits and deadlines
may differ; they change traversal budgets, not selector or equality semantics:

```json
{
  "steps": [
    {"check": "baseline", "timeout": 2, "condition": {"role": "checkbox", "field": "checked", "equals": true}},
    {"action": "click", "x": 54, "y": 266},
    {"check": "wait", "timeout": 5, "after": 0, "condition": {"role": "checkbox", "field": "checked", "equals": true}}
  ]
}
```

The baseline reads one complete, readable control whose value does not match
(R-CHECK-2).
An already matching value, absent control, or incomplete baseline fails before
input. The later wait resolves the selector again and requires a matching value.
A replacement widget is allowed: this is a transition of the selected logical
control, not persistent object identity. Baseline and final observation IDs are
included for diagnosis. The baseline proves an earlier nonmatching observation;
it does not prove causation, observe every intermediate change, or make the
multi-field reads atomic. Put it immediately before the intended input sequence.

## Results and failures

Batch `status: "completed"` means all steps completed. `verified: true` additionally
means at least one wait/assert ran and every requested check passed. It verifies
those conditions at their observation times, not every possible app effect.
A baseline alone is not verification. Successful input-only or baseline-only
batches have `verified: null`; failed batches have `verified: false`.

Each check retains its `condition`, `observation` (the last observation),
`limits` (configured budgets), `observations` count, `verified`, and
`duration_seconds`. The observation's `hints` describe standalone
`inspect` flags; check failure messages describe the JSON `limits`. Baselines and transition
waits also include `baseline_observation_id`. Failure stops subsequent steps and
preserves earlier results. It reports `failed_phase: "check"` and `failed_index`.
CLI exit codes remain 0 for success, 1 for runtime failure, and 2 for invalid input.

Optional `failure_screenshot` uses the same path/delay schema and validation as
`screenshot`. It runs only after an operational failure, while the session and caller
remain connected. Its path appears in `artifacts`; its errors appear in
`failure_screenshot_error` without replacing the original check error. It is skipped
on cancellation. Successful batches use only the ordinary final `screenshot`.

The batch retains the serialized input worker while checking. Status and stop
remain on the runner's independent command loop. A cancellation watcher interrupts
in-flight D-Bus calls on client disconnect, session stop, or app exit; polling
pauses are interruptible too. Checks create no input logs or captions.

## Measuring waiting

`benchmark_check_traversal.py` compares default and explicit traversal budgets
against one running large GTK or Qt app. It records baseline, wait, assert,
and standalone inspection observations, complete CLI timings, and runner/app logs.
Write its output outside the repository:

```sh
python docs/benchmark_check_traversal.py /tmp/check-traversal-gtk.json
python docs/benchmark_check_traversal.py /tmp/check-traversal-gtk-x11.json --x11
python docs/benchmark_check_traversal.py /tmp/check-traversal-qt.json --app qt
python docs/benchmark_check_traversal.py /tmp/check-traversal-qt-x11.json --app qt --x11
```

On October 1, 2026, three samples per operation on the pinned NixOS stack
traversed 315 objects in the GTK probe, with its result label at depth 14, and
804 objects in the Qt probe. Defaults (256 nodes, depth 8, 2-second queries)
failed every baseline, wait, and assertion. Explicit settings (1024 nodes,
depth 16, 5-second queries) passed every check on the same apps. Every
check had a 5-second total deadline. Default standalone inspection was partial;
explicit inspection was complete. Nine successful Qt check queries took
more than 2 seconds, exercising the larger query budget.

| App/backend | Default wait, failed | Explicit wait, passed | Explicit assert, passed |
| --- | ---: | ---: | ---: |
| GTK Wayland | 5.155 s | 0.789 s | 0.719 s |
| GTK Xwayland | 5.160 s | 1.134 s | 1.167 s |
| Qt Wayland | 5.152 s | 2.126 s | 2.156 s |
| Qt Xwayland | 5.151 s | 2.116 s | 2.184 s |

Values are median complete CLI times, excluding app/Nix startup. These are
shared-host measurements; development checks overlapped some GTK samples.
They demonstrate coverage with sufficient limits, not total-agent savings.
Raw samples and logs are in the [PR benchmark comments](https://github.com/krscott/framewisp/pull/73#issuecomment-5932359865).

`benchmark_checks.py` compares a fixed 500 ms delay followed by inspection,
client-side inspection polling, and a batch containing a `wait` check. It
uses `tests/wait_probe.py` with a configurable delayed response. Run it in the
development shell with an output path outside the repository. Raw per-run
responses belong in a PR comment, not checked-in files.

These local CLI measurements exclude tool scheduling and model processing.
Tool/model turn counts and total agent wall time must come from separate agent
trials, not inferred from process timings. No general end-to-end speedup is claimed.

On NixOS x86_64 with Python 3.14.7 and the pinned Sway/Pixman/GTK stack, ten
samples per path gave these median complete CLI times (no recording, 1280x720).
Sampling overlapped regression tests on a shared host. At a 350 ms app delay,
fixed sleep took 0.994 s, client polling 0.844 s, and the batch check 0.640 s;
all passed. At a 900 ms app delay, the 500 ms fixed-sleep path failed all ten
checks. Client polling and the batch check passed all ten, at 1.210 s and
1.165 s respectively. The batch check used one CLI call per sample.
[350 ms raw samples](https://github.com/krscott/framewisp/pull/64#issuecomment-5752423801)
and [900 ms raw samples](https://github.com/krscott/framewisp/pull/64#issuecomment-5752423964)
include every response and retry count.

Separate single agent trials took 17.799 s for fixed sleep, 32.494 s for polling
(one retry), and 13.692 s for the batch check. Each passed. The polling
trial used four tool-result/model cycles; the others used two. These include Nix
startup, tool dispatch, completion polls, and model processing, and do not
establish a general speedup. The [agent trial report](https://github.com/krscott/framewisp/pull/64#issuecomment-5752415533)
contains the reproduction script, timing boundary, and raw outputs.
