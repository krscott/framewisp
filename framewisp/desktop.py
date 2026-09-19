"""Per-user ownership and emergency stop for the real desktop (Linux)."""

import fcntl
import json
import os
import select
import signal
import socket
import stat
import struct
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

# Linux SO_PEERPIDFD, available since 6.5; not exposed by Python 3.14 yet.
SO_PEERPIDFD = 77


def desktop_directory() -> Path:
    root = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    if not root.is_absolute():
        raise RuntimeError("XDG_RUNTIME_DIR must be an absolute path.")
    info = root.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError(
            "XDG_RUNTIME_DIR must be owned by you and private (mode 0700)."
        )
    return root / "framewisp"


def write_state(path: Path, value: dict[str, object]) -> None:
    descriptor, name = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output)
            output.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def reserve_desktop() -> Generator[Path]:
    directory = desktop_directory()
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise RuntimeError(f"{directory} must be a private directory owned by you.")
    descriptor = os.open(
        directory / "desktop.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "A desktop attachment already exists. Run framewisp --detach to stop it."
            ) from None
        # Never unlink the lock file: all contenders must lock the same inode.
        for name in ("control.sock", "detach.sock", "desktop.json"):
            (directory / name).unlink(missing_ok=True)
        try:
            yield directory
        finally:
            for name in ("control.sock", "detach.sock", "desktop.json"):
                (directory / name).unlink(missing_ok=True)
    finally:
        os.close(descriptor)


def detach_desktop() -> int:
    """Stop the socket's actual owner, without trusting a PID in a state file."""
    try:
        directory = desktop_directory()
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(0.5)
            connection.connect(str(directory / "detach.sock"))
            _, uid, _ = struct.unpack(
                "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if uid != os.getuid():
                raise RuntimeError(
                    "Refusing to stop a desktop connection owned by another user."
                )
            process = connection.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD)
        try:
            poll = select.poll()
            poll.register(process, select.POLLIN)
            try:
                signal.pidfd_send_signal(process, signal.SIGTERM)
                if not poll.poll(500):
                    # A stopped process or blocked main thread cannot run cleanup.
                    signal.pidfd_send_signal(process, signal.SIGKILL)
                    if not poll.poll(1000):
                        raise RuntimeError(
                            "The attach process did not exit after SIGKILL."
                        )
            except ProcessLookupError:
                pass
        finally:
            os.close(process)
        print("Desktop attachment stopped.")
        return 0
    except (FileNotFoundError, ConnectionRefusedError):
        print("No active desktop attachment.")
        return 0
    except (OSError, RuntimeError) as error:
        print(f"Could not detach: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(detach_desktop())
