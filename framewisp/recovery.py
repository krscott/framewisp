"""The session lock, the recovery journal and recovery of headless sessions."""

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
from typing import Literal, cast

from framewisp.connection import DESKTOP_KINDS, session_kind
from framewisp.errors import SessionError

LOCK = ".session.lock"
JOURNAL = ".recovery-journal.json"
JOURNAL_TEMPORARY = ".recovery-journal.tmp"
# Symlink to a recording's private clip. It stays while that clip remains,
# after an interruption or a failed finalization or publish.
PRIVATE_CLIP = ".private-clip"
# Far above any journal, whose runtime directory path fits in a 108-byte socket address, or
# any session.json that recovery reads.
MAX_METADATA_BYTES = 1 << 16


@dataclass(frozen=True)
class Journal:
    """A verified recovery journal. A null identity means mkdir may be uncommitted."""

    runtime_dir: Path
    runtime_dir_identity: list[int] | None


def identity(path: Path) -> list[int]:
    info = path.lstat()
    return [info.st_dev, info.st_ino]


def parse_identity(value: object) -> list[int] | None:
    parts = cast(list[object], value) if isinstance(value, list) else []
    if len(parts) != 2 or not all(type(part) is int for part in parts):
        return None
    return cast(list[int], parts)


def lock_identity(lock: int) -> list[int]:
    # A held descriptor pins its inode, so a matching identity cannot be a reused
    # inode number while the runner or any supervisor still holds the lock.
    info = os.fstat(lock)
    return [info.st_dev, info.st_ino]


def runtime_dir_name(session_dir: Path) -> str:
    device, inode = identity(session_dir)
    return f"framewisp-{os.getuid()}-{device:x}-{inode:x}"


def runtime_dir_path(session_dir: Path) -> Path:
    return Path(tempfile.gettempdir()) / runtime_dir_name(session_dir)


def temporary_parents() -> list[Path]:
    """List where a session's runtime directory can be when no journal says which."""
    # gettempdir() skips a directory it cannot write to, such as a full or read-only
    # one, so a session may have started in any of these.
    parents: list[Path] = []
    try:
        parents.append(Path(tempfile.gettempdir()))
    except OSError:
        pass
    for value in (os.environ.get("TMPDIR"), "/tmp"):
        try:
            if value:
                parents.append(Path(value).absolute())
        except OSError:
            # A relative TMPDIR has no location without a working directory.
            pass
    return list(dict.fromkeys(parents))


def path_status(path: Path) -> Literal["present", "absent", "unchecked"]:
    # Only a confirmed absence may read as finished cleanup. Path.exists() cannot
    # tell that from a path it failed to check.
    try:
        path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError:
        return "unchecked"
    return "present"


def likely_runtime_dir(session_dir: Path) -> Path:
    """Find this session's runtime directory, for refusals and for checking leftovers."""
    name = runtime_dir_name(session_dir)
    candidates = [parent / name for parent in temporary_parents()]
    for status in ("present", "unchecked"):
        for runtime_dir in candidates:
            if path_status(runtime_dir) == status:
                return runtime_dir
    return candidates[0]


def check_socket_paths(runtime_dir: Path) -> None:
    # Sway names its IPC socket with a UID and signed 32-bit PID. Budget for
    # every possible PID before starting any children, plus the terminating NUL.
    socket_path = runtime_dir / f"sway-ipc.{os.getuid()}.2147483647.sock"
    size = len(os.fsencode(socket_path)) + 1
    if size > 108:
        raise SessionError(
            f"Runtime directory {runtime_dir} makes socket paths up to {size} bytes "
            "including the terminating NUL, over the 108-byte limit. "
            "Set TMPDIR to a shorter directory."
        )


def journal_runtime_dir(session_dir: Path, journal: dict[str, object]) -> Path:
    """Return the journal's runtime directory, or raise ValueError or OSError."""
    value = journal.get("runtime_directory")
    if value is None:
        raise ValueError("journal has no runtime directory path")
    if not isinstance(value, str):
        raise ValueError("journal runtime directory path must be an absolute path")
    runtime_dir = Path(value)
    if not runtime_dir.is_absolute() or runtime_dir.name != runtime_dir_name(
        session_dir
    ):
        raise ValueError(f"journal has an invalid runtime directory path: {value!r}")
    # Path.exists() reports a path that cannot be checked as missing, which would
    # read as a finished cleanup. Only a confirmed absence may.
    try:
        runtime_dir.lstat()
    except FileNotFoundError:
        pass
    return runtime_dir


