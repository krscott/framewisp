"""Private, single-threaded Linux subreaper for one managed process tree.

Executed by filename so it also works with the installed package's interpreter.
Only this process reaps its children. A listed child's PID cannot be reused
before we signal it because an exited child remains an unreaped zombie.
"""

import ctypes
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

TERM_SECONDS = 5.0
KILL_SECONDS = 2.0


def children() -> list[int]:
    return [
        int(pid)
        for pid in Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
    ]


def enable_ownership() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER failed")
    children()  # Check procfs before starting any app.
    descriptor = os.pidfd_open(os.getpid())
    try:
        signal.pidfd_send_signal(descriptor, 0)
    finally:
        os.close(descriptor)


def reap(app: subprocess.Popen[bytes]) -> list[int]:
    reaped: list[int] = []
    # Return to the supervisor loop even if a busy app keeps producing zombies.
    for _ in range(256):
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return reaped
        if pid == 0:
            return reaped
        reaped.append(pid)
        if pid == app.pid:
            app.returncode = os.waitstatus_to_exitcode(status)
    return reaped


def cleanup(app: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + TERM_SECONDS + KILL_SECONDS
    kill_at = deadline - KILL_SECONDS
    terminated: set[int] = set()
    failures: dict[int, str] = {}
    while owned := children():
        now = time.monotonic()
        if now >= deadline:
            raise RuntimeError(
                f"Cleanup exceeded {TERM_SECONDS + KILL_SECONDS:g}s; "
                f"supervisor PID {os.getpid()}, app PID {app.pid}, "
                f"surviving child PIDs {owned}, signal failures {failures}. "
                "Inspect these processes for blocked kernel I/O or denied signals."
            )
        for pid in owned:
            if time.monotonic() >= deadline:
                break
            if now >= kill_at or pid not in terminated:
                try:
                    descriptor = os.pidfd_open(pid)
                    try:
                        try:
                            signal.pidfd_send_signal(
                                descriptor,
                                signal.SIGKILL if now >= kill_at else signal.SIGTERM,
                            )
                        except ProcessLookupError:
                            pass
                    finally:
                        os.close(descriptor)
                    terminated.add(pid)
                    failures.pop(pid, None)
                except OSError as error:
                    # One denied signal must not prevent killing other children.
                    failures[pid] = str(error)
        # Killing parents reparents helpers here, including double-forked daemons
        # and descendants that are themselves subreapers. Re-scan until empty.
        reaped = reap(app)
        terminated.difference_update(reaped)
        for pid in reaped:
            failures.pop(pid, None)
        time.sleep(0.01)


def supervise(
    connection: socket.socket, command: list[str], runtime: Path | None = None
) -> None:
    def send(value: dict[str, object]) -> None:
        try:
            connection.sendall(json.dumps(value).encode() + b"\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # Runner loss still requires descendant cleanup.

    record = runtime / f"owner-{os.getpid()}.json" if runtime else None
    app: subprocess.Popen[bytes] | None = None
    try:
        enable_ownership()
        if record is not None:
            record.write_text(
                json.dumps({"supervisor": os.getpid(), "command": command})
            )
        # Only the supervisor receives runner stop requests. The app retains
        # default signal handlers and never inherits the control socket.
        # Use the exec environment, before Python's own startup changes it.
        app_env: dict[str, str] = {}
        for entry in Path("/proc/self/environ").read_bytes().split(b"\0"):
            if entry:
                key, value = entry.split(b"=", 1)
                app_env[os.fsdecode(key)] = os.fsdecode(value)
        app = subprocess.Popen(
            command, env=app_env, stdin=subprocess.DEVNULL, start_new_session=True
        )
        if record is not None:
            record.write_text(json.dumps({"supervisor": os.getpid(), "app": app.pid}))
        send({"pid": app.pid})
        while True:
            reap(app)
            result = app.returncode
            if result is not None:
                send({"returncode": result})
                break
            readable, _, _ = select.select([connection], [], [], 0.05)
            if readable:
                connection.recv(1)  # Stop byte or EOF both request shutdown.
                break
    except (OSError, RuntimeError) as error:
        send({"error": f"Process ownership unavailable or launch failed: {error}"})
    finally:
        if app is not None:
            try:
                cleanup(app)
                if record is not None:
                    record.unlink(missing_ok=True)
                send({"returncode": app.returncode})
            except (OSError, RuntimeError) as error:
                if record is not None:
                    record.write_text(json.dumps({"error": str(error)}))
                send(
                    {
                        "error": f"Cleanup failed: supervisor PID {os.getpid()}, "
                        f"app PID {app.pid}: {error}. Check procfs access and signal permissions."
                    }
                )
        elif record is not None:
            record.unlink(missing_ok=True)
        send({"done": True})


if __name__ == "__main__":
    # A terminal signal must not bypass cleanup. The runner sends stop over the
    # private socket; its SIGKILL closes that socket too.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    signal.signal(signal.SIGINT, lambda signum, frame: None)
    signal.signal(signal.SIGTERM, lambda signum, frame: None)
    with socket.socket(fileno=int(sys.argv[1])) as control:
        supervise(control, sys.argv[3:], Path(sys.argv[2]) if sys.argv[2] else None)
