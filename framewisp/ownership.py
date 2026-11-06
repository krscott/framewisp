"""Kernel leases and a path-bound journal for headless crash recovery."""

import errno
import fcntl
import json
import os
import shlex
import shutil
import stat
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from framewisp.errors import SessionError

LOCK = ".headless.lock"
JOURNAL = ".headless-owner.json"
# Far above any journal, whose runtime path fits in a 108-byte socket address.
MAX_JOURNAL_BYTES = 1 << 16


@dataclass(frozen=True)
class Journal:
    """A verified ownership journal. A null identity means mkdir may be uncommitted."""

    runtime: Path
    runtime_identity: list[int] | None


def identity(path: Path) -> list[int]:
    info = path.lstat()
    return [info.st_dev, info.st_ino]


def parse_identity(value: object) -> list[int] | None:
    parts = cast(list[object], value) if isinstance(value, list) else []
    if len(parts) != 2 or not all(type(part) is int for part in parts):
        return None
    return cast(list[int], parts)


def lease_identity(lease: int) -> list[int]:
    # A held descriptor pins its inode, so a matching identity cannot be a reused
    # inode number while any owner still holds the lease.
    info = os.fstat(lease)
    return [info.st_dev, info.st_ino]


def runtime_name(session: Path) -> str:
    device, inode = identity(session)
    return f"framewisp-{os.getuid()}-{device:x}-{inode:x}"


def runtime_path(session: Path) -> Path:
    return Path(tempfile.gettempdir()) / runtime_name(session)


def check_socket_paths(runtime: Path) -> None:
    # Sway names its IPC socket with a UID and signed 32-bit PID. Budget for
    # every possible PID before starting any children, plus the terminating NUL.
    socket_path = runtime / f"sway-ipc.{os.getuid()}.2147483647.sock"
    size = len(os.fsencode(socket_path)) + 1
    if size > 108:
        raise SessionError(
            f"Runtime directory {runtime} makes socket paths up to {size} bytes "
            "including the terminating NUL, over the 108-byte limit. "
            "Set TMPDIR to a shorter directory."
        )


def journal_runtime(session: Path, journal: dict[str, object]) -> Path:
    """Return the journal's runtime path, or raise ValueError or OSError."""
    value = journal.get("runtime_path")
    if not isinstance(value, str) or "\0" in value:
        raise ValueError("journal runtime path must be an absolute path")
    runtime = Path(value)
    if not runtime.is_absolute() or runtime.name != runtime_name(session):
        raise ValueError(f"journal has an invalid runtime path: {value!r}")
    # Path.exists() reports a path that cannot be checked as missing, which would
    # read as a finished cleanup. Only a confirmed absence may.
    try:
        runtime.lstat()
    except FileNotFoundError:
        pass
    return runtime


def unusable_lock(lock: Path, problem: str) -> str:
    recover = shlex.join(["framewisp", str(lock.parent), "recover"])
    links = shlex.join(["find", str(lock.parent), "-samefile", str(lock)])
    return (
        f"Session lock {lock} is unusable ({problem}): it must be a private regular "
        "file with one link. It may have been replaced, so nothing was changed. "
        f"If only its mode changed, run chmod 600 on it. If it has extra links, "
        f"find them with `{links}` and delete all but the lock. Otherwise move it "
        "aside. "
        f"Then run `{recover}`, which checks the lock against the session's "
        "ownership journal and reports what to do next."
    )


@contextmanager
def session_lease(session: Path) -> Generator[int, None, None]:
    directory = session.lstat()
    if (
        not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != os.getuid()
        or directory.st_mode & 0o022
    ):
        raise SessionError(
            f"Unsafe session directory: {session}. Use a directory you own without group or other write permission (chmod 700)."
        )
    lock = session / LOCK
    try:
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        # Other errors, such as an unwritable directory, say nothing about the lock.
        if error.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO) or (
            error.errno == errno.EACCES and lock.exists()
        ):
            raise SessionError(unusable_lock(lock, str(error.strerror))) from error
        raise
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            problem = f"{stat.filemode(info.st_mode)}, {info.st_nlink} links"
            raise SessionError(unusable_lock(lock, problem))
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SessionError(
                "Session owner or descendant cleanup is still live. Wait for cleanup and retry."
            ) from None
        if identity(session / LOCK) != lease_identity(descriptor):
            raise SessionError("Headless lock changed while acquiring ownership.")
        yield descriptor
    finally:
        # Supervisors share this open file description. Closing, rather than
        # unlocking, keeps the lease held until every supervisor has closed it.
        os.close(descriptor)