def kept_clip_note(session_dir: Path) -> str:
    clip = kept_clip(session_dir)
    if clip is None:
        return ""
    recover = shlex.join(["framewisp", str(session_dir), "recover"])
    return (
        f"\nAn interrupted clip is kept at {str(clip)!r}. These steps do not touch "
        f"it, and a successful `{recover}` reports it."
    )


def unusable_lock(lock_path: Path, problem: str) -> str:
    recover = shlex.join(["framewisp", str(lock_path.parent), "recover"])
    return (
        f"Session lock {lock_path} is unusable ({problem}). It may have been replaced, so "
        "nothing was changed. If only its mode changed, run chmod 600 on it. "
        "Otherwise move it aside. "
        f"Then run `{recover}`, which checks the lock against the session's "
        "recovery journal and reports what to do next."
    ) + kept_clip_note(lock_path.parent)


@contextmanager
def session_lock(session_dir: Path) -> Generator[int, None, None]:
    try:
        directory = session_dir.lstat()
    except OSError as error:
        raise SessionError(
            unreadable_journal(session_dir, str(error.strerror))
        ) from error
    if (
        not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != os.getuid()
        or directory.st_mode & 0o022
    ):
        raise SessionError(
            f"Unsafe session directory: {session_dir}. Use a directory you own without group or other write permission (chmod 700)."
            + kept_clip_note(session_dir)
        )
    lock_path = session_dir / LOCK
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        if error.errno == errno.ENOENT:
            raise SessionError(
                unreadable_journal(session_dir, str(error.strerror))
            ) from error
        # Other errors, such as an unwritable directory, say nothing about the lock.
        if error.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO) or (
            error.errno == errno.EACCES and lock_path.exists()
        ):
            raise SessionError(unusable_lock(lock_path, str(error.strerror))) from error
        raise
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stop = shlex.join(["framewisp", str(session_dir), "stop"])
            raise SessionError(
                f"{session_dir} is in use by a live session, or by supervisors "
                "cleaning up after a runner that just died.\n"
                "To end a live session, press Ctrl+C in its runner's terminal. "
                f"Once a headless session is ready, `{stop}` also ends it.\n"
                f"Or use a session directory other than {session_dir}.\n"
                "If the session is still starting or its runner just died, "
                "wait a few seconds and retry."
            ) from None
        yield descriptor
    finally:
        # Supervisors share this open file description. Closing, rather than
        # unlocking, keeps the lock held until every supervisor has closed it.
        os.close(descriptor)


def cleanup_steps(session_dir: Path, runtime_dir: Path, *process_checks: str) -> str:
    """Describe manual cleanup for a session that recovery cannot verify."""
    framewisp = shlex.join(["framewisp", str(session_dir)])
    quoted = shlex.quote(str(runtime_dir))
    # The directory's name is the same under any TMPDIR. In a UTF-8 locale, `.*`
    # would not match a TMPDIR that is not valid UTF-8.
    environment = shlex.quote(f"^XDG_RUNTIME_DIR=.*/{runtime_dir.name}$")
    checks = (*process_checks, f"LC_ALL=C grep -zlsE {environment} /proc/*/environ")
    files = ("session.json", ".session.json", JOURNAL, JOURNAL_TEMPORARY)
    lines = [
        "Confirm the session has exited. Run these outside any sandbox, in the PID "
        "namespace that started the session, because they cannot see processes "
        f"elsewhere. `{framewisp} status` must not report running, and these "
        "commands must print nothing:",
        *(f"  {check}" for check in checks),
        "Inspect any supervisor records, which can report incomplete cleanup:",
        f"  cat {quoted}/supervisor-*.json",
        "Then delete its runtime directory, session metadata and recovery journal, "
        "keeping logs:",
        # find does not follow a symlinked start; rm needs only directory access.
        f"  find {quoted} -type d -exec chmod u+rwx {{}} +",
        f"  rm -rf -- {quoted}",
        "  " + shlex.join(["rm", "-rf", "--", *(str(session_dir / m) for m in files)]),
        f"Then start again with `{framewisp} run -- APP`.",
    ]
    return "\n".join(lines) + kept_clip_note(session_dir)


def read_json(path: Path) -> object:
    """Parse session metadata or a recovery journal that may be damaged."""
    # Deeply nested JSON raises RecursionError, which callers must also handle.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{path.name} must be a regular file")
        if info.st_size > MAX_METADATA_BYTES:
            raise ValueError(f"{path.name} is over {MAX_METADATA_BYTES} bytes")
        return json.load(source)


