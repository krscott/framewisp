# Input latency measurements

## Persistent input

Measured September 20, 2026 UTC against baseline `d20cd47`, with the same
bundled demo, 1280x720 Pixman display, complete CLI launches, input logging
enabled, and no recording. Each warm result uses 40 wall-clock samples after
one warm-up per action. p95 uses the nearest-rank method. Host and exact
backend versions are included with the raw samples in [measurements](measurements/).

| Wayland operation | Before p50 / p95 (ms) | Persistent p50 / p95 (ms) | Median speedup |
| --- | ---: | ---: | ---: |
| Click | 907 / 1046 | 135 / 155 | 6.7x |
| Key | 832 / 927 | 135 / 151 | 6.2x |
| Eight ASCII characters, interval 0 | 826 / 868 | 138 / 165 | 6.0x |

These warm medians meet the 120-250 ms engineering estimate in #54. This
baseline was slower than the earlier #53 sample of about 750 ms; host load and
measurement runs differ. Some baseline samples overlapped regression testing.
The samples describe this host, not a latency guarantee or agent inference time.

Cold startup is measured separately, from launching the runner to session
metadata appearing. Across six launches, the median was 567 ms before and
667 ms afterward; maxima were 609 and 703 ms. Six launches are not enough to
characterize a startup tail. The extra 100 ms settles the persistent keyboard
before readiness. The app can still be painting at this point.

The per-command VNC handshake and focus pause are gone. Scrolling no longer
needs its 100 ms disconnect pause because the pointer remains alive. Xwayland
pointer priming still waits 100 ms per pointer action, and its Unicode XTest
helper retains its 100 ms keyboard initialization. Double-click spacing and
requested typing/drag pacing remain. Unicode helpers still run as subprocesses;
these measurements make no Unicode speed claim.

Regression checks cover first keys, modifier release, Unicode and pacing,
scroll targeting, drags, double-click recognition, Xwayland coordinates, and
Qt popup lifetime between commands. Separate tests interrupt held gestures,
cancel ASCII/Unicode typing, query status and stop during long actions, reject
invalid requests, and disconnect the VNC server without replaying queued input.

To reproduce inside the development shell, preserve the baseline source in a
separate directory, then run these commands sequentially on an otherwise idle host:

```sh
python docs/benchmark_input.py before.json --source /path/to/baseline
python docs/benchmark_input.py after.json
python docs/benchmark_input.py before-x11.json --source /path/to/baseline --x11
python docs/benchmark_input.py after-x11.json --x11
```

The script starts and stops its own private sessions. It records all raw
samples and versions. Selection/reset actions stay outside the timed samples.

## Earlier import deferral measurements

The following report describes the earlier implementation, before persistent input.

Measured on the development NixOS x86_64 host with Python 3.14.7 and the pinned
vncdotool 1.2.0. The target was the bundled Wayland demo at 1280x720. Each entry
is the median of five subprocess wall-clock measurements after one warm-up.
These are local observations, not latency guarantees; host load varies.

| Invocation | Before (seconds) | After deferring imports (seconds) |
| --- | ---: | ---: |
| `framewisp --help` | 0.777 | 0.235 |
| `vncdo --help` | 0.711 | 0.796 |
| VNC connection and `pause 0` | 0.819 | 0.848 |
| VNC connection and `pause 0.1` | 0.925 | 0.939 |
| VNC click with the 100 ms settling delay | 0.930 | 0.977 |
| Complete `framewisp SESSION click 120 100` | 1.738 | 1.180 |

`python -X importtime -m framewisp --help` attributed about 368 ms to
`vncdotool.api` and its imports, and 174 ms to `framewisp.attach` and its imports
in a separate baseline sample. Neither is needed by a screenshot or input CLI:
the runner uses the API for its idle VNC connection, and only the foreground
attach owner needs the desktop portal libraries. Importing those modules when
their owning commands run removed about 0.56 seconds from a complete click in
this sample, roughly 32%, without changing input delivery.

The remaining VNC subprocess startup is substantial. Its measured connection
cost also includes protocol setup and scheduling, so these timings do not isolate
network processing alone. The extra `pause 0.1` accounts for approximately 100 ms,
as requested. Framewisp retains that settling delay and the existing scroll/X11
delays because they address observed dropped input. No batching or persistent
input transport was added. Those are separate design choices if further speed
improvements are needed. Agent thinking time remains outside these measurements.

## Reproduce

In `nix develop`, start `framewisp /tmp/latency-demo run -- framewisp-demo` in
another terminal. Then run this from the development shell:

```python
import json
import statistics
import subprocess
import time
from pathlib import Path

session = Path('/tmp/latency-demo')
state = json.loads((session / 'session.json').read_text())
socket = str(Path(state['runtime_directory']) / 'vnc.sock')
vnc = ['vncdo', '-s', socket, '--delay', '0']
commands = [
    ['framewisp', '--help'],
    ['vncdo', '--help'],
    vnc + ['pause', '0'],
    vnc + ['pause', '0.1'],
    vnc + ['pause', '0.1', 'move', '120', '100', 'click', '1'],
    ['framewisp', str(session), 'click', '120', '100'],
]
for command in commands:
    samples = []
    for _ in range(6):
        started = time.perf_counter()
        result = subprocess.run(command, capture_output=True, check=False, timeout=20)
        assert result.returncode == 0, result.stderr
        samples.append(time.perf_counter() - started)
    print(command, statistics.median(samples[1:]))
```

Stop the demo with `framewisp /tmp/latency-demo stop` afterward.