def cleanup_steps(session: Path, runtime: Path, *process_checks: str) -> str:
    """Describe manual cleanup for ownership that recovery cannot verify."""
    framewisp = shlex.join(["framewisp", str(session)])
    quoted = shlex.quote(str(runtime))
    # The directory's name is the same under any TMPDIR.
    environment = shlex.quote(f"^XDG_RUNTIME_DIR=.*/{runtime.name}$")
    checks = (*process_checks, f"grep -zlsE {environment} /proc/*/environ")
    metadata = ("session.json", ".session.json", JOURNAL, ".headless-owner.tmp")
    lines = [
        "Confirm the session has exited. Run these outside any sandbox, in the PID "
        "namespace that started the session, because they cannot see processes "
        f"elsewhere. `{framewisp} status` must not report running, and these "
        "commands must print nothing:",
        *(f"  {check}" for check in checks),
        "Inspect any supervisor records, which can report incomplete cleanup:",
        f"  cat {quoted}/owner-*.json",
        "Then delete its runtime directory and metadata, keeping logs:",
        f"  rm -rf -- {quoted}",
        "  " + shlex.join(["rm", "-rf", "--", *(str(session / m) for m in metadata)]),
        f"Then start again with `{framewisp} run -- APP`.",
    ]
    return "\n".join(lines)


def stop_step(session: Path) -> str:
    framewisp = shlex.join(["framewisp", str(session)])
    return f"If the session is running, stop it with `{framewisp} stop`.\n"


def unreadable_journal(session: Path, problem: str) -> str:
    # The journal is the only trustworthy record of the session's TMPDIR, and it is
    # unusable here. A path copied from it could also aim these steps at another
    # session, so they use the path derived from this session's identity.
    runtime = runtime_path(session)
    return (
        f"Cannot verify ownership at {session / JOURNAL}: {problem}\n"
        "Cannot prove the session has exited. Nothing was removed.\n"
        + stop_step(session)
        + f"Otherwise follow these steps, which assume TMPDIR is {runtime.parent}. "
        "If the session started with another TMPDIR, set it and run recover again "
        "for steps with that path. session.json, if present, records the path as "
        "runtime_directory.\n" + cleanup_steps(session, runtime)
    )


def verify_lock(session: Path, recorded: list[int], runtime: Path, lease: int) -> None:
    held = lease_identity(lease)
    if recorded == held:
        return
    lock = session / LOCK
    raise SessionError(
        f"Session lock {lock} was replaced: the ownership journal records "
        f"device:inode {recorded[0]}:{recorded[1]}, but found {held[0]}:{held[1]}. "
        "A live session may still hold the original lock, so nothing was removed.\n"
        f"{stop_step(session)}If you still have the original lock file, move it back "
        f"to {lock} and run recover again.\n"
        "If the original lock is lost:\n"
        + cleanup_steps(session, runtime, f"grep ':{recorded[1]} ' /proc/locks")
    )


def read_journal(session: Path, lease: int) -> Journal:
    """Return the verified journal, or refuse with manual cleanup steps."""
    path = session / JOURNAL
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor) as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
            ):
                raise ValueError("journal must be an owned regular file with one link")
            if info.st_size > MAX_JOURNAL_BYTES:
                raise ValueError(f"journal is over {MAX_JOURNAL_BYTES} bytes")
            value: object = json.load(source)
        if not isinstance(value, dict):
            raise ValueError("journal must be an object")
        journal = cast(dict[str, object], value)
        if journal.get("session") != identity(session):
            raise ValueError("journal does not belong to this session directory")
        runtime = journal_runtime(session, journal)
        if "runtime" not in journal:
            raise ValueError("journal has no runtime identity")
        runtime_identity = parse_identity(journal["runtime"])
        if runtime_identity is None and journal["runtime"] is not None:
            raise ValueError("journal has an invalid runtime identity")
        lock = parse_identity(journal.get("lock"))
        if lock is None:
            raise ValueError(
                "journal has no valid session lock identity. It may predate lock "
                "verification"
            )
    # Deeply nested JSON raises RecursionError.
    except (OSError, ValueError, RecursionError) as error:
        raise SessionError(unreadable_journal(session, str(error))) from error
    verify_lock(session, lock, runtime, lease)
    return Journal(runtime, runtime_identity)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    except OSError as error:
        # EINVAL means this filesystem cannot sync directories. Sessions still work
        # there, without power-loss durability.
        if error.errno != errno.EINVAL:
            raise
    finally:
        os.close(descriptor)


