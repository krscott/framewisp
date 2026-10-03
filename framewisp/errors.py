"""Actionable failures from session processes and their display connections."""

import json
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast


class SessionError(RuntimeError):
    """An expected operational failure that the CLI can report without a traceback."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        input_message: str | None = None,
    ):
        super().__init__(message)
        self.returncode = returncode
        # Only supply diagnostics constructed without literal or encoded input.
        self.input_message = input_message


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


def app_exit_message(session: Path, returncode: int) -> str:
    exit_code = returncode if returncode >= 0 else 128 - returncode
    reason = f"exit {exit_code}"
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = f"signal {-returncode}"
        reason = f"{name} ({reason})"
    return f"App exited: {reason}; see {session / 'app.log'}"


def recorded_app_exit(session: Path) -> str | None:
    try:
        record = json.loads((session / "app-exit.json").read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        return f"Cannot read app exit record: {error}"
    returncode = (
        cast(dict[str, object], record).get("returncode")
        if isinstance(record, dict)
        else None
    )
    if type(returncode) is not int:
        return f"Invalid app exit record: {session / 'app-exit.json'}"
    return app_exit_message(session, returncode)


def display_command(
    session: Path,
    command: list[str],
    *,
    timeout: float,
    env: dict[str, str] | None = None,
    display: str | None = None,
    input_text: str | None = None,
    cancelled: Callable[[], bool] | None = None,
    input_content: bool = False,
) -> int:
    state = json.loads((session / "session.json").read_text())
    omit_content = input_content and state.get("retain_input_content") is not True
    context = (
        f"{command[0]} failed for session {session}, display {display or state['wayland_display']}, "
        f"runtime directory {state['runtime_directory']}"
    )
    try:
        with subprocess.Popen(
            command,
            env=env,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            deadline = time.monotonic() + timeout
            try:
                while True:
                    if cancelled is not None and cancelled():
                        raise InterruptedError(
                            "Input cancelled; caller disconnected or session stopped."
                        )
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(command, timeout)
                    try:
                        _, stderr = process.communicate(input_text, timeout=0.05)
                        break
                    except subprocess.TimeoutExpired:
                        input_text = None
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            result = subprocess.CompletedProcess(
                command, process.returncode, stderr=stderr
            )
    except InterruptedError:
        raise
    except (OSError, subprocess.TimeoutExpired) as error:
        detail = type(error).__name__ if omit_content else str(error)
        message = f"{context}: {detail}\n{SOCKET_ACCESS_HINT}"
        raise SessionError(
            message, input_message=message if omit_content else None
        ) from None
    if result.returncode:
        diagnostic = (
            "Input diagnostics omitted." if omit_content else result.stderr.strip()
        )
        message = (
            f"{context} (exit {result.returncode}):\n{diagnostic}\n{SOCKET_ACCESS_HINT}"
        )
        raise SessionError(
            message,
            returncode=result.returncode,
            input_message=message if omit_content else None,
        )
    if result.stderr and not omit_content:
        print(result.stderr, end="", file=sys.stderr)
    return 0
