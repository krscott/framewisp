import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from framewisp.errors import SessionError
from framewisp.ownership import (
    JOURNAL,
    LOCK,
    headless_runtime,
    recover_session,
    runtime_path,
    session_lease,
)


def wait_for(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        assert time.monotonic() < deadline
        time.sleep(0.02)


@pytest.mark.parametrize("stage", ["journal", "mkdir", "runtime", "children"])
def test_killed_startup_recovers(tmp_path: Path, stage: str) -> None:
    session = tmp_path / "session"
    session.mkdir()
    ready = tmp_path / "ready"
    script = """
import os, sys, time
from pathlib import Path
from framewisp.ownership import session_lease, headless_runtime, write_journal, runtime_path
from framewisp.processes import managed_process
session, ready, stage, probe = map(Path, sys.argv[1:])
with session_lease(session) as lease:
    if str(stage) in {'journal', 'mkdir'}:
        write_journal(session, None)
        if str(stage) == 'mkdir':
            runtime_path(session).mkdir(mode=0o700)
        ready.touch()
        time.sleep(60)
    with headless_runtime(session) as runtime:
        if str(stage) == 'runtime':
            ready.touch()
            time.sleep(60)
        env = os.environ | {'FRAMEWISP_OWNER_FD': str(lease), 'FRAMEWISP_OWNER_RUNTIME': str(runtime)}
        with managed_process([sys.executable, str(probe), str(session / 'tree')], log=session / 'app.log', env=env):
            ready.touch()
            time.sleep(60)
"""
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(session),
            str(ready),
            stage,
            str(Path(__file__).with_name("descendants_probe.py")),
        ],
    ) as runner:
        try:
            wait_for(ready)
            pids: list[int] = []
            if stage == "children":
                for name in ("app", "helper", "detached"):
                    path = session / "tree" / name
                    wait_for(path)
                    pids.append(int(path.read_text()))
            runner.kill()
            runner.wait(timeout=5)
            deadline = time.monotonic() + 10
            while True:
                try:
                    assert recover_session(session) == 0
                    break
                except SessionError as error:
                    assert "still live" in str(error)
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
            assert not runtime_path(session).exists()
            assert not (session / JOURNAL).exists()
            assert all(not Path(f"/proc/{pid}").exists() for pid in pids)
            if stage == "children":
                assert (session / "app.log").exists()
            with session_lease(session), headless_runtime(session) as runtime:
                assert runtime.is_dir()
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait(timeout=5)


def test_live_owner_and_copied_or_tampered_metadata(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    copied = tmp_path / "copy"
    with session_lease(session), headless_runtime(session) as runtime:
        (session / "session.json").write_text(
            json.dumps(
                {"runtime_directory": str(runtime), "processes": {"app": os.getpid()}}
            )
        )
        (runtime / "important").write_text("live resource")
        shutil.copytree(session, copied)
        with pytest.raises(SessionError, match="still live"):
            recover_session(session)
        with pytest.raises(SessionError, match="does not belong"):
            recover_session(copied)
        assert (runtime / "important").read_text() == "live resource"
        # Neither runtime paths nor process IDs in session.json authorize recovery.
        (session / "session.json").write_text("not even valid JSON")
    (session / "session.json").unlink()
    assert recover_session(session) == 0
    assert (copied / "session.json").exists()


def test_incomplete_cleanup_preserves_resources_and_diagnostics(tmp_path: Path) -> None:
    runtime = runtime_path(tmp_path)
    with pytest.raises(SessionError, match="Cleanup is incomplete"):
        with session_lease(tmp_path), headless_runtime(tmp_path) as runtime:
            (runtime / "owner-123.json").write_text('{"error": "signal denied"}')
            (tmp_path / "app.log").write_text("diagnostic")
    with pytest.raises(SessionError, match="owner-123.json"):
        recover_session(tmp_path)
    assert (tmp_path / "app.log").read_text() == "diagnostic"
    assert runtime.is_dir()
    (runtime / "owner-123.json").unlink()
    recover_session(tmp_path)


@pytest.mark.parametrize("name", [LOCK, JOURNAL])
def test_symlink_ownership_files_refused(tmp_path: Path, name: str) -> None:
    target = tmp_path / "unrelated"
    target.write_text("preserve")
    (tmp_path / name).symlink_to(target)
    with pytest.raises((SessionError, OSError)):
        recover_session(tmp_path)
    assert target.read_text() == "preserve"


def test_runtime_replacement_refused(tmp_path: Path) -> None:
    runtime = runtime_path(tmp_path)
    original = tmp_path / "saved"
    with pytest.raises(SessionError, match="Runtime identity changed"):
        with session_lease(tmp_path), headless_runtime(tmp_path) as runtime:
            runtime.rename(original)
            runtime.mkdir(mode=0o700)
            (runtime / "unrelated").write_text("preserve")
    with pytest.raises(SessionError, match="Runtime identity changed"):
        recover_session(tmp_path)
    assert (runtime / "unrelated").read_text() == "preserve"
    shutil.rmtree(runtime)
    original.rename(runtime)
    recover_session(tmp_path)
