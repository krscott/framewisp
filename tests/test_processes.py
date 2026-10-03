import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from framewisp import _supervisor
from framewisp.errors import SessionError
from framewisp.processes import managed_process


def wait_for_tree(root: Path) -> list[int]:
    deadline = time.monotonic() + 5
    while not all((root / name).exists() for name in ("app", "helper", "detached")):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return [int((root / name).read_text()) for name in ("app", "helper", "detached")]


@pytest.mark.parametrize("exit_app", [False, True])
def test_owned_descendants_and_independent_trees(
    tmp_path: Path, exit_app: bool
) -> None:
    probe = Path(__file__).with_name("descendants_probe.py")
    first = tmp_path / "first"
    second = tmp_path / "second"
    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    ) as unrelated:
        try:
            with managed_process(
                [sys.executable, str(probe), str(second)],
                log=tmp_path / "second.log",
                env=os.environ.copy(),
            ) as other:
                other_pids = wait_for_tree(second)
                started = time.monotonic()
                with managed_process(
                    [sys.executable, str(probe), str(first)],
                    log=tmp_path / "first.log",
                    env=os.environ.copy(),
                ) as app:
                    pids = wait_for_tree(first)
                    assert app.pid == pids[0]
                    if exit_app:
                        (first / "exit").touch()
                        deadline = time.monotonic() + 5
                        while app.poll() is None:
                            assert time.monotonic() < deadline
                            time.sleep(0.02)
                        assert app.returncode == 23
                assert time.monotonic() - started < 9
                assert all(not Path(f"/proc/{pid}").exists() for pid in pids)
                assert other.poll() is None
                assert unrelated.poll() is None
                assert all(Path(f"/proc/{pid}").exists() for pid in other_pids)
            assert all(not Path(f"/proc/{pid}").exists() for pid in other_pids)
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)


def test_failed_context_cleans_descendants(tmp_path: Path) -> None:
    pids: list[int] = []
    with pytest.raises(RuntimeError, match="failed startup"):
        with managed_process(
            [
                sys.executable,
                str(Path(__file__).with_name("descendants_probe.py")),
                str(tmp_path),
            ],
            log=tmp_path / "app.log",
            env=os.environ.copy(),
        ):
            pids = wait_for_tree(tmp_path)
            raise RuntimeError("failed startup")
    assert all(not Path(f"/proc/{pid}").exists() for pid in pids)


def test_failed_exec_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(SessionError, match="launch failed"):
        with managed_process(
            [str(tmp_path / "missing")], log=tmp_path / "app.log", env=os.environ.copy()
        ):
            pytest.fail("Missing command was launched")


def test_inherited_ignored_sigchld_preserves_exit_status(tmp_path: Path) -> None:
    script = """
import os, signal, sys, time
from pathlib import Path
from framewisp.processes import managed_process
signal.signal(signal.SIGCHLD, signal.SIG_IGN)
with managed_process([sys.executable, '-c', 'raise SystemExit(23)'],
                     log=Path(sys.argv[1]), env=os.environ.copy()) as app:
    deadline = time.monotonic() + 5
    while app.poll() is None:
        assert time.monotonic() < deadline
        time.sleep(.02)
    assert app.returncode == 23
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "app.log")],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_denied_signals_have_bounded_actionable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(descriptor: int, signum: int) -> None:
        raise PermissionError("signal denied")

    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) as app:
        try:
            with monkeypatch.context() as patch:
                patch.setattr(_supervisor, "children", lambda: [app.pid])
                patch.setattr(signal, "pidfd_send_signal", denied)
                patch.setattr(_supervisor, "TERM_SECONDS", 0.05)
                patch.setattr(_supervisor, "KILL_SECONDS", 0.05)
                started = time.monotonic()
                with pytest.raises(RuntimeError, match="signal denied") as error:
                    _supervisor.cleanup(app)
                assert time.monotonic() - started < 1
                assert f"app PID {app.pid}" in str(error.value)
                assert "surviving child PIDs" in str(error.value)
        finally:
            app.terminate()
            app.wait(timeout=5)


def test_unavailable_ownership_does_not_launch_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable() -> None:
        raise PermissionError("prctl denied")

    monkeypatch.setattr(_supervisor, "enable_ownership", unavailable)
    marker = tmp_path / "launched"
    parent, child = socket.socketpair()
    with parent, child:
        _supervisor.supervise(
            child,
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()",
            ],
        )
        child.shutdown(socket.SHUT_WR)
        messages = parent.recv(4096).decode()
    assert "Process ownership unavailable" in messages
    assert "prctl denied" in messages
    assert not marker.exists()