def write_journal(session: Path, runtime: Path, lease: int, *, created: bool) -> None:
    temporary = session / ".headless-owner.tmp"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as output:
        json.dump(
            {
                "session": identity(session),
                "lock": lease_identity(lease),
                "runtime_path": str(runtime),
                "runtime": identity(runtime) if created else None,
            },
            output,
        )
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(session / JOURNAL)
    sync_directory(session)


def remove_runtime(session: Path, lease: int) -> None:
    journal = read_journal(session, lease)
    runtime = journal.runtime
    if runtime.is_symlink():
        raise SessionError(f"Refusing symlink runtime: {runtime}")
    if runtime.exists():
        info = runtime.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise SessionError(f"Runtime ownership or permissions changed: {runtime}")
        if journal.runtime_identity is None:
            # Interruption between mkdir and committing its inode can only have
            # left an empty directory. Never recursively remove it without proof.
            try:
                runtime.rmdir()
            except OSError as error:
                raise SessionError(
                    unreadable_journal(
                        session,
                        f"journal records no runtime identity, but {runtime} "
                        f"cannot be removed: {error.strerror}",
                    )
                ) from error
        else:
            if journal.runtime_identity != identity(runtime):
                raise SessionError(
                    f"Runtime identity changed; resources remain at {runtime}"
                )
            pending = list(runtime.glob("owner-*.json"))
            if pending:
                raise SessionError(
                    f"Cleanup is incomplete; resources remain at {runtime}. "
                    f"Inspect supervisor records: {', '.join(str(path) for path in pending)}"
                )
            shutil.rmtree(runtime)


def refuse_abandoned_ownership(session: Path) -> None:
    runtime = runtime_path(session)
    if (
        (session / JOURNAL).exists()
        or (session / JOURNAL).is_symlink()
        or (session / ".headless-owner.tmp").exists()
        or (session / ".headless-owner.tmp").is_symlink()
        or runtime.exists()
        or runtime.is_symlink()
    ):
        raise SessionError(
            "Abandoned headless ownership remains. Run framewisp SESSION recover first."
        )


@contextmanager
def headless_runtime(session: Path, lease: int) -> Generator[Path, None, None]:
    runtime = runtime_path(session)
    refuse_abandoned_ownership(session)
    check_socket_paths(runtime)
    write_journal(session, runtime, lease, created=False)
    runtime.mkdir(mode=0o700)
    write_journal(session, runtime, lease, created=True)
    try:
        yield runtime
    finally:
        remove_runtime(session, lease)
        # Keep proof of ownership until metadata is gone, including if shutdown
        # is interrupted after removing the runtime.
        for name in ("session.json", ".session.json"):
            (session / name).unlink(missing_ok=True)
        (session / JOURNAL).unlink()


def recover_session(session: Path) -> int:
    with session_lease(session) as lease:
        if not (session / JOURNAL).exists() and not (session / JOURNAL).is_symlink():
            # Before the first journal commit, no runtime or children may exist.
            # A partial temporary journal is disposable, but stale session JSON
            # without an ownership journal belongs to an unsupported older run.
            if (
                runtime_path(session).exists()
                or runtime_path(session).is_symlink()
                or (session / "session.json").exists()
                or (session / "session.json").is_symlink()
                or (session / ".session.json").exists()
                or (session / ".session.json").is_symlink()
            ):
                raise SessionError(unreadable_journal(session, "journal is missing"))
        else:
            remove_runtime(session, lease)
        for name in ("session.json", ".session.json", ".headless-owner.tmp", JOURNAL):
            (session / name).unlink(missing_ok=True)
    print(
        json.dumps(
            {"session": str(session), "status": "recovered", "logs": "preserved"}
        )
    )
    return 0
