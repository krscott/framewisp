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
from framewisp.ownership import (
    ACTIVE_RECORDING,
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
def test_live_owner_and_copied_or_damaged_metadata(tmp_path: Path) -> None:
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


# R-LIFE-7
def test_incomplete_cleanup_preserves_resources_and_diagnostics(tmp_path: Path) -> None:
    runtime = runtime_path(tmp_path)
    with pytest.raises(SessionError, match="Cleanup is incomplete"):
        with (
            session_lease(tmp_path) as lease,
            headless_runtime(tmp_path, lease) as runtime,
        ):
            (runtime / "owner-123.json").write_text('{"error": "signal denied"}')
            (tmp_path / "app.log").write_text("diagnostic")
    with pytest.raises(SessionError, match="owner-123.json") as refusal:
        recover_session(tmp_path)
    assert "rm -rf" in str(refusal.value)
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
@pytest.mark.parametrize("kind", ["0644", "fifo"])
def test_openable_replacement_lock_refused_while_session_live(
    tmp_path: Path, kind: str
) -> None:
    lock = tmp_path / LOCK
    with session_lease(tmp_path) as lease, headless_runtime(tmp_path, lease) as runtime:
        lock.rename(tmp_path / "original.lock")
        if kind == "0644":
            lock.touch(mode=0o644)
        else:
            os.mkfifo(lock, 0o600)
        with pytest.raises(SessionError, match="was replaced"):
            recover_session(tmp_path)
        assert runtime.is_dir()
    assert not runtime.exists()


# R-LIFE-5
def test_hard_linked_lock_is_the_same_lock_while_session_live(tmp_path: Path) -> None:
    lock = tmp_path / LOCK
    with session_lease(tmp_path) as lease, headless_runtime(tmp_path, lease) as runtime:
        original = tmp_path / "original.lock"
        lock.rename(original)
        os.link(original, lock)
        with pytest.raises(SessionError, match="still live"):
            recover_session(tmp_path)
        assert runtime.is_dir()
    assert not runtime.exists()


# R-LIFE-5, R-LIFE-7
@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_unusable_replacement_lock_refused_while_session_live(
    tmp_path: Path, kind: str
) -> None:
    lock = tmp_path / LOCK
    with session_lease(tmp_path) as lease, headless_runtime(tmp_path, lease) as runtime:
        original = tmp_path / "original.lock"
        lock.rename(original)
        if kind == "symlink":
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


# R-LIFE-4
def test_permissive_chmod_does_not_block_recovery(
    abandoned: tuple[Path, Path],
) -> None:
    session, runtime = abandoned
    subprocess.run(["chmod", "-R", "go+rX", session], check=True)
    assert stat.S_IMODE((session / LOCK).stat().st_mode) & 0o044
    assert recover_session(session) == 0
    assert not runtime.exists()


# R-LIFE-4, R-LIFE-5
def test_hard_linked_copy_refused_and_original_recovers(
    abandoned: tuple[Path, Path], tmp_path: Path
) -> None:
    session, runtime = abandoned
    copied = tmp_path / "copy"
    subprocess.run(["cp", "-al", session, copied], check=True)
    assert (session / LOCK).stat().st_nlink == 2
    assert (copied / JOURNAL).stat().st_ino == (session / JOURNAL).stat().st_ino
    with pytest.raises(SessionError, match="does not belong"):
        recover_session(copied)
    assert (runtime / "kept").read_text() == "preserve"
    assert recover_session(session) == 0
    assert not runtime.exists()
    with pytest.raises(SessionError, match="does not belong"):
        recover_session(copied)


# R-LIFE-5
def test_replaced_lock_refused_after_permissive_chmod(
    abandoned: tuple[Path, Path],
) -> None:
    session, runtime = abandoned
    replace_lock(session)
    subprocess.run(["chmod", "-R", "go+rX", session], check=True)
    with pytest.raises(SessionError, match="was replaced"):
        recover_session(session)
    assert (runtime / "kept").read_text() == "preserve"


# R-LIFE-5, R-LIFE-7
@pytest.mark.skipif(os.getuid() == 0, reason="Root can open any file")
def test_lock_the_owner_cannot_open_gives_steps(abandoned: tuple[Path, Path]) -> None:
    session, runtime = abandoned
    (session / LOCK).chmod(0)
    with pytest.raises(SessionError, match="is unusable") as refusal:
        recover_session(session)
    assert "chmod 600" in str(refusal.value)
    assert (runtime / "kept").read_text() == "preserve"
    (session / LOCK).chmod(0o600)
    assert recover_session(session) == 0


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


# R-LIFE-5, R-LIFE-7
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


def remove_journal_field(session: Path, field: str) -> None:
    journal = json.loads((session / JOURNAL).read_text())
    del journal[field]
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
    "no-runtime-identity": lambda s, _: remove_journal_field(s, "runtime"),
    "no-runtime-path": lambda s, _: remove_journal_field(s, "runtime_path"),
}