def stop_step(session_dir: Path) -> str:
    framewisp = shlex.join(["framewisp", str(session_dir)])
    return f"If the session is running, stop it with `{framewisp} stop`.\n"


def missing_session_steps(session_dir: Path) -> str:
    # The runtime directory's name comes from the session directory's identity, which
    # is gone. Nothing printable can identify it, and a starting session has no
    # marked process yet, so these steps must never single out a directory to delete.
    uid = os.getuid()
    parents = shlex.join(str(parent) for parent in temporary_parents())
    pattern = shlex.quote(f"framewisp-{uid}-*")
    environment = shlex.quote(f"^XDG_RUNTIME_DIR=.*/framewisp-{uid}-[0-9a-f-]+$")
    framewisp = shlex.join(["framewisp", "NEW_PATH", "recover"])
    return (
        f"Session directory {session_dir} does not exist, or cannot be read. Check the "
        "path.\n"
        f"If it moved within its filesystem, run `{framewisp}`: its session lock and "
        "recovery journal moved with it.\n"
        "If it was deleted, its processes and runtime directory may remain. A leftover "
        "directory only uses disk space, because a new session gets its own. Its name "
        "cannot be told from your other sessions'. To look for one, list the runtime "
        f"directory names under {parents}, then the names running processes use, from "
        "outside any sandbox:\n"
        f"  find -H {parents} -maxdepth 1 -name {pattern} -printf '%f\\n'\n"
        f"  LC_ALL=C grep -zahsoE {environment} /proc/*/environ | tr '\\0' '\\n' "
        "| LC_ALL=C sed 's|.*/||' | sort -u\n"
        "A name in the second list has running processes, which may be another "
        "session's. A name only in the first list does not prove the session is gone: "
        "a session that is still starting looks the same, and supervisor-*.json records in "
        "it mean a session may be live. Stop processes or delete a directory only if "
        "you know it was this session's. If unsure, leave it."
    )


def derived_steps(session_dir: Path) -> str:
    """Describe manual cleanup when no journal can be trusted to name the runtime directory."""
    # A journal is the only trustworthy record of the session's TMPDIR. A path copied
    # from an unverified one could also aim these steps at another session, so they
    # use the path derived from this session's identity.
    try:
        runtime_dir = likely_runtime_dir(session_dir)
    except OSError:
        return missing_session_steps(session_dir)
    unchecked = (
        f"Cannot check {runtime_dir}, so a runtime directory may remain there. Restore access, then "
        "run recover again. Unset TMPDIR instead only if you know this session never "
        "ran there, because recover then stops checking it.\n"
        if path_status(runtime_dir) == "unchecked"
        else ""
    )
    return (
        unchecked
        + stop_step(session_dir)
        + f"Otherwise follow these steps, which assume TMPDIR is {runtime_dir.parent}. "
        "If the session started with another TMPDIR, set it and run recover again "
        "for steps with that path.\n" + cleanup_steps(session_dir, runtime_dir)
    )


def unreadable_journal(session_dir: Path, problem: str) -> str:
    return (
        f"Cannot verify the recovery journal {session_dir / JOURNAL}: {problem}\n"
        "Cannot prove the session has exited. Nothing was removed.\n"
        + derived_steps(session_dir)
    )


def unverified_runtime_dir(session_dir: Path, runtime_dir: Path, problem: str) -> str:
    return (
        f"{problem}\nNothing was removed.\n"
        + stop_step(session_dir)
        + "Otherwise:\n"
        + cleanup_steps(session_dir, runtime_dir)
    )


def records_desktop_session(session_dir: Path) -> bool:
    try:
        metadata = read_json(session_dir / "session.json")
    except (OSError, ValueError, RecursionError):
        return False
    return session_kind(metadata) in DESKTOP_KINDS


def stale_desktop_session(session_dir: Path) -> str:
    metadata = shlex.join(["rm", "-f", "--", str(session_dir / "session.json")])
    return (
        f"{session_dir} records an attached or teaching session, which recover does not clean up. "
        "Nothing was removed.\n"
        f"If {session_dir / LOCK} was moved or replaced, first confirm that the runner "
        "of that attached or teaching session has stopped: "
        "the terminal that ran attach or teach is back at its prompt. "
        "Then start it again with attach or teach "
        "(for teach, with a new lesson directory), "
        f"or delete its metadata:\n  {metadata}"
    ) + kept_clip_note(session_dir)


