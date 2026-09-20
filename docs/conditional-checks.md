# Conditional checks

A headless batch can send input and verify known accessible state in one request.
Checks use the same private AT-SPI tree as `inspect`. The GTK demo and the test
probe work on Wayland and Xwayland. Toolkit omissions still apply; see
[inspection coverage](ui-inspection.md). Visual-only state needs screenshots.

Save this example as `check.json` and run
`framewisp /tmp/framewisp-demo batch --file check.json` after starting the demo:

```json
{
  "actions": [
    {"action": "click", "x": 120, "y": 100},
    {"action": "key", "chord": "Ctrl+a"},
    {"action": "type", "text": "HelloGUI", "interval": 0},
    {"action": "key", "chord": "Return"},
    {
      "action": "wait", "timeout": 5,
      "condition": {"role": "label", "name": "Entered:", "field": "text", "equals": "Entered: HelloGUI"}
    },
    {
      "action": "assert", "timeout": 2,
      "condition": {"role": "text box", "field": "text", "equals": "HelloGUI"}
    }
  ],
  "failure_capture": {"path": "/tmp/check-failed.png"}
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
role and name substring rules as inspection. It must select exactly one object
in a complete observation. Duplicate matches fail immediately, even if one has
the desired value. Missing objects, stale references, truncation, unsupported
apps, and timeouts never count as success. A wait can retry those observations;
an unavailable bus fails immediately. There is no absence condition and no
snapshot-ID selector.

| Field | Equality semantics |
| --- | --- |
| `text` | Exact, case-sensitive accessible text. Missing Text interface is unknown. |
| `name` | Exact, case-sensitive accessible name. |
| `value` | Exact finite numeric CurrentValue. Missing Value interface is unknown. |
| `checked` | Boolean checked flag on a checkable role or object with the checkable flag. Indeterminate state is unknown. |
| `enabled` | Boolean: either sensitive or enabled AT-SPI flag. This accommodates the demonstrated GTK and Qt controls, but does not prove that input will succeed. |

Role/name strings must be nonempty and at most 1024 characters. Expected text/name
can be empty and is capped at 1024 characters. Check traversal uses inspection's
default depth 8 and 256-node limits, with two matches sufficient to reject
ambiguity. Each observation gets at most two seconds and the remaining deadline.
Large or inaccessible trees may require screenshot-based verification.

## States and transitions

Ordinary `wait` and `assert` check state. A value that already matches is valid.
To require a transition, insert a `baseline` check before the triggering input,
then set the wait's `after` to that baseline's zero-based action index. Both
steps must have the identical condition:

```json
{
  "actions": [
    {"action": "baseline", "timeout": 2, "condition": {"role": "checkbox", "field": "checked", "equals": true}},
    {"action": "click", "x": 54, "y": 266},
    {"action": "wait", "timeout": 5, "after": 0, "condition": {"role": "checkbox", "field": "checked", "equals": true}}
  ]
}
```

The baseline must read one complete, readable control whose value does not match.
An already matching value, absent control, or incomplete baseline fails before
input. The later wait resolves the selector again and requires a matching value.
A replacement widget is allowed: this is a transition of the selected logical
control, not persistent object identity. Baseline and final snapshot IDs are
included for diagnosis. The baseline proves an earlier nonmatching observation;
it does not prove causation, observe every intermediate change, or make the
multi-field reads atomic. Put it immediately before the intended input sequence.

## Results and failures

Batch `status: "completed"` means all steps completed. `verified: true` additionally
means at least one wait/assert ran and every requested check passed. It verifies
those conditions at their observation times, not every possible app effect.
A baseline alone is not verification. Successful input-only or baseline-only
batches have `verified: null`; failed batches have `verified: false`.

Each check retains its `condition`, `observation` (last full inspection response),
`observations` count, `verified`, and `duration_seconds`. Baselines and transition
waits also include `baseline_snapshot_id`. Failure stops subsequent steps and
preserves earlier results. It reports `failed_phase: "check"` and `failed_index`.
CLI exit codes remain 0 for success, 1 for runtime failure, and 2 for invalid input.

Optional `failure_capture` uses the same path/delay schema and validation as
`capture`. It runs only after an operational failure, while the session and caller
remain connected. Its path appears in `artifacts`; capture errors appear in
`failure_capture_error` without replacing the original check error. It is skipped
on cancellation. Successful batches use only the ordinary final `capture`.

The batch retains the serialized input worker while checking. Status and stop
remain on the runner's independent control loop. A cancellation watcher interrupts
in-flight D-Bus calls on client disconnect, session stop, or app exit; polling
pauses are interruptible too. Checks create no input logs or captions.

## Measuring waiting

`benchmark_checks.py` compares a fixed 500 ms delay followed by inspection,
client-side inspection polling, and a batch containing a conditional wait. It
uses `tests/wait_probe.py` with a configurable delayed response. Run it in the
development shell with an output path outside the repository. Raw per-run
responses belong in a PR comment, not checked-in files.

These local CLI measurements exclude tool scheduling and model processing.
Tool/model turn counts and total agent wall time must come from separate agent
trials, not inferred from process timings. No general end-to-end speedup is claimed.