# R-LIFE-5, R-LIFE-7
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


# R-LIFE-5, R-LIFE-7
def test_fifo_journal_with_a_writer_refused_with_steps(
    abandoned: tuple[Path, Path],
) -> None:
    session, runtime = abandoned
    (session / JOURNAL).unlink()
    os.mkfifo(session / JOURNAL, 0o600)
    writer = os.open(session / JOURNAL, os.O_RDWR)
    try:
        with pytest.raises(SessionError, match="must be a regular file"):
            recover_session(session)
    finally:
        os.close(writer)
    assert (runtime / "kept").read_text() == "preserve"


# R-LIFE-5, R-LIFE-7
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


# R-LIFE-5, R-LIFE-7
def test_journal_without_runtime_path_refused_outside_default_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    session = tmp_path / "session"
    session.mkdir()
    runtime = runtime_path(session)
    with session_lease(session) as lease:
        write_journal(session, runtime, lease, created=False)
        runtime.mkdir(mode=0o700)
        write_journal(session, runtime, lease, created=True)
    (runtime / "owner-123.json").write_text("{}")
    remove_journal_field(session, "runtime_path")
    with pytest.raises(SessionError, match="no runtime path") as refusal:
        recover_session(session)
    assert str(runtime) in str(refusal.value)
    assert (session / JOURNAL).exists()
    assert (runtime / "owner-123.json").exists()


# R-LIFE-5, R-LIFE-7
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


def replace_runtime(session: Path, runtime: Path) -> None:
    runtime.rename(session / "moved-runtime")
    runtime.mkdir(mode=0o700)
    (runtime / "kept").write_text("preserve")


def symlink_runtime(session: Path, runtime: Path) -> None:
    runtime.rename(session / "moved-runtime")
    runtime.symlink_to(session / "moved-runtime")


def open_runtime_permissions(_: Path, runtime: Path) -> None:
    runtime.chmod(0o755)


