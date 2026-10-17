import json
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image

from framewisp.errors import SessionError
from framewisp.ownership import (
    JOURNAL,
    check_socket_paths,
    headless_runtime,
    recover_session,
    runtime_path,
    session_lease,
)
from framewisp.sessions import resolve_session


@pytest.fixture
def temp_roots(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Path]]:
    # Keep the test's own socket paths short regardless of pytest's basetemp.
    with tempfile.TemporaryDirectory(prefix="fw-", dir="/tmp") as directory:
        first, second = Path(directory) / "a", Path(directory) / "b"
        first.mkdir()
        second.mkdir()
        monkeypatch.setenv("TMPDIR", str(first))
        monkeypatch.setattr(tempfile, "tempdir", None)
        yield first, second


def test_paths_follow_tmpdir(
    tmp_path: Path, temp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = temp_roots
    monkeypatch.chdir(tmp_path)
    named = resolve_session("demo")
    assert named.parent.parent == first
    assert resolve_session("./demo") == tmp_path / "demo"
    with session_lease(tmp_path), headless_runtime(tmp_path) as runtime:
        assert runtime.parent == first
        assert json.loads((tmp_path / JOURNAL).read_text())["runtime_path"] == str(
            runtime
        )
        monkeypatch.setenv("TMPDIR", str(second))
        monkeypatch.setattr(tempfile, "tempdir", None)
        assert resolve_session("demo") != named
        assert runtime_path(tmp_path).parent == second
    assert not runtime.exists()


def test_default_paths_without_temporary_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.chdir(tmp_path)
    assert runtime_path(tmp_path).parent == Path("/tmp")
    assert resolve_session("demo").parent.parent == Path("/tmp")


def test_legacy_journal_uses_original_tmp_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", "/tmp")
    with session_lease(tmp_path), headless_runtime(tmp_path) as runtime:
        journal = json.loads((tmp_path / JOURNAL).read_text())
        del journal["runtime_path"]
        (tmp_path / JOURNAL).write_text(json.dumps(journal))
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert not runtime.exists()


@pytest.mark.parametrize("size", [107, 108, 109])
def test_socket_length_counts_bytes_and_nul(size: int) -> None:
    suffix = f"/sway-ipc.{os.getuid()}.2147483647.sock"
    runtime_bytes = size - len(os.fsencode(suffix)) - 1
    runtime = Path("/é" + "a" * (runtime_bytes - 3))
    if size <= 108:
        check_socket_paths(runtime)
    else:
        with pytest.raises(SessionError, match="109 bytes.*108-byte limit.*TMPDIR"):
            check_socket_paths(runtime)


def test_long_tmpdir_fails_without_runtime_or_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / ("long" * 30)))
    with session_lease(tmp_path):
        with pytest.raises(SessionError, match="Set TMPDIR to a shorter directory"):
            with headless_runtime(tmp_path):
                pytest.fail("Long runtime path was accepted")
    assert not (tmp_path / JOURNAL).exists()
    assert not runtime_path(tmp_path).exists()


@pytest.mark.parametrize("path", [None, 123, "relative", "/", "/tmp/bad\0path"])
def test_invalid_journal_runtime_path_refused(tmp_path: Path, path: object) -> None:
    with pytest.raises(SessionError, match="runtime path"):
        with session_lease(tmp_path), headless_runtime(tmp_path):
            journal = json.loads((tmp_path / JOURNAL).read_text())
            journal["runtime_path"] = path
            (tmp_path / JOURNAL).write_text(json.dumps(journal))
    with pytest.raises(SessionError, match="runtime path"):
        recover_session(tmp_path)
    journal = json.loads((tmp_path / JOURNAL).read_text())
    journal["runtime_path"] = str(runtime_path(tmp_path))
    (tmp_path / JOURNAL).write_text(json.dumps(journal))
    recover_session(tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("ending", ["stop", "recover"])
def test_session_controls_with_changed_tmpdir(
    tmp_path: Path,
    temp_roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    named: bool,
    ending: str,
) -> None:
    first, second = temp_roots
    monkeypatch.chdir(tmp_path)
    session = resolve_session("demo" if named else str(first / "session"))
    log = tmp_path / "runner.log"
    with log.open("w") as output:
        runner = subprocess.Popen(
            [
                "framewisp",
                "demo" if named else str(session),
                "run",
                "--",
                "framewisp-demo",
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 15
        while "Session ready:" not in log.read_text():
            assert runner.poll() is None, log.read_text()
            assert time.monotonic() < deadline, log.read_text()
            time.sleep(0.05)
        state = json.loads((session / "session.json").read_text())
        runtime = Path(state["runtime_directory"])
        assert runtime.parent == first
        monkeypatch.setenv("TMPDIR", str(second))
        monkeypatch.setattr(tempfile, "tempdir", None)

        def command(*arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["framewisp", str(session), *arguments],
                capture_output=True,
                text=True,
                timeout=20,
            )

        assert command("status").returncode == 0
        # Wait for the app's accessibility registration after socket readiness.
        deadline = time.monotonic() + 10
        while True:
            observation = command("inspect", "--json", "--name", "Apply")
            if (
                observation.returncode == 0
                and json.loads(observation.stdout)["matches"]
            ):
                break
            assert time.monotonic() < deadline, observation
            time.sleep(0.05)
        batch = tmp_path / "batch.json"
        batch.write_text(json.dumps({"actions": [{"action": "key", "chord": "Tab"}]}))
        result = command("batch", "--file", str(batch))
        assert result.returncode == 0, result
        capture = tmp_path / "capture.png"
        result = command("screenshot", str(capture))
        assert result.returncode == 0, result
        with Image.open(capture) as image:
            assert image.size == (1280, 720)
        if ending == "stop":
            result = command("stop")
            assert result.returncode == 0, result
            assert runner.wait(timeout=20) == 0
        else:
            runner.send_signal(signal.SIGKILL)
            runner.wait(timeout=5)
            deadline = time.monotonic() + 15
            while True:
                result = command("recover")
                if result.returncode == 0:
                    break
                assert "still live" in result.stderr, result
                assert time.monotonic() < deadline, result
                time.sleep(0.05)
        assert not runtime.exists()
        assert not (session / "session.json").exists()
        assert not (session / JOURNAL).exists()
        assert all(
            not Path(f"/proc/{pid}").exists() for pid in state["processes"].values()
        )
    finally:
        if runner.poll() is None:
            runner.terminate()
            runner.wait(timeout=20)
