"""Session socket messages; no desktop libraries required."""

import json
import socket
import sys
from pathlib import Path


def request_attached(session: Path, action: str, parameters: dict[str, object]) -> int:
    try:
        state = json.loads((session / "session.json").read_text())
        if state.get("kind") != "attached":
            print("This command requires an attached desktop session.", file=sys.stderr)
            return 1
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(Path(state["runtime_directory"]) / "control.sock"))
            connection.sendall(
                (
                    json.dumps(
                        {
                            "action": action,
                            "parameters": parameters,
                            "attachment": state["attachment"],
                        }
                    )
                    + "\n"
                ).encode()
            )
            with connection.makefile("r") as response:
                line = response.readline()
        if not line:
            print("Attached session disconnected.", file=sys.stderr)
            return 1
        result = json.loads(line)
        if result["error"]:
            print(result["error"], file=sys.stderr)
        return int(result["status"])
    except (FileNotFoundError, ConnectionError):
        print(
            "Attached session is disconnected. Ask the user to run attach in another terminal.",
            file=sys.stderr,
        )
        return 1

    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(
            f"Cannot use attached session: {error}. Ask the user to reconnect.",
            file=sys.stderr,
        )
        return 1


def reply(
    connection: socket.socket,
    *,
    error: str | None = None,
    data: dict[str, object] | None = None,
) -> None:
    try:
        connection.sendall((json.dumps({"error": error, "data": data}) + "\n").encode())
    except (BrokenPipeError, ConnectionResetError, TimeoutError):
        # A caller can stop waiting while an action or recording finishes.
        pass
