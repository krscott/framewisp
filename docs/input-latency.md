# Input latency measurements

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
