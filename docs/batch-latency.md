# Batch CLI measurements

The benchmark compares four separate CLI calls with one batch on the same
persistent runner. Both execute click `(120, 100)`, type `HelloGUI` at interval
zero, press Return, and save a full 1280x720 PNG. Every sample waits for a new
`Entered: HelloGUI` entry in the real GTK demo's log. Input logging is enabled;
recording is off. No direct VNC calls bypass the production input path.

Run each backend separately in the development environment:

```sh
python docs/benchmark_input.py docs/batch-wayland.json --samples 40
python docs/benchmark_input.py docs/batch-x11.json --samples 40 --x11
```

The script starts and cleans up its own sessions. It resets the demo outside
each timed sequence, takes one warm-up then 40 samples for each case, and keeps
raw wall-clock samples plus nearest-rank p95 and median. Cold session readiness
is measured separately across six launches. Individual click, key and typing
measurements are also separate from the full sequence. The sequence timing
includes process startup, IPC, input logging, capture, and the app-log check.
It excludes session startup, reset, plan-file creation, and intentional pacing.
App acknowledgement is part of the benchmark, not a batch feature.

Measured on September 20, 2026 on NixOS x86_64, Linux 6.18.52, an Intel Core
i7-8550U, Python 3.14.7, vncdotool 1.2.0, Sway 1.12, wayvnc 0.10.1, and
Xwayland 24.1.13. Backends ran sequentially without other framewisp tests or
benchmarks running locally. These are host-specific observations, not latency
guarantees. Raw samples and versions are in [Wayland](batch-wayland.json) and
[Xwayland](batch-x11.json).

| Backend | Four CLI calls p50 / p95 | One batch p50 / p95 | Median reduction |
| --- | ---: | ---: | ---: |
| Wayland | 639 / 748 ms | 209 / 234 ms | 67% |
| Xwayland | 777 / 1013 ms | 340 / 374 ms | 56% |

The batch saved 429 ms on Wayland and 438 ms on Xwayland at the median. Both
paths already use the persistent runner input connection, so these are the
incremental savings from batching. They must not be added to the persistence
speedup reported for #54. Xwayland retains its pointer-priming delay.

Cold metadata readiness was 662 ms median on Wayland and 752 ms on Xwayland;
this does not promise that the app has painted. Recording and caption finalization
were not part of these timings. Input completion alone remains insufficient to
verify an application result, which is why each sequence checks the demo log.

Local execution and agent/tool turns are different measurements. For an agent
issuing one tool call per CLI command, this sequence changes four calls into one.
An agent already grouping the four commands into one shell call saves processes
but still uses one tool call. These benchmarks do not measure model inference,
tool scheduling, image transfer, or image inspection, so they establish no
seconds saved for those costs. Both paths produce one screenshot and check the
same app acknowledgement without retries.
