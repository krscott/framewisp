"""Runner-side handle for a private descendant-owning supervisor."""

import json
import select
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from framewisp.errors import SessionError


@dataclass
class OwnedProcess:
    supervisor: subprocess.Popen[bytes]
    connection: socket.socket
    log: Path
    pid: int = 0
    returncode: int | None = None
    buffer: bytes = b""
    errors: list[str] = field(default_factory=lambda: list[str]())
    done: bool = False

    def receive(self, timeout: float = 0) -> None:
        if not select.select([self.connection], [], [], timeout)[0]:
            return
        data = self.connection.recv(4096)
        if not data:
            if not self.done:
                raise SessionError(
                    f"Process supervisor {self.supervisor.pid} disconnected before cleanup; "
                    f"app PID {self.pid}, log: {self.log}"
                )
            return
        self.buffer += data
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            message = json.loads(line)
            if "pid" in message:
                self.pid = int(message["pid"])
            if "returncode" in message:
                self.returncode = int(message["returncode"])
            if "error" in message:
                self.errors.append(str(message["error"]))
            if "done" in message:
                self.done = True

    def poll(self) -> int | None:
        self.receive()
        if self.errors:
            raise SessionError(
                f"{'; '.join(self.errors)}; supervisor PID {self.supervisor.pid}, "
                f"app PID {self.pid}, log: {self.log}"
            )
        return self.returncode

    def close(self) -> None:
        try:
            try:
                self.connection.sendall(b"s")
            except (BrokenPipeError, ConnectionResetError):
                pass
            deadline = time.monotonic() + 8
            while not self.done:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SessionError(
                        f"Supervisor {self.supervisor.pid} cleanup timed out; "
                        f"app PID {self.pid}, log: {self.log}"
                    )
                self.receive(min(remaining, 0.1))
            self.supervisor.wait(timeout=max(0.01, deadline - time.monotonic()))
            if self.errors:
                raise SessionError(
                    f"{'; '.join(self.errors)}; supervisor PID {self.supervisor.pid}, "
                    f"app PID {self.pid}, log: {self.log}"
                )
        finally:
            self.connection.close()


@contextmanager
def managed_process(
    command: list[str],
    *,
    log: Path,
    env: dict[str, str],
    output: BinaryIO | None = None,
) -> Generator[OwnedProcess, None, None]:
    parent, child = socket.socketpair()
    with parent, child, ExitStack() as stack:
        destination = (
            output if output is not None else stack.enter_context(log.open("wb"))
        )
        supervisor_env = env.copy()
        lease = int(supervisor_env.pop("FRAMEWISP_OWNER_FD", "-1"))
        runtime = supervisor_env.pop("FRAMEWISP_OWNER_RUNTIME", "")
        supervisor = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("_supervisor.py")),
                str(child.fileno()),
                runtime,
                *command,
            ],
            env=supervisor_env,
            stdin=subprocess.DEVNULL,
            stdout=destination,
            stderr=subprocess.STDOUT,
            pass_fds=(child.fileno(),) + ((lease,) if lease >= 0 else ()),
            start_new_session=True,
        )
        child.close()
        process = OwnedProcess(supervisor, parent, log)
        try:
            deadline = time.monotonic() + 10
            while not process.pid and not process.done:
                if time.monotonic() >= deadline:
                    raise SessionError(
                        f"Supervisor {supervisor.pid} startup timed out; log: {log}"
                    )
                process.receive(0.1)
            if process.errors:
                raise SessionError(f"{'; '.join(process.errors)}; log: {log}")
            if not process.pid:
                raise SessionError(f"Supervisor did not launch {command!r}; log: {log}")
            yield process
        finally:
            process.close()
