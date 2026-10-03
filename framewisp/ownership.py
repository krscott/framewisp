"""Kernel leases and a path-bound journal for headless crash recovery."""

import fcntl
import json
import os
import shutil
import stat
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from framewisp.errors import SessionError

LOCK = ".headless.lock"
JOURNAL = ".headless-owner.json"


def identity(path: Path) -> list[int]:
    info = path.lstat()
    return [info.st_dev, info.st_ino]


def runtime_path(session: Path) -> Path:
    device, inode = identity(session)
    return Path(f"/tmp/framewisp-{os.getuid()}-{device:x}-{inode:x}")


@contextmanager
def session_lease(session: Path) -> Generator[int, None, None]:
    descriptor = os.open(session / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise SessionError("Headless lock must be a regular file with one link.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SessionError(
                "Headless owner or descendant cleanup is still live. Wait for cleanup and retry."
            ) from None
        if identity(session / LOCK) != [info.st_dev, info.st_ino]:
            raise SessionError("Headless lock changed while acquiring ownership.")
        yield descriptor
    finally:
        # Supervisors share this open file description. Closing, rather than
        # unlocking, keeps the lease held until every supervisor has closed it.
        os.close(descriptor)


def read_journal(session: Path) -> dict[str, object]:
    path = session / JOURNAL
    if path.is_symlink():
        raise SessionError(f"Refusing symlink ownership journal: {path}")
    try:
        value: object = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("journal must be an object")
        journal = cast(dict[str, object], value)
        if journal.get("session") != identity(session):
            raise ValueError("journal does not belong to this session directory")
        return journal
    except (OSError, ValueError) as error:
        raise SessionError(f"Cannot verify ownership at {path}: {error}") from error


def write_journal(session: Path, runtime: Path | None) -> None:
    temporary = session / ".headless-owner.tmp"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as output:
        json.dump(
            {
                "session": identity(session),
                "runtime": identity(runtime) if runtime else None,
            },
            output,
        )
    temporary.replace(session / JOURNAL)


def remove_runtime(session: Path) -> None:
    journal = read_journal(session)
    runtime = runtime_path(session)
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
        if journal.get("runtime") is None:
            # Interruption between mkdir and committing its inode can only have
            # left an empty directory. Never recursively remove it without proof.
            runtime.rmdir()
        else:
            if journal.get("runtime") != identity(runtime):
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


@contextmanager
def headless_runtime(session: Path) -> Generator[Path, None, None]:
    runtime = runtime_path(session)
    if (
        (session / JOURNAL).exists()
        or (session / JOURNAL).is_symlink()
        or runtime.exists()
        or runtime.is_symlink()
    ):
        raise SessionError(
            "Abandoned headless ownership remains. Run framewisp SESSION recover first."
        )
    write_journal(session, None)
    runtime.mkdir(mode=0o700)
    write_journal(session, runtime)
    try:
        yield runtime
    finally:
        remove_runtime(session)
        (session / JOURNAL).unlink()


def recover_session(session: Path) -> int:
    with session_lease(session):
        if not (session / JOURNAL).exists() and not (session / JOURNAL).is_symlink():
            # Before the first journal commit, no runtime or children may exist.
            # A partial temporary journal is disposable, but stale session JSON
            # without an ownership journal belongs to an unsupported older run.
            if (
                runtime_path(session).exists()
                or runtime_path(session).is_symlink()
                or (session / "session.json").exists()
                or (session / "session.json").is_symlink()
            ):
                raise SessionError(
                    "No verified headless ownership journal. Resources remain; recovery cannot safely clean an older or foreign session."
                )
        else:
            remove_runtime(session)
        for name in ("session.json", ".session.json", ".headless-owner.tmp", JOURNAL):
            (session / name).unlink(missing_ok=True)
    print(
        json.dumps(
            {"session": str(session), "status": "recovered", "logs": "preserved"}
        )
    )
    return 0
