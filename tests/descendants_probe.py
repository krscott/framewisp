"""An app with a helper and a double-forked, SIGTERM-resistant daemon."""

import os
import signal
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
root.mkdir(exist_ok=True)


def publish_pid(name: str) -> None:
    # Readers use the file's existence as the signal that its PID is complete.
    temporary = root / f"{name}.tmp"
    temporary.write_text(str(os.getpid()))
    temporary.replace(root / name)


if os.fork() == 0:
    publish_pid("helper")
    while True:
        time.sleep(1)
if os.fork() == 0:
    os.setsid()
    if os.fork() != 0:
        os._exit(0)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    publish_pid("detached")
    while True:
        time.sleep(1)
publish_pid("app")
while not (root / "exit").exists():
    time.sleep(0.02)
raise SystemExit(23)
