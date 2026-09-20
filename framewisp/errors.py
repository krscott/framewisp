"""Actionable failures from session processes and their display connections."""

from pathlib import Path


class SessionError(RuntimeError):
    """An expected operational failure that the CLI can report without a traceback."""


SOCKET_ACCESS_HINT = (
    "The runner and control commands both need access to the private display sockets. "
    "A sandbox can deny that access even if the runner started successfully; "
    "use your execution environment's normal permission process when needed."
)


def log_failure(message: str, log: Path, *, display: bool = False) -> SessionError:
    """Include a bounded tail without loading a potentially large component log."""
    with log.open("rb") as source:
        source.seek(0, 2)
        source.seek(max(0, source.tell() - 4096))
        tail = "\n".join(source.read().decode(errors="replace").splitlines()[-12:])
    detail = f"{message}. See {log}"
    if tail:
        detail += f"\n{tail}"
    if display:
        detail += f"\n{SOCKET_ACCESS_HINT}"
    return SessionError(detail)
