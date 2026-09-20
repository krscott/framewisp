"""Actionable failures from session processes and their display connections."""

import json
import subprocess
import sys
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


def display_command(
    session: Path,
    command: list[str],
    *,
    timeout: float,
    env: dict[str, str] | None = None,
    display: str | None = None,
    input_text: str | None = None,
) -> int:
    state = json.loads((session / "session.json").read_text())
    context = (
        f"{command[0]} failed for session {session}, display {display or state['wayland_display']}, "
        f"runtime directory {state['runtime_directory']}"
    )
    try:
        result = subprocess.run(
            command,
            env=env,
            input=input_text,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SessionError(f"{context}: {error}\n{SOCKET_ACCESS_HINT}") from None
    if result.returncode:
        raise SessionError(
            f"{context} (exit {result.returncode}):\n{result.stderr.strip()}\n{SOCKET_ACCESS_HINT}"
        )
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return 0
