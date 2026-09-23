"""Compare full and region CLI captures of identical frozen demo content."""

import argparse
import json
import math
import os
import platform
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--x11", action="store_true")
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")
    command = [sys.executable, "-m", "framewisp"]
    samples: dict[str, list[float]] = {}
    print(
        json.dumps(
            {
                "environment": platform.platform(),
                "python": sys.version,
                "backend": "x11" if args.x11 else "wayland",
                "samples": args.samples,
            }
        ),
        flush=True,
    )
    with tempfile.TemporaryDirectory(prefix="framewisp-crop-bench-") as temporary:
        directory = Path(temporary)
        session = directory / "session"
        with (directory / "runner.log").open("w+") as log:
            runner = subprocess.Popen(
                [
                    *command,
                    str(session),
                    "run",
                    *(["--x11"] if args.x11 else []),
                    "--",
                    sys.executable,
                    "-m",
                    "framewisp.demo",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            app_pid: int | None = None
            try:
                deadline = time.monotonic() + 20
                while not (session / "session.json").exists():
                    if runner.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise RuntimeError(log.read())
                    time.sleep(0.01)
                time.sleep(1)
                    app_pid = json.loads((session / "session.json").read_text())[
                        "processes"
                    ]["app"]
                    assert isinstance(app_pid, int)
                os.kill(app_pid, signal.SIGSTOP)
                time.sleep(0.1)
                reference: bytes | None = None
                for index in range(args.samples + 1):
                    for name in (
                        ["full", "region"] if index % 2 else ["region", "full"]
                    ):
                        path = directory / (name + ".png")
                        capture_command = [
                            *command,
                            str(session),
                            "screenshot",
                            str(path),
                            "--json",
                        ]
                        if name == "region":
                            capture_command += ["--region", "40", "80", "400", "160"]
                        started = time.perf_counter()
                        result = subprocess.run(
                            capture_command,
                            capture_output=True,
                            text=True,
                            check=True,
                            timeout=15,
                        )
                        elapsed = time.perf_counter() - started
                        metadata = json.loads(result.stdout)
                        with Image.open(path) as image:
                            pixels = (
                                (
                                    image.crop((40, 80, 440, 240))
                                    if name == "full"
                                    else image
                                )
                                .convert("RGB")
                                .tobytes()
                            )
                        if reference is None:
                            reference = pixels
                        assert pixels == reference, "Content changed between captures"
                        if index == 0:
                            continue
                        print(
                            json.dumps(
                                {
                                    "run": index,
                                    "kind": name,
                                    "cli_seconds": elapsed,
                                    "bytes": path.stat().st_size,
                                    **metadata,
                                }
                            ),
                            flush=True,
                        )
                        for metric, value in [
                            ("cli_seconds", elapsed),
                            ("capture_seconds", metadata["capture_seconds"]),
                            ("bytes", path.stat().st_size),
                        ]:
                            samples.setdefault(name + "." + metric, []).append(value)
                print(
                    json.dumps(
                        {
                            "summary": {
                                name: {
                                    "p50": statistics.median(values),
                                    "p95": sorted(values)[
                                        math.ceil(len(values) * 0.95) - 1
                                    ],
                                }
                                for name, values in samples.items()
                            },
                            "agent_workflow": "Not measured; no model latency or token savings claimed.",
                        }
                    ),
                    flush=True,
                )
            finally:
                if app_pid is not None:
                    os.kill(app_pid, signal.SIGCONT)
                runner.terminate()
                runner.wait(timeout=20)


if __name__ == "__main__":
    main()
