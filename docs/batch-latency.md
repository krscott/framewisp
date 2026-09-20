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

The current [Wayland samples](batch-wayland.json) were collected during the
regression test run and will be replaced with an isolated run before review is
complete. The report records OS, CPU, Python, vncdotool, and display-tool versions.

Local execution and agent/tool turns are different measurements. For an agent
issuing one tool call per CLI command, this sequence changes four calls into one.
An agent already grouping the four commands into one shell call saves processes
but still uses one tool call. These benchmarks do not measure model inference,
tool scheduling, image transfer, or image inspection, so they establish no
seconds saved for those costs. Both paths produce one screenshot and check the
same app acknowledgement without retries.
