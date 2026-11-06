import json
import os
import signal
import time

import pytest
from test_integration import Demo, cli, demo, wait_until

__all__ = ["demo"]


# R-INSPECT-6
@pytest.mark.integration
def test_unresponsive_app_does_not_block_stop(demo: Demo) -> None:
    wait_until(lambda: "Demo ready" in (demo.directory / "app.log").read_text())
    state = json.loads((demo.directory / "session.json").read_text())
    pid = state["processes"]["app"]
    os.kill(pid, signal.SIGSTOP)
    try:
        started = time.monotonic()
        cli(demo.directory, "stop")
        assert time.monotonic() - started < 10
    finally:
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
    assert demo.process.wait(timeout=5) == 0
    assert not demo.runtime.exists()