def verify_lock(
    session_dir: Path, recorded: list[int], runtime_dir: Path, lock: int
) -> None:
    held = lock_identity(lock)
    if recorded == held:
        return
    lock_path = session_dir / LOCK
    raise SessionError(
        f"Session lock {lock_path} was replaced: the recovery journal records "
        f"device:inode {recorded[0]}:{recorded[1]}, but found {held[0]}:{held[1]}. "
        "A live session may still hold the original lock, so nothing was removed.\n"
        f"{stop_step(session_dir)}If you still have the original lock file, move it back "
        f"to {lock_path} and run recover again.\n"
        "If the original lock is lost:\n"
        + cleanup_steps(session_dir, runtime_dir, f"grep ':{recorded[1]} ' /proc/locks")
    )


def read_journal(session_dir: Path, lock: int) -> Journal:
    """Return the verified journal, or refuse with manual cleanup steps."""
    path = session_dir / JOURNAL
    try:
        value = read_json(path)
        if not isinstance(value, dict):
            raise ValueError("journal must be an object")
        journal = cast(dict[str, object], value)
        if journal.get("session_directory_identity") != identity(session_dir):
            raise ValueError("journal does not belong to this session directory")
        runtime_dir = journal_runtime_dir(session_dir, journal)
        if "runtime_directory_identity" not in journal:
            raise ValueError("journal has no runtime directory identity")
        runtime_dir_identity = parse_identity(journal["runtime_directory_identity"])
        if (
            runtime_dir_identity is None
            and journal["runtime_directory_identity"] is not None
        ):
            raise ValueError("journal has an invalid runtime directory identity")
        recorded_lock = parse_identity(journal.get("session_lock_identity"))
        if recorded_lock is None:
            raise ValueError("journal has no valid session lock identity")
    except (OSError, ValueError, RecursionError) as error:
        raise SessionError(unreadable_journal(session_dir, str(error))) from error
    verify_lock(session_dir, recorded_lock, runtime_dir, lock)
    return Journal(runtime_dir, runtime_dir_identity)


def headless_runner_stopped(session_dir: Path) -> bool:
    """Return True only when the verified session lock is free and a journal remains.

    A live headless runner and its supervisors hold an exclusive flock on the
    lock inode recorded in the recovery journal, and clean shutdown deletes the
    journal before releasing the lock. Unreadable, replaced, or copied state
    returns False so callers keep their normal diagnostics.
    """
    lock_path = session_dir / LOCK
    try:
        descriptor = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return False
        try:
            # flock has no non-locking query; a shared lock avoids blocking other readers.
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        value = read_json(session_dir / JOURNAL)
        if not isinstance(value, dict):
            return False
        journal = cast(dict[str, object], value)
        return journal.get("session_directory_identity") == identity(
            session_dir
        ) and parse_identity(journal.get("session_lock_identity")) == lock_identity(
            descriptor
        )
    except (OSError, ValueError, RecursionError):
        return False
    finally:
        os.close(descriptor)


def stopped_runner_message(session_dir: Path) -> str:
    recover = shlex.join(["framewisp", str(session_dir), "recover"])
    return (
        "The runner has stopped without cleaning up. "
        f"Run `{recover}` to clean up the session directory."
    )


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


def write_journal(
    session_dir: Path, runtime_dir: Path, lock: int, *, created: bool
) -> None:
    temporary = session_dir / JOURNAL_TEMPORARY
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(
            {
                "session_directory_identity": identity(session_dir),
                "session_lock_identity": lock_identity(lock),
                "runtime_directory": str(runtime_dir),
                "runtime_directory_identity": (
                    identity(runtime_dir) if created else None
                ),
            },
            output,
        )
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(session_dir / JOURNAL)
    sync_directory(session_dir)


def make_directories_removable(root: Path) -> None:
    """Apps own this tree and can leave read-only directories in it."""
    for parent, directories, _ in os.walk(root):
        for name in directories:
            path = Path(parent, name)
            if not path.is_symlink():
                path.chmod(0o700)


