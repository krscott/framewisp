import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from framewisp.errors import SessionError
from framewisp.ownership import (
    JOURNAL,
    LOCK,
    headless_runtime,
    recover_session,
    runtime_path,
    session_lease,
    write_journal,
)


def wait_for(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        assert time.monotonic() < deadline
        time.sleep(0.02)


# R-LIFE-4
@pytest.mark.parametrize(
    "stage",
    [
        "lease",
        "partial-journal",
        "journal",
        "mkdir",
        "runtime",
        "children",
        "shutdown-runtime",
        "shutdown-metadata",
    ],
)
def test_killed_startup_recovers(
    tmp_path: Path, stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "session"
    session.mkdir()
    ready = tmp_path / "ready"
    runtime = runtime_path(session)
    script = """
import os, shutil, sys, time
from pathlib import Path
from framewisp.ownership import session_lease, headless_runtime, write_journal, runtime_path
from framewisp.processes import managed_process
session, ready, stage, probe = map(Path, sys.argv[1:])
with session_lease(session) as lease:
    if str(stage) == 'lease':
        ready.touch()
        time.sleep(60)
    if str(stage) == 'partial-journal':
        (session / '.headless-owner.tmp').write_text('{')
        ready.touch()
        time.sleep(60)
    if str(stage) in {'journal', 'mkdir'}:
        write_journal(session, runtime_path(session), lease, created=False)
        if str(stage) == 'mkdir':
            runtime_path(session).mkdir(mode=0o700)
        ready.touch()
        time.sleep(60)
    with headless_runtime(session, lease) as runtime:
        if str(stage) in {'shutdown-runtime', 'shutdown-metadata'}:
            (session / 'session.json').write_text('{}')
            shutil.rmtree(runtime)
            if str(stage) == 'shutdown-metadata':
                (session / 'session.json').unlink()
            ready.touch()
            time.sleep(60)
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
            monkeypatch.setenv("TMPDIR", str(tmp_path))
            monkeypatch.setattr(tempfile, "tempdir", None)
            deadline = time.monotonic() + 10
            while True:
                try:
                    assert recover_session(session) == 0
                    break
                except SessionError as error:
                    assert "still live" in str(error)
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
            assert not runtime.exists()
            assert not (session / JOURNAL).exists()
            assert all(not Path(f"/proc/{pid}").exists() for pid in pids)
            if stage == "children":
                assert (session / "app.log").exists()
            monkeypatch.undo()
            with (
                session_lease(session) as lease,
                headless_runtime(session, lease) as runtime,
            ):
                assert runtime.is_dir()
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait(timeout=5)


# R-LIFE-5
def test_live_owner_and_copied_or_tampered_metadata(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    copied = tmp_path / "copy"
    with session_lease(session) as lease, headless_runtime(session, lease) as runtime:
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
    assert recover_session(session) == 0
    assert (copied / "session.json").exists()


def test_incomplete_cleanup_preserves_resources_and_diagnostics(tmp_path: Path) -> None:
    runtime = runtime_path(tmp_path)
    with pytest.raises(SessionError, match="Cleanup is incomplete"):
        with (
            session_lease(tmp_path) as lease,
            headless_runtime(tmp_path, lease) as runtime,
        ):
            (runtime / "owner-123.json").write_text('{"error": "signal denied"}')
            (tmp_path / "app.log").write_text("diagnostic")
    with pytest.raises(SessionError, match="owner-123.json"):
        recover_session(tmp_path)
    assert (tmp_path / "app.log").read_text() == "diagnostic"
    assert runtime.is_dir()
    (runtime / "owner-123.json").unlink()
    recover_session(tmp_path)


# R-LIFE-5
@pytest.mark.parametrize("name", [LOCK, JOURNAL])
def test_symlink_ownership_files_refused(tmp_path: Path, name: str) -> None:
    target = tmp_path / "unrelated"
    target.write_text("preserve")
    (tmp_path / name).symlink_to(target)
    with pytest.raises((SessionError, OSError)):
        recover_session(tmp_path)
    assert target.read_text() == "preserve"


@pytest.fixture
def runtime_backup() -> Generator[Path, None, None]:
    with TemporaryDirectory(prefix="framewisp-test-") as directory:
        yield Path(directory) / "saved"


# R-LIFE-5
def test_runtime_replacement_refused(
    tmp_path: Path, runtime_backup: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = runtime_path(tmp_path)
    original = runtime_backup
    with pytest.raises(SessionError, match="Runtime identity changed"):
        with (
            session_lease(tmp_path) as lease,
            headless_runtime(tmp_path, lease) as runtime,
        ):
            runtime.rename(original)
            runtime.mkdir(mode=0o700)
            (runtime / "unrelated").write_text("preserve")
            monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with pytest.raises(SessionError, match="Runtime identity changed"):
        recover_session(tmp_path)
    assert (runtime / "unrelated").read_text() == "preserve"
    shutil.rmtree(runtime)
    original.rename(runtime)
    recover_session(tmp_path)


def replace_lock(session: Path) -> Path:
    original = session / "original.lock"
    (session / LOCK).rename(original)
    os.close(os.open(session / LOCK, os.O_CREAT | os.O_WRONLY, 0o600))
    return original


def run_commands(message: str, prefix: str) -> str:
    # Refusals are the only manual cleanup guide, so tests run them verbatim.
    commands = [
        line.strip() for line in message.splitlines() if line.strip().startswith(prefix)
    ]
    assert commands
    results = [
        subprocess.run(command, shell=True, capture_output=True, text=True, check=False)
        for command in commands
    ]
    # A shell status of 126 or above means the command never ran, so empty output
    # would look clean. grep exits 2 for unreadable /proc entries, which is normal.
    assert all(result.returncode < 126 for result in results), results
    return "".join(result.stdout for result in results)


@pytest.fixture
def abandoned(tmp_path: Path) -> Generator[tuple[Path, Path], None, None]:
    """A session whose owner exited without cleanup, leaving journal and runtime."""
    session = tmp_path / "session with spaces"
    session.mkdir()
    runtime = runtime_path(session)
    with session_lease(session) as lease:
        write_journal(session, runtime, lease, created=False)
        runtime.mkdir(mode=0o700)
        write_journal(session, runtime, lease, created=True)
    (runtime / "kept").write_text("preserve")
    (session / "session.json").write_text("{}")
    (session / "app.log").write_text("diagnostic")
    yield session, runtime
    shutil.rmtree(runtime, ignore_errors=True)


# R-LIFE-5
def test_replaced_lock_refused_while_session_live(tmp_path: Path) -> None:
    with session_lease(tmp_path) as lease, headless_runtime(tmp_path, lease) as runtime:
        (runtime / "important").write_text("live resource")
        original = replace_lock(tmp_path)
        with pytest.raises(SessionError, match="was replaced") as refusal:
            recover_session(tmp_path)
        assert (runtime / "important").read_text() == "live resource"
        assert (tmp_path / JOURNAL).exists()
        # The refusal is the only manual runbook, so it must name exact targets.
        message = str(refusal.value)
        assert "/proc/locks" in message and str(runtime) in message
        assert original.exists()
        assert run_commands(message, "grep ':") != ""
    # The owner still holds the journal's lock, so its own cleanup succeeds.
    assert not runtime.exists()
    assert not (tmp_path / JOURNAL).exists()


# R-LIFE-5
@pytest.mark.parametrize("kind", ["0644", "hard-link", "fifo", "symlink", "directory"])
def test_unusable_replacement_lock_refused_while_session_live(
    tmp_path: Path, kind: str
) -> None:
    lock = tmp_path / LOCK
    with session_lease(tmp_path) as lease, headless_runtime(tmp_path, lease) as runtime:
        original = tmp_path / "original.lock"
        lock.rename(original)
        if kind == "0644":
            lock.touch(mode=0o644)
        elif kind == "hard-link":
            os.link(original, lock)
        elif kind == "fifo":
            os.mkfifo(lock, 0o600)
        elif kind == "symlink":
            lock.symlink_to(original)
        else:
            lock.mkdir()
        with pytest.raises(SessionError, match="is unusable") as refusal:
            recover_session(tmp_path)
        assert str(lock) in str(refusal.value)
        assert "move it aside" in " ".join(str(refusal.value).split())
        assert runtime.is_dir()
        lock.rename(tmp_path / "aside")
        with pytest.raises(SessionError, match="was replaced|still live"):
            recover_session(tmp_path)
        assert runtime.is_dir()
    assert not runtime.exists()


def test_unwritable_session_is_not_blamed_on_lock(tmp_path: Path) -> None:
    tmp_path.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            recover_session(tmp_path)
    finally:
        tmp_path.chmod(0o700)


# R-LIFE-5
def test_deleted_lock_refused_while_session_live(tmp_path: Path) -> None:
    with session_lease(tmp_path) as lease, headless_runtime(tmp_path, lease) as runtime:
        (tmp_path / LOCK).unlink()
        with pytest.raises(SessionError, match="was replaced"):
            recover_session(tmp_path)
        assert runtime.is_dir()
    assert not runtime.exists()


# R-LIFE-5
def test_restoring_original_lock_allows_recovery(
    abandoned: tuple[Path, Path],
) -> None:
    session, runtime = abandoned
    original = replace_lock(session)
    with pytest.raises(SessionError, match="move it back"):
        recover_session(session)
    assert (runtime / "kept").exists()
    original.replace(session / LOCK)
    assert recover_session(session) == 0
    assert not runtime.exists()


# R-LIFE-5
def test_lost_lock_manual_cleanup_commands_work(abandoned: tuple[Path, Path]) -> None:
    session, runtime = abandoned
    replace_lock(session).unlink()
    with pytest.raises(SessionError, match="was replaced") as refusal:
        recover_session(session)
    message = str(refusal.value)
    assert run_commands(message, "grep ") == ""
    run_commands(message, "rm ")
    assert not runtime.exists()
    assert (session / "app.log").read_text() == "diagnostic"
    assert recover_session(session) == 0


def rewrite_journal(session: Path, /, **fields: object) -> None:
    journal = json.loads((session / JOURNAL).read_text())
    (session / JOURNAL).write_text(json.dumps(journal | fields))


def replace_journal_with_directory(session: Path, _: Path) -> None:
    (session / JOURNAL).unlink()
    (session / JOURNAL).mkdir()


def replace_journal_with_symlink(session: Path, _: Path) -> None:
    (session / JOURNAL).unlink()
    (session / JOURNAL).symlink_to(session / "app.log")


def pad_journal(session: Path, _: Path) -> None:
    with (session / JOURNAL).open("a") as journal:
        journal.write(" " * (1 << 16))


def remove_runtime_identity(session: Path, _: Path) -> None:
    journal = json.loads((session / JOURNAL).read_text())
    del journal["runtime"]
    (session / JOURNAL).write_text(json.dumps(journal))


DAMAGED_JOURNALS: dict[str, Callable[[Path, Path], object]] = {
    "nested-arrays": lambda s, _: (s / JOURNAL).write_text("[" * 200_000),
    "nested-arrays-under-size-limit": lambda s, _: (s / JOURNAL).write_text(
        "[" * 60_000
    ),
    "empty": lambda s, _: (s / JOURNAL).write_text(""),
    "truncated": lambda s, _: (s / JOURNAL).write_text('{"session": ['),
    "binary": lambda s, _: (s / JOURNAL).write_bytes(b"\xff\xfe\x00"),
    "not-an-object": lambda s, _: (s / JOURNAL).write_text("[]"),
    "oversized": pad_journal,
    "directory": replace_journal_with_directory,
    "symlink": replace_journal_with_symlink,
    "foreign-session": lambda s, _: rewrite_journal(s, session=[0, 0]),
    "unencodable-runtime": lambda s, r: rewrite_journal(
        s, runtime_path=f"/tmp/\ud800/{r.name}"
    ),
    "overlong-runtime": lambda s, r: rewrite_journal(
        s, runtime_path=f"/{'a' * 5000}/{r.name}"
    ),
    "runtime-under-file": lambda s, r: rewrite_journal(
        s, runtime_path=f"{s}/app.log/{r.name}"
    ),
    "invalid-runtime-identity": lambda s, _: rewrite_journal(s, runtime="bad"),
    "null-runtime-identity": lambda s, _: rewrite_journal(s, runtime=None),
    "no-runtime-identity": remove_runtime_identity,
}


# R-LIFE-5
@pytest.mark.parametrize("kind", DAMAGED_JOURNALS)
def test_unreadable_journal_refused_with_working_manual_steps(
    abandoned: tuple[Path, Path], kind: str
) -> None:
    session, runtime = abandoned
    DAMAGED_JOURNALS[kind](session, runtime)
    with pytest.raises(SessionError, match="Cannot verify ownership") as refusal:
        recover_session(session)
    message = str(refusal.value)
    assert "Nothing was removed" in message and " stop`" in message
    assert (runtime / "kept").read_text() == "preserve"
    assert (session / "session.json").exists()
    assert (session / JOURNAL).exists() or (session / JOURNAL).is_symlink()
    assert run_commands(message, "grep ") == ""
    run_commands(message, "rm ")
    assert not runtime.exists()
    assert (session / "app.log").read_text() == "diagnostic"
    assert recover_session(session) == 0


# R-LIFE-5
def test_unreadable_journal_steps_find_session_under_another_tmpdir(
    abandoned: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, runtime = abandoned
    (session / JOURNAL).write_text("{")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", None)
    with pytest.raises(SessionError, match="TMPDIR") as refusal:
        recover_session(session)
    environment = os.environ | {"XDG_RUNTIME_DIR": str(runtime)}
    with subprocess.Popen(["sleep", "60"], env=environment) as survivor:
        try:
            found = run_commands(str(refusal.value), "grep ")
        finally:
            survivor.kill()
    assert f"/proc/{survivor.pid}/environ" in found


# R-LIFE-5
def test_missing_journal_refused_with_working_manual_steps(
    abandoned: tuple[Path, Path],
) -> None:
    session, runtime = abandoned
    (session / JOURNAL).unlink()
    with pytest.raises(SessionError, match="journal is missing") as refusal:
        recover_session(session)
    assert (runtime / "kept").read_text() == "preserve"
    run_commands(str(refusal.value), "rm ")
    assert not runtime.exists()
    assert recover_session(session) == 0


# R-LIFE-5
def test_foreign_journal_steps_leave_its_runtime_alone(
    abandoned: tuple[Path, Path], tmp_path: Path
) -> None:
    session, runtime = abandoned
    copied = tmp_path / "copy"
    copied.mkdir()
    shutil.copy(session / JOURNAL, copied / JOURNAL)
    with pytest.raises(SessionError, match="does not belong") as refusal:
        recover_session(copied)
    run_commands(str(refusal.value), "rm ")
    assert (runtime / "kept").read_text() == "preserve"
    assert recover_session(copied) == 0
    assert (runtime / "kept").read_text() == "preserve"


def test_journal_is_durable_before_commit_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[tuple[bool, bool]] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        synced.append((is_directory, (tmp_path / JOURNAL).exists()))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    with session_lease(tmp_path) as lease:
        write_journal(tmp_path, runtime_path(tmp_path), lease, created=False)
    # The contents reach disk before the rename, then the rename itself.
    assert synced == [(False, False), (True, True)]


def test_journal_commit_tolerates_filesystem_without_directory_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync

    def refusing_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "Invalid argument")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", refusing_fsync)
    with session_lease(tmp_path) as lease:
        write_journal(tmp_path, runtime_path(tmp_path), lease, created=False)
    assert (tmp_path / JOURNAL).exists()


# R-LIFE-5
@pytest.mark.parametrize("lock", [None, "bad", [1], [True, 1]])
def test_journal_without_valid_lock_identity_refused(
    abandoned: tuple[Path, Path], lock: object
) -> None:
    session, runtime = abandoned
    journal = json.loads((session / JOURNAL).read_text())
    if lock is None:
        del journal["lock"]
    else:
        journal["lock"] = lock
    (session / JOURNAL).write_text(json.dumps(journal))
    before = (session / JOURNAL).read_bytes()
    with pytest.raises(SessionError, match="no valid session lock") as refusal:
        recover_session(session)
    assert " stop`" in str(refusal.value)
    assert (runtime / "kept").read_text() == "preserve"
    assert (session / "session.json").exists()
    assert (session / JOURNAL).read_bytes() == before
    message = str(refusal.value)
    assert run_commands(message, "grep ") == ""
    run_commands(message, "rm ")
    assert not runtime.exists()
    assert recover_session(session) == 0


# R-LIFE-5
def test_symlinked_runtime_refused(
    tmp_path: Path, runtime_backup: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = runtime_path(tmp_path)
    with pytest.raises(SessionError, match="Refusing symlink runtime"):
        with (
            session_lease(tmp_path) as lease,
            headless_runtime(tmp_path, lease) as runtime,
        ):
            (runtime / "preserved").write_text("preserve")
            runtime.rename(runtime_backup)
            runtime.symlink_to(runtime_backup)
            monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with pytest.raises(SessionError, match="Refusing symlink runtime"):
        recover_session(tmp_path)
    assert (runtime_backup / "preserved").read_text() == "preserve"
    runtime.unlink()
    runtime_backup.rename(runtime)
    recover_session(tmp_path)


# R-SESSION-6
@pytest.mark.parametrize("mode", [0o720, 0o702])
def test_run_refuses_writable_session_without_creating_files(
    tmp_path: Path, mode: int
) -> None:
    session = tmp_path / "session"
    session.mkdir()
    session.chmod(mode)
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", str(session), "run", "--", "true"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1
    assert "Unsafe session directory" in result.stderr
    assert list(session.iterdir()) == []


# R-SESSION-6
@pytest.mark.skipif(
    Path("/").stat().st_uid == os.getuid(),
    reason="Needs a directory owned by another user",
)
def test_run_refuses_session_owned_by_another_user() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", "/", "run", "--", "true"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1
    assert "Unsafe session directory" in result.stderr


# R-SESSION-6
def test_session_lease_refuses_another_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    other = os.getuid() + 1
    monkeypatch.setattr(os, "getuid", lambda: other)
    with pytest.raises(SessionError, match="Unsafe session directory"):
        with session_lease(tmp_path):
            pass
    assert not (tmp_path / LOCK).exists()


# R-SESSION-6
@pytest.mark.parametrize("mode", [0o720, 0o702])
def test_writable_session_refused_before_creating_lock(
    tmp_path: Path, mode: int
) -> None:
    tmp_path.chmod(mode)
    try:
        with pytest.raises(SessionError, match="Unsafe session directory"):
            recover_session(tmp_path)
        assert not (tmp_path / LOCK).exists()
    finally:
        tmp_path.chmod(0o700)
