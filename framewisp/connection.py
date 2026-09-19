"""CLI requests to a foreground attach process; no desktop libraries required."""

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
            connection.connect(str(Path(state["runtime_directory"]) / "attach.sock"))
            connection.sendall(
                (
                    json.dumps({"action": action, "parameters": parameters}) + "\n"
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
            "Attached session is disconnected. Run attach again to reconnect.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(request_attached(Path(sys.argv[1]), "detach", {}))
