"""An app with a helper and a double-forked, SIGTERM-resistant daemon."""

import os
import signal
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
root.mkdir(exist_ok=True)
if os.fork() == 0:
    (root / "helper").write_text(str(os.getpid()))
    while True:
        time.sleep(1)
if os.fork() == 0:
    os.setsid()
    if os.fork() != 0:
        os._exit(0)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root / "detached").write_text(str(os.getpid()))
    while True:
        time.sleep(1)
(root / "app").write_text(str(os.getpid()))
while not (root / "exit").exists():
    time.sleep(0.02)
raise SystemExit(23)
