import errno
import fcntl
import json
import os
import shlex
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
from framewisp.recovery import (
    JOURNAL,
    JOURNAL_TEMPORARY,
    LOCK,
    PRIVATE_CLIP,
    headless_runtime_dir,
    recover_session,
    runtime_dir_path,
    session_lock,
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
        "lock",
        "partial-journal",
        "journal",
        "mkdir",
        "runtime-directory",
        "children",
        "shutdown-runtime-directory",
        "shutdown-metadata",
    ],
)
def test_killed_startup_recovers(
    tmp_path: Path, stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    ready = tmp_path / "ready"
    runtime_dir = runtime_dir_path(session_dir)
    script = """
import os, shutil, sys, time
from pathlib import Path
from framewisp.recovery import session_lock, headless_runtime_dir, write_journal, runtime_dir_path
from framewisp.processes import managed_process
session_dir, ready, stage, probe = map(Path, sys.argv[1:])
with session_lock(session_dir) as lock:
    if str(stage) == 'lock':
        ready.touch()
        time.sleep(60)
    if str(stage) == 'partial-journal':
        (session_dir / '.recovery-journal.tmp').write_text('{')
        ready.touch()
        time.sleep(60)
    if str(stage) in {'journal', 'mkdir'}:
        write_journal(session_dir, runtime_dir_path(session_dir), lock, created=False)
        if str(stage) == 'mkdir':
            runtime_dir_path(session_dir).mkdir(mode=0o700)
        ready.touch()
        time.sleep(60)
    with headless_runtime_dir(session_dir, lock) as runtime_dir:
        if str(stage) in {'shutdown-runtime-directory', 'shutdown-metadata'}:
            (session_dir / 'session.json').write_text('{}')
            shutil.rmtree(runtime_dir)
            if str(stage) == 'shutdown-metadata':
                (session_dir / 'session.json').unlink()
            ready.touch()
            time.sleep(60)
        if str(stage) == 'runtime-directory':
            ready.touch()
            time.sleep(60)
        env = os.environ | {'FRAMEWISP_SESSION_LOCK_FD': str(lock), 'FRAMEWISP_RUNTIME_DIRECTORY': str(runtime_dir)}
        with managed_process([sys.executable, str(probe), str(session_dir / 'tree')], log=session_dir / 'app.log', env=env):
            ready.touch()
            time.sleep(60)
"""
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(session_dir),
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
                    path = session_dir / "tree" / name
                    wait_for(path)
                    pids.append(int(path.read_text()))
            runner.kill()
            runner.wait(timeout=5)
            monkeypatch.setenv("TMPDIR", str(tmp_path))
            monkeypatch.setattr(tempfile, "tempdir", None)
            deadline = time.monotonic() + 10
            while True:
                try:
                    assert recover_session(session_dir) == 0
                    break
                except SessionError as error:
                    assert "is in use by a live session" in str(error)
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
            assert not runtime_dir.exists()
            assert not (session_dir / JOURNAL).exists()
            assert not (session_dir / "session.json").exists()
            assert all(not Path(f"/proc/{pid}").exists() for pid in pids)
            if stage == "children":
                assert (session_dir / "app.log").exists()
            monkeypatch.undo()
            with (
                session_lock(session_dir) as lock,
                headless_runtime_dir(session_dir, lock) as runtime_dir,
            ):
                assert runtime_dir.is_dir()
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait(timeout=5)


# R-LIFE-5
def test_live_runner_and_copied_or_damaged_metadata(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    copied = tmp_path / "copy"
    with (
        session_lock(session_dir) as lock,
        headless_runtime_dir(session_dir, lock) as runtime_dir,
    ):
        (session_dir / "session.json").write_text(
            json.dumps(
                {
                    "runtime_directory": str(runtime_dir),
                    "processes": {"app": os.getpid()},
                }
            )
        )
        (runtime_dir / "important").write_text("live resource")
        shutil.copytree(session_dir, copied)
        with pytest.raises(SessionError, match="is in use by a live session") as busy:
            recover_session(session_dir)
        stop = shlex.join(["framewisp", str(session_dir), "stop"])
        assert f"run `{stop}`" in str(busy.value)
        assert "wait a few seconds and retry" in str(busy.value)
        with pytest.raises(SessionError, match="does not belong"):
            recover_session(copied)
        assert (runtime_dir / "important").read_text() == "live resource"
        # Neither runtime directory paths nor process IDs in session metadata
        # authorize recovery.
        (session_dir / "session.json").write_text("not even valid JSON")
    assert recover_session(session_dir) == 0
    assert (copied / "session.json").exists()


# R-LIFE-7
def test_incomplete_cleanup_preserves_resources_and_diagnostics(tmp_path: Path) -> None:
    runtime_dir = runtime_dir_path(tmp_path)
    with pytest.raises(SessionError, match="Cleanup is incomplete"):
        with (
            session_lock(tmp_path) as lock,
            headless_runtime_dir(tmp_path, lock) as runtime_dir,
        ):
            (runtime_dir / "supervisor-123.json").write_text(
                '{"error": "signal denied"}'
            )
            (tmp_path / "app.log").write_text("diagnostic")
    with pytest.raises(SessionError, match="supervisor-123.json") as refusal:
        recover_session(tmp_path)
    assert "rm -rf" in str(refusal.value)
    assert (tmp_path / "app.log").read_text() == "diagnostic"
    assert runtime_dir.is_dir()
    (runtime_dir / "supervisor-123.json").unlink()
    recover_session(tmp_path)


# R-LIFE-5
@pytest.mark.parametrize("name", [LOCK, JOURNAL])
def test_symlinked_lock_or_journal_refused(tmp_path: Path, name: str) -> None:
    target = tmp_path / "unrelated"
    target.write_text("preserve")
    (tmp_path / name).symlink_to(target)
    with pytest.raises((SessionError, OSError)):
        recover_session(tmp_path)
    assert target.read_text() == "preserve"


@pytest.fixture
def runtime_dir_backup() -> Generator[Path, None, None]:
    with TemporaryDirectory(prefix="framewisp-test-") as directory:
        yield Path(directory) / "saved"


# R-LIFE-5
def test_runtime_dir_replacement_refused(
    tmp_path: Path, runtime_dir_backup: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = runtime_dir_path(tmp_path)
    original = runtime_dir_backup
    with pytest.raises(SessionError, match="Runtime directory identity changed"):
        with (
            session_lock(tmp_path) as lock,
            headless_runtime_dir(tmp_path, lock) as runtime_dir,
        ):
            runtime_dir.rename(original)
            runtime_dir.mkdir(mode=0o700)
            (runtime_dir / "unrelated").write_text("preserve")
            monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with pytest.raises(SessionError, match="Runtime directory identity changed"):
        recover_session(tmp_path)
    assert (runtime_dir / "unrelated").read_text() == "preserve"
    shutil.rmtree(runtime_dir)
    original.rename(runtime_dir)
    recover_session(tmp_path)


def replace_lock(session_dir: Path) -> Path:
    original = session_dir / "original.lock"
    (session_dir / LOCK).rename(original)
    os.close(os.open(session_dir / LOCK, os.O_CREAT | os.O_WRONLY, 0o600))
    return original


def run_commands(message: str, prefix: str) -> str:
    # Refusals are the only manual cleanup guide, so tests run them verbatim.
    commands = [
        line.strip()
        for line in message.splitlines()
        if line.strip().removeprefix("LC_ALL=C ").startswith(prefix)
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
    """A session whose runner exited without cleanup, leaving journal and runtime directory."""
    session_dir = tmp_path / "session with spaces"
    session_dir.mkdir()
    runtime_dir = runtime_dir_path(session_dir)
    with session_lock(session_dir) as lock:
        write_journal(session_dir, runtime_dir, lock, created=False)
        runtime_dir.mkdir(mode=0o700)
        write_journal(session_dir, runtime_dir, lock, created=True)
    (runtime_dir / "kept").write_text("preserve")
    (session_dir / "session.json").write_text("{}")
    (session_dir / "app.log").write_text("diagnostic")
    yield session_dir, runtime_dir
    shutil.rmtree(runtime_dir, ignore_errors=True)


# R-LIFE-5
def test_replaced_lock_refused_while_session_live(tmp_path: Path) -> None:
    with (
        session_lock(tmp_path) as lock,
        headless_runtime_dir(tmp_path, lock) as runtime_dir,
    ):
        (runtime_dir / "important").write_text("live resource")
        original = replace_lock(tmp_path)
        with pytest.raises(SessionError, match="was replaced") as refusal:
            recover_session(tmp_path)
        assert (runtime_dir / "important").read_text() == "live resource"
        assert (tmp_path / JOURNAL).exists()
        # The refusal is the only manual runbook, so it must name exact targets.
        message = str(refusal.value)
        assert "/proc/locks" in message and str(runtime_dir) in message
        assert original.exists()
        assert run_commands(message, "grep ':") != ""
    # The runner still holds the journal's lock, so its own cleanup succeeds.
    assert not runtime_dir.exists()
    assert not (tmp_path / JOURNAL).exists()


# R-LIFE-5
@pytest.mark.parametrize("kind", ["0644", "fifo"])
def test_openable_replacement_lock_refused_while_session_live(
    tmp_path: Path, kind: str
) -> None:
    lock_path = tmp_path / LOCK
    with (
        session_lock(tmp_path) as lock,
        headless_runtime_dir(tmp_path, lock) as runtime_dir,
    ):
        lock_path.rename(tmp_path / "original.lock")
        if kind == "0644":
            lock_path.touch(mode=0o644)
        else:
            os.mkfifo(lock_path, 0o600)
        with pytest.raises(SessionError, match="was replaced"):
            recover_session(tmp_path)
        assert runtime_dir.is_dir()
    assert not runtime_dir.exists()


# R-LIFE-5
def test_hard_linked_lock_is_the_same_lock_while_session_live(tmp_path: Path) -> None:
    lock_path = tmp_path / LOCK
    with (
        session_lock(tmp_path) as lock,
        headless_runtime_dir(tmp_path, lock) as runtime_dir,
    ):
        original = tmp_path / "original.lock"
        lock_path.rename(original)
        os.link(original, lock_path)
        with pytest.raises(SessionError, match="is in use by a live session"):
            recover_session(tmp_path)
        assert runtime_dir.is_dir()
    assert not runtime_dir.exists()


# R-LIFE-5, R-LIFE-7
@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_unusable_replacement_lock_refused_while_session_live(
    tmp_path: Path, kind: str
) -> None:
    lock_path = tmp_path / LOCK
    with (
        session_lock(tmp_path) as lock,
        headless_runtime_dir(tmp_path, lock) as runtime_dir,
    ):
        original = tmp_path / "original.lock"
        lock_path.rename(original)
        if kind == "symlink":
            lock_path.symlink_to(original)
        else:
            lock_path.mkdir()
        with pytest.raises(SessionError, match="is unusable") as refusal:
            recover_session(tmp_path)
        assert str(lock_path) in str(refusal.value)
        assert "move it aside" in " ".join(str(refusal.value).split())
        assert runtime_dir.is_dir()
        lock_path.rename(tmp_path / "aside")
        with pytest.raises(
            SessionError, match="was replaced|is in use by a live session"
        ):
            recover_session(tmp_path)
        assert runtime_dir.is_dir()
    assert not runtime_dir.exists()


# R-LIFE-4
def test_permissive_chmod_does_not_block_recovery(
    abandoned: tuple[Path, Path],
) -> None:
    session_dir, runtime_dir = abandoned
    subprocess.run(["chmod", "-R", "go+rX", session_dir], check=True)
    assert stat.S_IMODE((session_dir / LOCK).stat().st_mode) & 0o044
    assert recover_session(session_dir) == 0
    assert not runtime_dir.exists()


# R-LIFE-4, R-LIFE-5
def test_hard_linked_copy_refused_and_original_recovers(
    abandoned: tuple[Path, Path], tmp_path: Path
) -> None:
    session_dir, runtime_dir = abandoned
    copied = tmp_path / "copy"
    subprocess.run(["cp", "-al", session_dir, copied], check=True)
    assert (session_dir / LOCK).stat().st_nlink == 2
    assert (copied / JOURNAL).stat().st_ino == (session_dir / JOURNAL).stat().st_ino
    with pytest.raises(SessionError, match="does not belong"):
        recover_session(copied)
    assert (runtime_dir / "kept").read_text() == "preserve"
    assert recover_session(session_dir) == 0
    assert not runtime_dir.exists()
    with pytest.raises(SessionError, match="does not belong"):
        recover_session(copied)


# R-LIFE-5
def test_replaced_lock_refused_after_permissive_chmod(
    abandoned: tuple[Path, Path],
) -> None:
    session_dir, runtime_dir = abandoned
    replace_lock(session_dir)
    subprocess.run(["chmod", "-R", "go+rX", session_dir], check=True)
    with pytest.raises(SessionError, match="was replaced"):
        recover_session(session_dir)
    assert (runtime_dir / "kept").read_text() == "preserve"


# R-LIFE-5, R-LIFE-7
@pytest.mark.skipif(os.getuid() == 0, reason="Root can open any file")
def test_lock_the_owner_cannot_open_gives_steps(abandoned: tuple[Path, Path]) -> None:
    session_dir, runtime_dir = abandoned
    (session_dir / LOCK).chmod(0)
    with pytest.raises(SessionError, match="is unusable") as refusal:
        recover_session(session_dir)
    assert "chmod 600" in str(refusal.value)
    assert (runtime_dir / "kept").read_text() == "preserve"
    (session_dir / LOCK).chmod(0o600)
    assert recover_session(session_dir) == 0


def test_unwritable_session_is_not_blamed_on_lock(tmp_path: Path) -> None:
    tmp_path.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            recover_session(tmp_path)
    finally:
        tmp_path.chmod(0o700)


# R-LIFE-5
def test_deleted_lock_refused_while_session_live(tmp_path: Path) -> None:
    with (
        session_lock(tmp_path) as lock,
        headless_runtime_dir(tmp_path, lock) as runtime_dir,
    ):
        (tmp_path / LOCK).unlink()
        with pytest.raises(SessionError, match="was replaced"):
            recover_session(tmp_path)
        assert runtime_dir.is_dir()
    assert not runtime_dir.exists()


# R-LIFE-5
def test_restoring_original_lock_allows_recovery(
    abandoned: tuple[Path, Path],
) -> None:
    session_dir, runtime_dir = abandoned
    original = replace_lock(session_dir)
    with pytest.raises(SessionError, match="move it back"):
        recover_session(session_dir)
    assert (runtime_dir / "kept").exists()
    original.replace(session_dir / LOCK)
    assert recover_session(session_dir) == 0
    assert not runtime_dir.exists()


# R-LIFE-5, R-LIFE-7
def test_lost_lock_manual_cleanup_commands_work(abandoned: tuple[Path, Path]) -> None:
    session_dir, runtime_dir = abandoned
    replace_lock(session_dir).unlink()
    with pytest.raises(SessionError, match="was replaced") as refusal:
        recover_session(session_dir)
    message = str(refusal.value)
    assert run_commands(message, "grep ") == ""
    run_commands(message, "rm ")
    assert not runtime_dir.exists()
    assert (session_dir / "app.log").read_text() == "diagnostic"
    assert recover_session(session_dir) == 0


def rewrite_journal(session_dir: Path, /, **fields: object) -> None:
    journal = json.loads((session_dir / JOURNAL).read_text())
    (session_dir / JOURNAL).write_text(json.dumps(journal | fields))


def replace_journal_with_directory(session_dir: Path, _: Path) -> None:
    (session_dir / JOURNAL).unlink()
    (session_dir / JOURNAL).mkdir()


def replace_journal_with_symlink(session_dir: Path, _: Path) -> None:
    (session_dir / JOURNAL).unlink()
    (session_dir / JOURNAL).symlink_to(session_dir / "app.log")


def pad_journal(session_dir: Path, _: Path) -> None:
    with (session_dir / JOURNAL).open("a") as journal:
        journal.write(" " * (1 << 16))


def remove_journal_field(session_dir: Path, field: str) -> None:
    journal = json.loads((session_dir / JOURNAL).read_text())
    del journal[field]
    (session_dir / JOURNAL).write_text(json.dumps(journal))


DAMAGED_JOURNALS: dict[str, Callable[[Path, Path], object]] = {
    "nested-arrays": lambda s, _: (s / JOURNAL).write_text("[" * 200_000),
    "nested-arrays-under-size-limit": lambda s, _: (s / JOURNAL).write_text(
        "[" * 60_000
    ),
    "empty": lambda s, _: (s / JOURNAL).write_text(""),
    "truncated": lambda s, _: (s / JOURNAL).write_text(
        '{"session_directory_identity": ['
    ),
    "binary": lambda s, _: (s / JOURNAL).write_bytes(b"\xff\xfe\x00"),
    "not-an-object": lambda s, _: (s / JOURNAL).write_text("[]"),
    "oversized": pad_journal,
    "directory": replace_journal_with_directory,
    "symlink": replace_journal_with_symlink,
    "foreign-session": lambda s, _: rewrite_journal(
        s, session_directory_identity=[0, 0]
    ),
    "unencodable-runtime-directory": lambda s, r: rewrite_journal(
        s, runtime_directory=f"/tmp/\ud800/{r.name}"
    ),
    "overlong-runtime-directory": lambda s, r: rewrite_journal(
        s, runtime_directory=f"/{'a' * 5000}/{r.name}"
    ),
    "runtime-directory-under-file": lambda s, r: rewrite_journal(
        s, runtime_directory=f"{s}/app.log/{r.name}"
    ),
    "invalid-runtime-directory-identity": lambda s, _: rewrite_journal(
        s, runtime_directory_identity="bad"
    ),
    "no-runtime-directory-identity": lambda s, _: remove_journal_field(
        s, "runtime_directory_identity"
    ),
    "no-runtime-directory-path": lambda s, _: remove_journal_field(
        s, "runtime_directory"
    ),
}


# R-LIFE-5, R-LIFE-7
@pytest.mark.parametrize("kind", DAMAGED_JOURNALS)
def test_unreadable_journal_refused_with_working_manual_steps(
    abandoned: tuple[Path, Path], kind: str
) -> None:
    session_dir, runtime_dir = abandoned
    DAMAGED_JOURNALS[kind](session_dir, runtime_dir)
    with pytest.raises(
        SessionError, match="Cannot verify the recovery journal"
    ) as refusal:
        recover_session(session_dir)
    message = str(refusal.value)
    assert "Nothing was removed" in message and " stop`" in message
    assert (runtime_dir / "kept").read_text() == "preserve"
    assert (session_dir / "session.json").exists()
    assert (session_dir / JOURNAL).exists() or (session_dir / JOURNAL).is_symlink()
    assert run_commands(message, "grep ") == ""
    run_commands(message, "rm ")
    assert not runtime_dir.exists()
    assert (session_dir / "app.log").read_text() == "diagnostic"
    assert recover_session(session_dir) == 0


# R-LIFE-5, R-LIFE-7
def test_fifo_journal_with_a_writer_refused_with_steps(
    abandoned: tuple[Path, Path],
) -> None:
    session_dir, runtime_dir = abandoned
    (session_dir / JOURNAL).unlink()
    os.mkfifo(session_dir / JOURNAL, 0o600)
    writer = os.open(session_dir / JOURNAL, os.O_RDWR)
    try:
        with pytest.raises(SessionError, match="must be a regular file"):
            recover_session(session_dir)
    finally:
        os.close(writer)
    assert (runtime_dir / "kept").read_text() == "preserve"


# R-LIFE-5, R-LIFE-7
def test_unreadable_journal_steps_find_session_under_another_tmpdir(
    abandoned: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir, runtime_dir = abandoned
    (session_dir / JOURNAL).write_text("{")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", None)
    with pytest.raises(SessionError, match="TMPDIR") as refusal:
        recover_session(session_dir)
    environment = os.environ | {"XDG_RUNTIME_DIR": str(runtime_dir)}
    with subprocess.Popen(["sleep", "60"], env=environment) as survivor:
        try:
            found = run_commands(str(refusal.value), "grep ")
        finally:
            survivor.kill()
    assert f"/proc/{survivor.pid}/environ" in found


# R-LIFE-5, R-LIFE-7
def test_journal_without_runtime_directory_path_refused_outside_default_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    runtime_dir = runtime_dir_path(session_dir)
    with session_lock(session_dir) as lock:
        write_journal(session_dir, runtime_dir, lock, created=False)
        runtime_dir.mkdir(mode=0o700)
        write_journal(session_dir, runtime_dir, lock, created=True)
    (runtime_dir / "supervisor-123.json").write_text("{}")
    remove_journal_field(session_dir, "runtime_directory")
    with pytest.raises(SessionError, match="no runtime directory path") as refusal:
        recover_session(session_dir)
    assert str(runtime_dir) in str(refusal.value)
    assert (session_dir / JOURNAL).exists()
    assert (runtime_dir / "supervisor-123.json").exists()


# R-LIFE-5, R-LIFE-7
def test_missing_journal_refused_with_working_manual_steps(
    abandoned: tuple[Path, Path],
) -> None:
    session_dir, runtime_dir = abandoned
    (session_dir / JOURNAL).unlink()
    with pytest.raises(SessionError, match="journal is missing") as refusal:
        recover_session(session_dir)
    assert (runtime_dir / "kept").read_text() == "preserve"
    run_commands(str(refusal.value), "rm ")
    assert not runtime_dir.exists()
    assert recover_session(session_dir) == 0


def replace_runtime_dir(session_dir: Path, runtime_dir: Path) -> None:
    runtime_dir.rename(session_dir / "moved-runtime-directory")
    runtime_dir.mkdir(mode=0o700)
    (runtime_dir / "kept").write_text("preserve")


def symlink_runtime_dir(session_dir: Path, runtime_dir: Path) -> None:
    runtime_dir.rename(session_dir / "moved-runtime-directory")
    runtime_dir.symlink_to(session_dir / "moved-runtime-directory")


def open_runtime_dir_permissions(_: Path, runtime_dir: Path) -> None:
    runtime_dir.chmod(0o755)


# R-LIFE-5, R-LIFE-7
@pytest.mark.parametrize(
    "change",
    [replace_runtime_dir, symlink_runtime_dir, open_runtime_dir_permissions],
    ids=["replaced", "symlink", "permissions"],
)
def test_changed_runtime_dir_refused_with_working_manual_steps(
    abandoned: tuple[Path, Path],
    change: Callable[[Path, Path], object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir, runtime_dir = abandoned
    change(session_dir, runtime_dir)
    # The journal's verified path applies even under another TMPDIR.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with pytest.raises(SessionError, match="Nothing was removed") as refusal:
        recover_session(session_dir)
    message = str(refusal.value)
    assert " stop`" in message and shlex.quote(str(runtime_dir)) in message
    assert (session_dir / JOURNAL).exists()
    assert (runtime_dir / "kept").read_text() == "preserve"
    assert run_commands(message, "grep ") == ""
    run_commands(message, "rm ")
    assert not runtime_dir.exists() and not runtime_dir.is_symlink()
    assert recover_session(session_dir) == 0


# R-LIFE-7
def test_supervisor_record_names_cannot_add_manual_steps(
    abandoned: tuple[Path, Path],
) -> None:
    session_dir, runtime_dir = abandoned
    (runtime_dir / "supervisor-x\n  rm -rf -- planted\n.json").write_text("{}")
    with pytest.raises(SessionError, match="Cleanup is incomplete") as refusal:
        recover_session(session_dir)
    assert "  rm -rf -- planted" not in str(refusal.value).splitlines()


# R-LIFE-5, R-LIFE-7
def test_null_runtime_directory_identity_steps_use_the_journal_path(
    abandoned: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir, runtime_dir = abandoned
    rewrite_journal(session_dir, runtime_directory_identity=None)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with pytest.raises(SessionError, match="no runtime directory identity") as refusal:
        recover_session(session_dir)
    message = str(refusal.value)
    assert " stop`" in message and (runtime_dir / "kept").read_text() == "preserve"
    assert (session_dir / JOURNAL).exists()
    run_commands(message, "rm ")
    assert not runtime_dir.exists()
    assert recover_session(session_dir) == 0


# R-LIFE-7
@pytest.mark.parametrize("kind", ["attached", "teaching"])
def test_stale_desktop_session_gets_desktop_session_steps(
    tmp_path: Path, kind: str
) -> None:
    (tmp_path / "session.json").write_text(json.dumps({"kind": kind}))
    with pytest.raises(SessionError, match="attach or teach") as refusal:
        recover_session(tmp_path)
    message = str(refusal.value)
    assert " run -- APP" not in message
    assert "--stop-sharing" not in message
    assert "attach again" not in message
    assert "(for teach, with a new lesson directory)" in message
    assert (tmp_path / "session.json").exists()
    run_commands(message, "rm ")
    assert recover_session(tmp_path) == 0


@pytest.mark.parametrize("kind", ["attached", "teaching"])
def test_run_gives_desktop_session_steps_for_stale_desktop_metadata(
    tmp_path: Path, kind: str
) -> None:
    (tmp_path / "session.json").write_text(json.dumps({"kind": kind}))
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", str(tmp_path), "run", "--", "true"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1
    assert "records an attached or teaching session" in result.stderr
    assert " recover`" not in result.stderr
    assert result.stderr.index("has stopped") < result.stderr.index("rm -f")
    assert (tmp_path / "session.json").exists()
    run_commands(result.stderr, "rm ")
    assert not (tmp_path / "session.json").exists()


@pytest.mark.parametrize("kind", [[], {}])
def test_recover_refuses_metadata_with_an_unhashable_kind(
    tmp_path: Path, kind: object
) -> None:
    (tmp_path / "session.json").write_text(json.dumps({"kind": kind}))
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", str(tmp_path), "recover"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1
    assert "journal is missing" in result.stderr
    assert "Traceback" not in result.stderr
    assert (tmp_path / "session.json").exists()


# R-LIFE-7
@pytest.mark.parametrize("kind", ["fifo", "device-link"])
def test_unreadable_session_metadata_cannot_hang_recovery(
    tmp_path: Path, kind: str
) -> None:
    metadata = tmp_path / "session.json"
    if kind == "fifo":
        os.mkfifo(metadata, 0o600)
    else:
        metadata.symlink_to("/dev/zero")
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", str(tmp_path), "recover"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1
    assert "journal is missing" in result.stderr and "rm -rf" in result.stderr


# R-LIFE-4
def test_lock_path_changed_after_flock_recovers_the_held_lock(
    abandoned: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir, runtime_dir = abandoned
    lock_path = session_dir / LOCK
    real_flock = fcntl.flock

    def change_lock_then_flock(descriptor: int, operation: int) -> None:
        real_flock(descriptor, operation)
        lock_path.unlink()
        lock_path.touch(mode=0o600)

    # flock excluded every holder of the journal's lock, so no runner can remain.
    monkeypatch.setattr(fcntl, "flock", change_lock_then_flock)
    assert recover_session(session_dir) == 0
    assert not runtime_dir.exists()


def run_missing_session_commands(message: str, runtime_dir: Path) -> None:
    # The lists are compared by name, so each must print the bare name.
    assert runtime_dir.name in run_commands(message, "find ").splitlines()
    assert runtime_dir.name not in run_commands(message, "grep ").splitlines()
    environment = os.environ | {"XDG_RUNTIME_DIR": str(runtime_dir)}
    with subprocess.Popen(["sleep", "60"], env=environment) as survivor:
        try:
            used = run_commands(message, "grep ")
        finally:
            survivor.kill()
    assert runtime_dir.name in used.splitlines()
    # A starting session has no marked process, so the lists cannot authorize rm.
    assert "If unsure, leave it" in " ".join(message.split())
    assert not [line for line in message.splitlines() if line.strip().startswith("rm ")]


# R-LIFE-7
def test_deleted_session_refusal_says_what_is_left_to_check(
    abandoned: tuple[Path, Path],
) -> None:
    session_dir, runtime_dir = abandoned
    shutil.rmtree(session_dir)
    with pytest.raises(SessionError, match="does not exist") as refusal:
        recover_session(session_dir)
    assert (runtime_dir / "kept").read_text() == "preserve"
    run_missing_session_commands(str(refusal.value), runtime_dir)


# R-LIFE-7
def test_moved_session_recovers_at_its_new_path(
    abandoned: tuple[Path, Path], tmp_path: Path
) -> None:
    session_dir, runtime_dir = abandoned
    moved = tmp_path / "moved"
    session_dir.rename(moved)
    with pytest.raises(SessionError, match="does not exist") as refusal:
        recover_session(session_dir)
    assert "moved" in str(refusal.value)
    assert recover_session(moved) == 0
    assert not runtime_dir.exists()


# R-LIFE-7
def test_session_deleted_during_shutdown_keeps_runtime_dir_and_gives_steps(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    runtime_dir = runtime_dir_path(session_dir)
    try:
        with pytest.raises(SessionError, match="does not exist") as refusal:
            with (
                session_lock(session_dir) as lock,
                headless_runtime_dir(session_dir, lock),
            ):
                (runtime_dir / "kept").write_text("preserve")
                shutil.rmtree(session_dir)
        assert (runtime_dir / "kept").read_text() == "preserve"
        run_missing_session_commands(str(refusal.value), runtime_dir)
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


# R-LIFE-7
def test_session_vanishing_during_lock_gets_steps(
    abandoned: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir, runtime_dir = abandoned
    real_open = os.open

    def remove_session_then_open(path: Path, flags: int, mode: int = 0o777) -> int:
        session_dir.rename(session_dir.with_name("moved"))
        return real_open(path, flags, mode)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", remove_session_then_open)
        with pytest.raises(SessionError, match="does not exist") as refusal:
            recover_session(session_dir)
    run_missing_session_commands(str(refusal.value), runtime_dir)


# R-LIFE-7
@pytest.mark.skipif(os.getuid() == 0, reason="Root can write a read-only directory")
@pytest.mark.parametrize("journal", ["unreadable", "missing"])
def test_read_only_tmpdir_is_still_checked_for_runtime_dir(
    tmp_path: Path, journal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setattr(tempfile, "tempdir", None)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    runtime_dir = runtime_dir_path(session_dir)
    with session_lock(session_dir) as lock:
        write_journal(session_dir, runtime_dir, lock, created=False)
        runtime_dir.mkdir(mode=0o700)
        write_journal(session_dir, runtime_dir, lock, created=True)
    (runtime_dir / "supervisor-1.json").write_text("{}")
    if journal == "missing":
        (session_dir / JOURNAL).unlink()
    else:
        (session_dir / JOURNAL).write_text("{")
    temporary.chmod(0o500)
    monkeypatch.setattr(tempfile, "tempdir", None)
    try:
        with pytest.raises(SessionError, match="Nothing was removed") as refusal:
            recover_session(session_dir)
        assert f"find {runtime_dir} -type d -exec chmod u+rwx {{}} +\n" in str(
            refusal.value
        )
        assert f"rm -rf -- {runtime_dir}" in str(refusal.value)
        assert (runtime_dir / "supervisor-1.json").exists()
    finally:
        temporary.chmod(0o700)


# R-LIFE-7
@pytest.mark.skipif(os.getuid() == 0, reason="Root can write a read-only directory")
def test_runtime_dir_found_when_tmpdir_was_read_only_from_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "configured"
    configured.mkdir(mode=0o500)
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setenv("TMPDIR", str(configured))
    monkeypatch.setattr(tempfile, "tempdir", str(fallback))
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    runtime_dir = runtime_dir_path(session_dir)
    runtime_dir.mkdir(mode=0o700)
    (runtime_dir / "supervisor-1.json").write_text("{}")
    with pytest.raises(SessionError, match="journal is missing") as refusal:
        recover_session(session_dir)
    assert f"rm -rf -- {runtime_dir}" in str(refusal.value)


# R-LIFE-7
@pytest.mark.skipif(os.getuid() == 0, reason="Root can search any directory")
def test_inaccessible_tmpdir_still_gives_steps(
    abandoned: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir, runtime_dir = abandoned
    (session_dir / JOURNAL).write_text("{")
    closed = tmp_path / "closed"
    (closed / "temporary").mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(closed / "temporary"))
    closed.chmod(0)
    try:
        with pytest.raises(SessionError, match="Nothing was removed") as refusal:
            recover_session(session_dir)
        assert " stop`" in str(refusal.value) and str(runtime_dir) in str(refusal.value)
    finally:
        closed.chmod(0o700)


# R-LIFE-7
@pytest.mark.skipif(os.getuid() == 0, reason="Root can search any directory")
def test_recovery_refuses_when_a_temporary_location_cannot_be_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / JOURNAL_TEMPORARY).write_text("{")
    closed = tmp_path / "closed"
    (closed / "temporary").mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(closed / "temporary"))
    closed.chmod(0)
    try:
        with pytest.raises(SessionError, match="journal is missing") as refusal:
            recover_session(session_dir)
        assert (session_dir / JOURNAL_TEMPORARY).exists()
        assert "Restore access, then" in " ".join(str(refusal.value).split())
        # Nothing ran under this TMPDIR, so the printed alternative applies.
        monkeypatch.delenv("TMPDIR")
        assert recover_session(session_dir) == 0
    finally:
        closed.chmod(0o700)


# R-LIFE-7
@pytest.mark.skipif(os.getuid() == 0, reason="Root can write a read-only directory")
def test_relative_read_only_tmpdir_is_still_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "relative").mkdir()
    monkeypatch.setenv("TMPDIR", "relative")
    monkeypatch.setattr(tempfile, "tempdir", None)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    runtime_dir = tmp_path / "relative" / runtime_dir_path(session_dir).name
    runtime_dir.mkdir(mode=0o700)
    (tmp_path / "relative").chmod(0o500)
    monkeypatch.setattr(tempfile, "tempdir", None)
    try:
        with pytest.raises(SessionError, match="journal is missing") as refusal:
            recover_session(session_dir)
        assert str(runtime_dir) in str(refusal.value)
    finally:
        (tmp_path / "relative").chmod(0o700)


# R-LIFE-7
def test_missing_session_listing_follows_symlinked_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("TMPDIR", str(link))
    monkeypatch.setattr(tempfile, "tempdir", None)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    runtime_dir = runtime_dir_path(session_dir)
    runtime_dir.mkdir(mode=0o700)
    shutil.rmtree(session_dir)
    with pytest.raises(SessionError, match="does not exist") as refusal:
        recover_session(session_dir)
    assert runtime_dir.name in run_commands(str(refusal.value), "find ").splitlines()


# R-LIFE-7
def test_relative_tmpdir_with_deleted_cwd_still_gives_steps(
    abandoned: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir, runtime_dir = abandoned
    (session_dir / JOURNAL).write_text("{")
    monkeypatch.setenv("TMPDIR", "relative")
    monkeypatch.setattr(tempfile, "tempdir", str(runtime_dir.parent))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    cwd.rmdir()
    with pytest.raises(SessionError, match="Nothing was removed") as refusal:
        recover_session(session_dir)
    assert str(runtime_dir) in str(refusal.value)


def refuse_tempdir() -> str:
    raise FileNotFoundError("No usable temporary directory found")


# R-LIFE-7
@pytest.mark.parametrize("journal", ["unreadable", "missing"])
def test_unusable_tempdir_refusal_gives_working_manual_steps(
    abandoned: tuple[Path, Path], journal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir, runtime_dir = abandoned
    if journal == "missing":
        (session_dir / JOURNAL).unlink()
    else:
        (session_dir / JOURNAL).write_text("{")
    monkeypatch.setenv("TMPDIR", str(runtime_dir.parent))
    monkeypatch.setattr(tempfile, "gettempdir", refuse_tempdir)
    with pytest.raises(SessionError, match="Nothing was removed") as refusal:
        recover_session(session_dir)
    message = str(refusal.value)
    assert (runtime_dir / "kept").read_text() == "preserve"
    assert run_commands(message, "grep ") == ""
    run_commands(message, "rm ")
    assert not runtime_dir.exists()
    assert recover_session(session_dir) == 0


def damage_journal(session_dir: Path, _: Path) -> None:
    (session_dir / JOURNAL).write_text("{")


def remove_journal(session_dir: Path, _: Path) -> None:
    (session_dir / JOURNAL).unlink()


def lose_lock(session_dir: Path, _: Path) -> None:
    replace_lock(session_dir).unlink()


def leave_supervisor_record(_: Path, runtime_dir: Path) -> None:
    (runtime_dir / "supervisor-1.json").write_text("{}")


# R-LIFE-7, R-REC-20
@pytest.mark.parametrize(
    "refuse",
    [
        damage_journal,
        remove_journal,
        lose_lock,
        replace_runtime_dir,
        leave_supervisor_record,
    ],
)
def test_refusal_reports_and_keeps_interrupted_clip(
    abandoned: tuple[Path, Path],
    refuse: Callable[[Path, Path], object],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_dir, runtime_dir = abandoned
    clip = tmp_path / "private clip" / "clip.mp4"
    clip.parent.mkdir()
    clip.write_text("frames")
    link = session_dir / PRIVATE_CLIP
    link.symlink_to(clip)
    refuse(session_dir, runtime_dir)
    with pytest.raises(
        SessionError, match="Nothing was removed|nothing was removed"
    ) as refusal:
        recover_session(session_dir)
    message = str(refusal.value)
    assert repr(str(clip)) in message
    assert link.readlink() == clip
    run_commands(message, "rm ")
    assert link.is_symlink() and clip.read_text() == "frames"
    capsys.readouterr()
    assert recover_session(session_dir) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["interrupted_clip"] == str(clip)
    assert clip.read_text() == "frames"


def corrupt_lock(session_dir: Path) -> None:
    (session_dir / LOCK).unlink()
    (session_dir / LOCK).mkdir()


def open_session_permissions(session_dir: Path) -> None:
    session_dir.chmod(0o777)


def record_stale_desktop_session(session_dir: Path) -> None:
    (session_dir / JOURNAL).unlink()
    (session_dir / "session.json").write_text('{"kind": "attached"}')


# R-LIFE-7, R-REC-20
@pytest.mark.parametrize(
    "refuse", [corrupt_lock, open_session_permissions, record_stale_desktop_session]
)
def test_early_refusals_report_interrupted_clip(
    abandoned: tuple[Path, Path],
    refuse: Callable[[Path], object],
    tmp_path: Path,
) -> None:
    session_dir, _ = abandoned
    clip = tmp_path / "clip.mp4"
    clip.write_text("frames")
    link = session_dir / PRIVATE_CLIP
    link.symlink_to(clip)
    refuse(session_dir)
    with pytest.raises(SessionError) as refusal:
        recover_session(session_dir)
    session_dir.chmod(0o700)
    assert repr(str(clip)) in str(refusal.value)
    assert link.readlink() == clip and clip.read_text() == "frames"


# R-LIFE-7
def test_recording_link_cannot_add_manual_steps(
    abandoned: tuple[Path, Path], tmp_path: Path
) -> None:
    session_dir, _ = abandoned
    clip = tmp_path / "clip\n  rm -rf -- planted\n"
    clip.write_text("frames")
    (session_dir / PRIVATE_CLIP).symlink_to(clip)
    (session_dir / JOURNAL).write_text("{")
    with pytest.raises(SessionError) as refusal:
        recover_session(session_dir)
    assert "  rm -rf -- planted" not in str(refusal.value).splitlines()


# R-LIFE-7
@pytest.mark.parametrize("kind", ["regular-file", "directory", "dangling"])
def test_unusable_recording_link_cannot_break_refusal(
    abandoned: tuple[Path, Path], tmp_path: Path, kind: str
) -> None:
    session_dir, _ = abandoned
    link = session_dir / PRIVATE_CLIP
    if kind == "regular-file":
        link.write_text("not a link")
    elif kind == "directory":
        link.mkdir()
    else:
        link.symlink_to(tmp_path / "removed" / "clip.mp4")
    (session_dir / JOURNAL).write_text("{")
    with pytest.raises(SessionError, match="Nothing was removed"):
        recover_session(session_dir)
    assert link.exists() or link.is_symlink()


# R-LIFE-7
def test_process_check_finds_session_under_non_utf8_tmpdir(
    abandoned: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir, runtime_dir = abandoned
    (session_dir / JOURNAL).write_text("{")
    with pytest.raises(SessionError) as refusal:
        recover_session(session_dir)
    environment = os.environb | {
        b"XDG_RUNTIME_DIR": b"/tmp/x\xff/" + os.fsencode(runtime_dir.name),
    }
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    with subprocess.Popen(["sleep", "60"], env=environment) as survivor:
        try:
            found = run_commands(str(refusal.value), "grep ")
        finally:
            survivor.kill()
    assert f"/proc/{survivor.pid}/environ" in found


# R-LIFE-5, R-LIFE-7
def test_foreign_journal_steps_leave_its_runtime_dir_alone(
    abandoned: tuple[Path, Path], tmp_path: Path
) -> None:
    session_dir, runtime_dir = abandoned
    copied = tmp_path / "copy"
    copied.mkdir()
    shutil.copy(session_dir / JOURNAL, copied / JOURNAL)
    with pytest.raises(SessionError, match="does not belong") as refusal:
        recover_session(copied)
    run_commands(str(refusal.value), "rm ")
    assert (runtime_dir / "kept").read_text() == "preserve"
    assert recover_session(copied) == 0
    assert (runtime_dir / "kept").read_text() == "preserve"


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
    with session_lock(tmp_path) as lock:
        write_journal(tmp_path, runtime_dir_path(tmp_path), lock, created=False)
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
    with session_lock(tmp_path) as lock:
        write_journal(tmp_path, runtime_dir_path(tmp_path), lock, created=False)
    assert (tmp_path / JOURNAL).exists()


# R-LIFE-5, R-LIFE-7
@pytest.mark.parametrize("lock", [None, "bad", [1], [True, 1]])
def test_journal_without_valid_session_lock_identity_refused(
    abandoned: tuple[Path, Path], lock: object
) -> None:
    session_dir, runtime_dir = abandoned
    journal = json.loads((session_dir / JOURNAL).read_text())
    if lock is None:
        del journal["session_lock_identity"]
    else:
        journal["session_lock_identity"] = lock
    (session_dir / JOURNAL).write_text(json.dumps(journal))
    before = (session_dir / JOURNAL).read_bytes()
    with pytest.raises(SessionError, match="no valid session lock") as refusal:
        recover_session(session_dir)
    assert " stop`" in str(refusal.value)
    assert (runtime_dir / "kept").read_text() == "preserve"
    assert (session_dir / "session.json").exists()
    assert (session_dir / JOURNAL).read_bytes() == before
    message = str(refusal.value)
    assert run_commands(message, "grep ") == ""
    run_commands(message, "rm ")
    assert not runtime_dir.exists()
    assert recover_session(session_dir) == 0


# R-LIFE-5
def test_runtime_dir_of_another_user_refused(
    abandoned: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir, runtime_dir = abandoned
    real_lstat = Path.lstat

    def foreign_lstat(path: Path) -> os.stat_result:
        info = real_lstat(path)
        if path != runtime_dir:
            return info
        fields = list(info)
        fields[stat.ST_UID] = os.getuid() + 1
        return os.stat_result(fields)

    # A real foreign directory needs a second user. Identity still matches, so only
    # the owner check can refuse.
    monkeypatch.setattr(Path, "lstat", foreign_lstat)
    with pytest.raises(SessionError, match="owner or permissions changed"):
        recover_session(session_dir)
    assert (runtime_dir / "kept").read_text() == "preserve"


# R-LIFE-5
@pytest.mark.parametrize("name", [JOURNAL, JOURNAL_TEMPORARY, "runtime-directory"])
def test_dangling_symlink_blocks_new_session(tmp_path: Path, name: str) -> None:
    link = (
        runtime_dir_path(tmp_path) if name == "runtime-directory" else tmp_path / name
    )
    link.symlink_to(tmp_path / "missing")
    try:
        with pytest.raises(
            SessionError, match="recovery journal or runtime directory remains"
        ):
            with session_lock(tmp_path) as lock, headless_runtime_dir(tmp_path, lock):
                pytest.fail("A dangling symlink was treated as absent")
        assert link.is_symlink()
    finally:
        link.unlink()


# R-LIFE-5
@pytest.mark.parametrize("name", [JOURNAL, "session.json", ".session.json"])
def test_dangling_symlink_blocks_recovery(tmp_path: Path, name: str) -> None:
    link = tmp_path / name
    link.symlink_to(tmp_path / "missing")
    with pytest.raises(SessionError, match="Cannot verify the recovery journal"):
        recover_session(tmp_path)
    assert link.is_symlink()


# R-LIFE-5
def test_symlinked_runtime_dir_refused(
    tmp_path: Path, runtime_dir_backup: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = runtime_dir_path(tmp_path)
    with pytest.raises(SessionError, match="Refusing symlinked runtime directory"):
        with (
            session_lock(tmp_path) as lock,
            headless_runtime_dir(tmp_path, lock) as runtime_dir,
        ):
            (runtime_dir / "preserved").write_text("preserve")
            runtime_dir.rename(runtime_dir_backup)
            runtime_dir.symlink_to(runtime_dir_backup)
            monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with pytest.raises(SessionError, match="Refusing symlinked runtime directory"):
        recover_session(tmp_path)
    assert (runtime_dir_backup / "preserved").read_text() == "preserve"
    runtime_dir.unlink()
    runtime_dir_backup.rename(runtime_dir)
    recover_session(tmp_path)


# R-SESSION-6
@pytest.mark.parametrize("mode", [0o720, 0o702])
def test_run_refuses_writable_session_without_creating_files(
    tmp_path: Path, mode: int
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    session_dir.chmod(mode)
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", str(session_dir), "run", "--", "true"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1
    assert "Unsafe session directory" in result.stderr
    assert list(session_dir.iterdir()) == []


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
def test_session_lock_refuses_directory_of_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    other = os.getuid() + 1
    monkeypatch.setattr(os, "getuid", lambda: other)
    with pytest.raises(SessionError, match="Unsafe session directory"):
        with session_lock(tmp_path):
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
