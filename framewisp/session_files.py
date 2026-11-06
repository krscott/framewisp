"""The files framewisp owns in a session directory, and the check protecting them."""

from pathlib import Path

from framewisp.ownership import ACTIVE_RECORDING, JOURNAL, LOCK

SESSION_FILES = (
    LOCK,
    JOURNAL,
    ".headless-owner.tmp",
    ACTIVE_RECORDING,
    "session.json",
    ".session.json",
    "app-exit.json",
    ".app-exit.json",
    "sway.log",
    "wayvnc.log",
    "recorder.log",
    "app.log",
    "console.jsonl",
    "inputs.jsonl",
    "activity.jsonl",
    "captions.log",
    "capture.log",
    "dbus.log",
    "accessibility.log",
    "registry.log",
)


def file_identity(path: Path) -> tuple[int, int] | None:
    """Return what hard links share, following symlinks, or None if unreadable."""
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_dev, info.st_ino


def protected_path_error(
    session: Path, path: Path, recording: Path | None
) -> str | None:
    """Refuse a resolved path that reaches `recording` or a session file."""
    if path == recording:
        return f"{path} is the active recording's destination. Choose a new path."
    owned = [session / name for name in SESSION_FILES]
    identity = file_identity(path)
    if path in {file.resolve() for file in owned} or (
        identity is not None and identity in {file_identity(file) for file in owned}
    ):
        return f"{path} is reserved for session files. Choose a new path."
    return None