# R-LIFE-5, R-LIFE-7
@pytest.mark.parametrize(
    "change",
    [replace_runtime, symlink_runtime, open_runtime_permissions],
    ids=["replaced", "symlink", "permissions"],
)
def test_changed_runtime_refused_with_working_manual_steps(
    abandoned: tuple[Path, Path],
    change: Callable[[Path, Path], object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, runtime = abandoned
    change(session, runtime)
    # The journal's verified path applies even under another TMPDIR.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with pytest.raises(SessionError, match="Nothing was removed") as refusal:
        recover_session(session)
    message = str(refusal.value)
    assert " stop`" in message and shlex.quote(str(runtime)) in message
    assert (session / JOURNAL).exists()
    assert (runtime / "kept").read_text() == "preserve"
    assert run_commands(message, "grep ") == ""
    run_commands(message, "rm ")
    assert not runtime.exists() and not runtime.is_symlink()
    assert recover_session(session) == 0


# R-LIFE-7
def test_supervisor_record_names_cannot_add_manual_steps(
    abandoned: tuple[Path, Path],
) -> None:
    session, runtime = abandoned
    (runtime / "owner-x\n  rm -rf -- planted\n.json").write_text("{}")
    with pytest.raises(SessionError, match="Cleanup is incomplete") as refusal:
        recover_session(session)
    assert "  rm -rf -- planted" not in str(refusal.value).splitlines()


# R-LIFE-5, R-LIFE-7
def test_null_runtime_identity_steps_use_the_journal_path(
    abandoned: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, runtime = abandoned
    rewrite_journal(session, runtime=None)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with pytest.raises(SessionError, match="no runtime identity") as refusal:
        recover_session(session)
    message = str(refusal.value)
    assert " stop`" in message and (runtime / "kept").read_text() == "preserve"
    assert (session / JOURNAL).exists()
    run_commands(message, "rm ")
    assert not runtime.exists()
    assert recover_session(session) == 0


# R-LIFE-7
def test_stale_attachment_gets_attachment_steps(tmp_path: Path) -> None:
    (tmp_path / "session.json").write_text('{"kind": "attached"}')
    with pytest.raises(SessionError, match="--detach") as refusal:
        recover_session(tmp_path)
    message = str(refusal.value)
    assert " run -- APP" not in message
    assert (tmp_path / "session.json").exists()
    run_commands(message, "rm ")
    assert recover_session(tmp_path) == 0


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
    session, runtime = abandoned
    lock = session / LOCK
    real_flock = fcntl.flock

    def change_lock_then_flock(descriptor: int, operation: int) -> None:
        real_flock(descriptor, operation)
        lock.unlink()
        lock.touch(mode=0o600)

    # flock excluded every holder of the journal's lock, so no owner can remain.
    monkeypatch.setattr(fcntl, "flock", change_lock_then_flock)
    assert recover_session(session) == 0
    assert not runtime.exists()


def run_missing_session_commands(message: str, runtime: Path) -> None:
    # The lists are compared by name, so each must print the bare name.
    assert runtime.name in run_commands(message, "find ").splitlines()
    assert runtime.name not in run_commands(message, "grep ").splitlines()
    environment = os.environ | {"XDG_RUNTIME_DIR": str(runtime)}
    with subprocess.Popen(["sleep", "60"], env=environment) as survivor:
        try:
            used = run_commands(message, "grep ")
        finally:
            survivor.kill()
    assert runtime.name in used.splitlines()
    # A starting session has no marked process, so the lists cannot authorize rm.
    assert "If unsure, leave it" in " ".join(message.split())
    assert not [line for line in message.splitlines() if line.strip().startswith("rm ")]


# R-LIFE-7
def test_deleted_session_refusal_says_what_is_left_to_check(
    abandoned: tuple[Path, Path],
) -> None:
    session, runtime = abandoned
    shutil.rmtree(session)
    with pytest.raises(SessionError, match="does not exist") as refusal:
        recover_session(session)
    assert (runtime / "kept").read_text() == "preserve"
    run_missing_session_commands(str(refusal.value), runtime)


# R-LIFE-7
def test_moved_session_recovers_at_its_new_path(
    abandoned: tuple[Path, Path], tmp_path: Path
) -> None:
    session, runtime = abandoned
    moved = tmp_path / "moved"
    session.rename(moved)
    with pytest.raises(SessionError, match="does not exist") as refusal:
        recover_session(session)
    assert "moved" in str(refusal.value)
    assert recover_session(moved) == 0
    assert not runtime.exists()


# R-LIFE-7
def test_session_deleted_during_shutdown_keeps_runtime_and_gives_steps(
    tmp_path: Path,
) -> None:
    session = tmp_path / "session"
    session.mkdir()
    runtime = runtime_path(session)
    try:
        with pytest.raises(SessionError, match="does not exist") as refusal:
            with (
                session_lease(session) as lease,
                headless_runtime(session, lease),
            ):
                (runtime / "kept").write_text("preserve")
                shutil.rmtree(session)
        assert (runtime / "kept").read_text() == "preserve"
        run_missing_session_commands(str(refusal.value), runtime)
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


# R-LIFE-7
def test_session_vanishing_during_lease_gets_steps(
    abandoned: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    session, runtime = abandoned
    real_open = os.open

    def remove_session_then_open(path: Path, flags: int, mode: int = 0o777) -> int:
        session.rename(session.with_name("moved"))
        return real_open(path, flags, mode)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", remove_session_then_open)
        with pytest.raises(SessionError, match="does not exist") as refusal:
            recover_session(session)
    run_missing_session_commands(str(refusal.value), runtime)


# R-LIFE-7
@pytest.mark.skipif(os.getuid() == 0, reason="Root can write a read-only directory")
@pytest.mark.parametrize("journal", ["unreadable", "missing"])
def test_read_only_tmpdir_is_still_checked_for_runtime(
    tmp_path: Path, journal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setattr(tempfile, "tempdir", None)
    session = tmp_path / "session"
    session.mkdir()
    runtime = runtime_path(session)
    with session_lease(session) as lease:
        write_journal(session, runtime, lease, created=False)
        runtime.mkdir(mode=0o700)
        write_journal(session, runtime, lease, created=True)
    (runtime / "owner-1.json").write_text("{}")
    if journal == "missing":
        (session / JOURNAL).unlink()
    else:
        (session / JOURNAL).write_text("{")
    temporary.chmod(0o500)
    monkeypatch.setattr(tempfile, "tempdir", None)
    try:
        with pytest.raises(SessionError, match="Nothing was removed") as refusal:
            recover_session(session)
        assert f"rm -rf -- {runtime}" in str(refusal.value)
        assert (runtime / "owner-1.json").exists()
    finally:
        temporary.chmod(0o700)


# R-LIFE-7
@pytest.mark.skipif(os.getuid() == 0, reason="Root can write a read-only directory")
def test_runtime_found_when_tmpdir_was_read_only_from_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "configured"
    configured.mkdir(mode=0o500)
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setenv("TMPDIR", str(configured))
    monkeypatch.setattr(tempfile, "tempdir", str(fallback))
    session = tmp_path / "session"
    session.mkdir()
    runtime = runtime_path(session)
    runtime.mkdir(mode=0o700)
    (runtime / "owner-1.json").write_text("{}")
    with pytest.raises(SessionError, match="journal is missing") as refusal:
        recover_session(session)
    assert f"rm -rf -- {runtime}" in str(refusal.value)


# R-LIFE-7
@pytest.mark.skipif(os.getuid() == 0, reason="Root can search any directory")
def test_inaccessible_tmpdir_still_gives_steps(
    abandoned: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, runtime = abandoned
    (session / JOURNAL).write_text("{")
    closed = tmp_path / "closed"
    (closed / "temporary").mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(closed / "temporary"))
    closed.chmod(0)
    try:
        with pytest.raises(SessionError, match="Nothing was removed") as refusal:
            recover_session(session)
        assert " stop`" in str(refusal.value) and str(runtime) in str(refusal.value)
    finally:
        closed.chmod(0o700)


# R-LIFE-7
@pytest.mark.skipif(os.getuid() == 0, reason="Root can search any directory")
def test_recovery_refuses_when_a_temporary_location_cannot_be_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "session"
    session.mkdir()
    (session / ".headless-owner.tmp").write_text("{")
    closed = tmp_path / "closed"
    (closed / "temporary").mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(closed / "temporary"))
    closed.chmod(0)
    try:
        with pytest.raises(SessionError, match="journal is missing") as refusal:
            recover_session(session)
        assert (session / ".headless-owner.tmp").exists()
        assert "Restore access, then" in " ".join(str(refusal.value).split())
        # Nothing ran under this TMPDIR, so the printed alternative applies.
        monkeypatch.delenv("TMPDIR")
        assert recover_session(session) == 0
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
    session = tmp_path / "session"
    session.mkdir()
    runtime = tmp_path / "relative" / runtime_path(session).name
    runtime.mkdir(mode=0o700)
    (tmp_path / "relative").chmod(0o500)
    monkeypatch.setattr(tempfile, "tempdir", None)
    try:
        with pytest.raises(SessionError, match="journal is missing") as refusal:
            recover_session(session)
        assert str(runtime) in str(refusal.value)
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
    session = tmp_path / "session"
    session.mkdir()
    runtime = runtime_path(session)
    runtime.mkdir(mode=0o700)
    shutil.rmtree(session)
    with pytest.raises(SessionError, match="does not exist") as refusal:
        recover_session(session)
    assert runtime.name in run_commands(str(refusal.value), "find ").splitlines()


# R-LIFE-7
def test_relative_tmpdir_with_deleted_cwd_still_gives_steps(
    abandoned: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, runtime = abandoned
    (session / JOURNAL).write_text("{")
    monkeypatch.setenv("TMPDIR", "relative")
    monkeypatch.setattr(tempfile, "tempdir", str(runtime.parent))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    cwd.rmdir()
    with pytest.raises(SessionError, match="Nothing was removed") as refusal:
        recover_session(session)
    assert str(runtime) in str(refusal.value)


def refuse_tempdir() -> str:
    raise FileNotFoundError("No usable temporary directory found")


# R-LIFE-7
@pytest.mark.parametrize("journal", ["unreadable", "missing"])
def test_unusable_tempdir_refusal_gives_working_manual_steps(
    abandoned: tuple[Path, Path], journal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, runtime = abandoned
    if journal == "missing":
        (session / JOURNAL).unlink()
    else:
        (session / JOURNAL).write_text("{")
    monkeypatch.setenv("TMPDIR", str(runtime.parent))
    monkeypatch.setattr(tempfile, "gettempdir", refuse_tempdir)
    with pytest.raises(SessionError, match="Nothing was removed") as refusal:
        recover_session(session)
    message = str(refusal.value)
    assert (runtime / "kept").read_text() == "preserve"
    assert run_commands(message, "grep ") == ""
    run_commands(message, "rm ")
    assert not runtime.exists()
    assert recover_session(session) == 0


def damage_journal(session: Path, _: Path) -> None:
    (session / JOURNAL).write_text("{")


def remove_journal(session: Path, _: Path) -> None:
    (session / JOURNAL).unlink()


def lose_lock(session: Path, _: Path) -> None:
    replace_lock(session).unlink()


def leave_supervisor_record(_: Path, runtime: Path) -> None:
    (runtime / "owner-1.json").write_text("{}")


# R-LIFE-7, R-REC-20
@pytest.mark.parametrize(
    "refuse",
    [
        damage_journal,
        remove_journal,
        lose_lock,
        replace_runtime,
        leave_supervisor_record,
    ],
)
def test_refusal_reports_and_keeps_interrupted_recording(
    abandoned: tuple[Path, Path],
    refuse: Callable[[Path, Path], object],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session, runtime = abandoned
    clip = tmp_path / "private clip" / "recording.mp4"
    clip.parent.mkdir()
    clip.write_text("frames")
    link = session / ACTIVE_RECORDING
    link.symlink_to(clip)
    refuse(session, runtime)
    with pytest.raises(
        SessionError, match="Nothing was removed|nothing was removed"
    ) as refusal:
        recover_session(session)
    message = str(refusal.value)
    assert repr(str(clip)) in message
    assert link.readlink() == clip
    run_commands(message, "rm ")
    assert link.is_symlink() and clip.read_text() == "frames"
    capsys.readouterr()
    assert recover_session(session) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["interrupted_recording"] == str(clip)
    assert clip.read_text() == "frames"


def corrupt_lock(session: Path) -> None:
    (session / LOCK).unlink()
    (session / LOCK).mkdir()


def open_session_permissions(session: Path) -> None:
    session.chmod(0o777)


def record_stale_attachment(session: Path) -> None:
    (session / JOURNAL).unlink()
    (session / "session.json").write_text('{"kind": "attached"}')


# R-LIFE-7, R-REC-20
@pytest.mark.parametrize(
    "refuse", [corrupt_lock, open_session_permissions, record_stale_attachment]
)
def test_early_refusals_report_interrupted_recording(
    abandoned: tuple[Path, Path],
    refuse: Callable[[Path], object],
    tmp_path: Path,
) -> None:
    session, _ = abandoned
    clip = tmp_path / "recording.mp4"
    clip.write_text("frames")
    link = session / ACTIVE_RECORDING
    link.symlink_to(clip)
    refuse(session)
    with pytest.raises(SessionError) as refusal:
        recover_session(session)
    session.chmod(0o700)
    assert repr(str(clip)) in str(refusal.value)
    assert link.readlink() == clip and clip.read_text() == "frames"


# R-LIFE-7
def test_recording_link_cannot_add_manual_steps(
    abandoned: tuple[Path, Path], tmp_path: Path
) -> None:
    session, _ = abandoned
    clip = tmp_path / "clip\n  rm -rf -- planted\n"
    clip.write_text("frames")
    (session / ACTIVE_RECORDING).symlink_to(clip)
    (session / JOURNAL).write_text("{")
    with pytest.raises(SessionError) as refusal:
        recover_session(session)
    assert "  rm -rf -- planted" not in str(refusal.value).splitlines()


# R-LIFE-7
@pytest.mark.parametrize("kind", ["regular-file", "directory", "dangling"])
def test_unusable_recording_link_cannot_break_refusal(
    abandoned: tuple[Path, Path], tmp_path: Path, kind: str
) -> None:
    session, _ = abandoned
    link = session / ACTIVE_RECORDING
    if kind == "regular-file":
        link.write_text("not a link")
    elif kind == "directory":
        link.mkdir()
    else:
        link.symlink_to(tmp_path / "removed" / "recording.mp4")
    (session / JOURNAL).write_text("{")
    with pytest.raises(SessionError, match="Nothing was removed"):
        recover_session(session)
    assert link.exists() or link.is_symlink()


# R-LIFE-7
def test_process_check_finds_session_under_non_utf8_tmpdir(
    abandoned: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    session, runtime = abandoned
    (session / JOURNAL).write_text("{")
    with pytest.raises(SessionError) as refusal:
        recover_session(session)
    environment = os.environb | {
        b"XDG_RUNTIME_DIR": b"/tmp/x\xff/" + os.fsencode(runtime.name),
    }
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    with subprocess.Popen(["sleep", "60"], env=environment) as survivor:
        try:
            found = run_commands(str(refusal.value), "grep ")
        finally:
            survivor.kill()
    assert f"/proc/{survivor.pid}/environ" in found


# R-LIFE-5, R-LIFE-7
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


# R-LIFE-5, R-LIFE-7
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
def test_runtime_of_another_user_refused(
    abandoned: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    session, runtime = abandoned
    real_lstat = Path.lstat

    def foreign_lstat(path: Path) -> os.stat_result:
        info = real_lstat(path)
        if path != runtime:
            return info
        fields = list(info)
        fields[stat.ST_UID] = os.getuid() + 1
        return os.stat_result(fields)

    # A real foreign directory needs a second user. Identity still matches, so only
    # the owner check can refuse.
    monkeypatch.setattr(Path, "lstat", foreign_lstat)
    with pytest.raises(SessionError, match="ownership or permissions changed"):
        recover_session(session)
    assert (runtime / "kept").read_text() == "preserve"


# R-LIFE-5
@pytest.mark.parametrize("name", [JOURNAL, ".headless-owner.tmp", "runtime"])
def test_dangling_symlink_blocks_new_session(tmp_path: Path, name: str) -> None:
    link = runtime_path(tmp_path) if name == "runtime" else tmp_path / name
    link.symlink_to(tmp_path / "missing")
    try:
        with pytest.raises(SessionError, match="Abandoned headless ownership"):
            with session_lease(tmp_path) as lease, headless_runtime(tmp_path, lease):
                pytest.fail("A dangling symlink was treated as absent")
        assert link.is_symlink()
    finally:
        link.unlink()


# R-LIFE-5
@pytest.mark.parametrize("name", [JOURNAL, "session.json", ".session.json"])
def test_dangling_symlink_blocks_recovery(tmp_path: Path, name: str) -> None:
    link = tmp_path / name
    link.symlink_to(tmp_path / "missing")
    with pytest.raises(SessionError, match="Cannot verify ownership"):
        recover_session(tmp_path)
    assert link.is_symlink()


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
