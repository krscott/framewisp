import json
import os
import signal
import time

import pytest
from test_integration import HeadlessSession, cli, headless_session, wait_until

__all__ = ["headless_session"]


# R-INSPECT-6
@pytest.mark.integration
def test_unresponsive_app_does_not_block_stop(
    headless_session: HeadlessSession,
) -> None:
    wait_until(
        lambda: "Demo app ready" in (headless_session.directory / "app.log").read_text()
    )
    metadata = json.loads((headless_session.directory / "session.json").read_text())
    pid = metadata["processes"]["app"]
    os.kill(pid, signal.SIGSTOP)
    try:
        started = time.monotonic()
        cli(headless_session.directory, "stop")
        assert time.monotonic() - started < 10
    finally:
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
    assert headless_session.process.wait(timeout=5) == 0
    assert not headless_session.runtime_dir.exists()