def remove_runtime_dir(session_dir: Path, lock: int) -> None:
    journal = read_journal(session_dir, lock)
    runtime_dir = journal.runtime_dir
    if runtime_dir.is_symlink():
        raise SessionError(
            unverified_runtime_dir(
                session_dir,
                runtime_dir,
                f"Refusing symlinked runtime directory: {runtime_dir}",
            )
        )
    if runtime_dir.exists():
        info = runtime_dir.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SessionError(
                unverified_runtime_dir(
                    session_dir,
                    runtime_dir,
                    f"Runtime directory owner or permissions changed: {runtime_dir}",
                )
            )
        if journal.runtime_dir_identity is None:
            # Interruption between mkdir and committing its inode can only have
            # left an empty directory. Never recursively remove it without proof.
            try:
                runtime_dir.rmdir()
            except OSError as error:
                raise SessionError(
                    unverified_runtime_dir(
                        session_dir,
                        runtime_dir,
                        f"Recovery journal records no runtime directory identity, but {runtime_dir} "
                        f"cannot be removed: {error.strerror}.",
                    )
                ) from error
        else:
            if journal.runtime_dir_identity != identity(runtime_dir):
                raise SessionError(
                    unverified_runtime_dir(
                        session_dir,
                        runtime_dir,
                        f"Runtime directory identity changed; resources remain at {runtime_dir}.",
                    )
                )
            pending = list(runtime_dir.glob("supervisor-*.json"))
            if pending:
                raise SessionError(
                    unverified_runtime_dir(
                        session_dir,
                        runtime_dir,
                        f"Cleanup is incomplete; resources remain at {runtime_dir}. "
                        "Inspect supervisor records: "
                        f"{', '.join(repr(str(path)) for path in pending)}",
                    )
                )
            make_directories_removable(runtime_dir)
            shutil.rmtree(runtime_dir)


def refuse_unrecovered_session(session_dir: Path) -> None:
    paths = (
        session_dir / JOURNAL,
        session_dir / JOURNAL_TEMPORARY,
        runtime_dir_path(session_dir),
    )
    if any(path_status(path) != "absent" for path in paths):
        recover = shlex.join(["framewisp", str(session_dir), "recover"])
        raise SessionError(
            "A headless session's recovery journal or runtime directory remains. "
            f"Run `{recover}` first, with the framewisp version that started that "
            "session."
        )


@contextmanager
def headless_runtime_dir(session_dir: Path, lock: int) -> Generator[Path, None, None]:
    runtime_dir = runtime_dir_path(session_dir)
    refuse_unrecovered_session(session_dir)
    check_socket_paths(runtime_dir)
    write_journal(session_dir, runtime_dir, lock, created=False)
    runtime_dir.mkdir(mode=0o700)
    write_journal(session_dir, runtime_dir, lock, created=True)
    try:
        yield runtime_dir
    finally:
        remove_runtime_dir(session_dir, lock)
        # Keep the journal until session metadata is gone, including if shutdown
        # is interrupted after removing the runtime directory.
        for name in ("session.json", ".session.json"):
            (session_dir / name).unlink(missing_ok=True)
        (session_dir / JOURNAL).unlink()


def recover_session(session_dir: Path) -> int:
    with session_lock(session_dir) as lock:
        if path_status(session_dir / JOURNAL) == "absent":
            # Before the first journal commit, no runtime directory or children may
            # exist. A partial temporary journal is disposable, but stale session
            # metadata without a recovery journal belongs to an unsupported older session.
            metadata = (session_dir / "session.json", session_dir / ".session.json")
            if any(
                path_status(path) != "absent"
                for path in (likely_runtime_dir(session_dir), *metadata)
            ):
                if records_desktop_session(session_dir):
                    raise SessionError(stale_desktop_session(session_dir))
                raise SessionError(
                    unreadable_journal(session_dir, "journal is missing")
                )
        else:
            remove_runtime_dir(session_dir, lock)
        for name in ("session.json", ".session.json", JOURNAL_TEMPORARY, JOURNAL):
            (session_dir / name).unlink(missing_ok=True)
        interrupted = interrupted_clip(session_dir)
    result: dict[str, str] = {
        "session_directory": str(session_dir),
        "status": "recovered",
        "logs": "preserved",
    }
    if interrupted is not None:
        result["interrupted_clip"] = str(interrupted)
    print(json.dumps(result))
    return 0


def kept_clip(session_dir: Path) -> Path | None:
    """Return the clip that the private clip link points at, if it remains."""
    # Refusals call this, so a damaged link must not raise.
    try:
        clip = (session_dir / PRIVATE_CLIP).readlink()
    except OSError:
        return None
    return None if path_status(clip) == "absent" else clip


def interrupted_clip(session_dir: Path) -> Path | None:
    """Clear the private clip link, returning the clip it left behind."""
    link = session_dir / PRIVATE_CLIP
    if not link.is_symlink():
        return None
    clip = link.readlink()
    link.unlink()
    if clip.exists():
        return clip
    # A runner killed before the first frame leaves an empty private directory.
    try:
        clip.parent.rmdir()
    except OSError:
        pass
    return None
