"""Measure complete CLI calls with normal input logging and no recording."""

import argparse
import importlib.metadata
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--x11", action="store_true")
    args = parser.parse_args()
    env = os.environ | {
        "PYTHONPATH": str(args.source.resolve())
        + os.pathsep
        + os.environ.get("PYTHONPATH", "")
    }
    command = [sys.executable, "-m", "framewisp"]
    samples: dict[str, list[float]] = {}

    def run(session: Path, *arguments: str) -> None:
        subprocess.run(
            [*command, str(session), *arguments],
            env=env,
            cwd=args.source,
            capture_output=True,
            check=True,
            timeout=20,
        )

    with tempfile.TemporaryDirectory(prefix="framewisp-bench-") as temporary:
        for cold in range(6):
            session = Path(temporary) / str(cold)
            started = time.perf_counter()
            with (Path(temporary) / "runner.log").open("w+") as log:
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
                    env=env,
                    cwd=args.source,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                try:
                    while not (session / "session.json").exists():
                        if runner.poll() is not None:
                            log.seek(0)
                            raise RuntimeError(log.read())
                        assert time.perf_counter() - started < 20
                        time.sleep(0.005)
                    samples.setdefault("cold_ready", []).append(
                        time.perf_counter() - started
                    )
                    app_log = session / "app.log"
                    while "Demo ready" not in app_log.read_text():
                        assert time.perf_counter() - started < 20
                        time.sleep(0.005)
                    if cold:
                        continue
                    for name, arguments in {
                        "click": ("click", "120", "100"),
                        "key": ("key", "Right"),
                        "ascii": ("type", "--interval", "0", "HelloGUI"),
                    }.items():
                        run(session, *arguments)
                        values: list[float] = []
                        for _ in range(args.samples):
                            if name == "ascii":
                                run(session, "key", "Ctrl+a")
                            start = time.perf_counter()
                            run(session, *arguments)
                            values.append(time.perf_counter() - start)
                        samples[name] = values
                    plan = Path(temporary) / "batch.json"
                    for name in ("sequence_separate", "sequence_batch"):
                        values = []
                        for index in range(args.samples + 1):
                            run(session, "click", "700", "513")
                            offset = app_log.stat().st_size
                            capture = Path(temporary) / f"{name}-{index}.png"
                            plan.write_text(
                                json.dumps(
                                    {
                                        "actions": [
                                            {"action": "click", "x": 120, "y": 100},
                                            {
                                                "action": "type",
                                                "text": "HelloGUI",
                                                "interval": 0,
                                            },
                                            {"action": "key", "chord": "Return"},
                                        ],
                                        "capture": {"path": str(capture)},
                                    }
                                )
                            )
                            start = time.perf_counter()
                            if name == "sequence_batch":
                                run(session, "batch", "--file", str(plan))
                            else:
                                run(session, "click", "120", "100")
                                run(session, "type", "--interval", "0", "HelloGUI")
                                run(session, "key", "Return")
                                run(session, "screenshot", str(capture))
                            deadline = time.monotonic() + 2
                            while (
                                b"Entered: HelloGUI\n"
                                not in app_log.read_bytes()[offset:]
                            ):
                                if time.monotonic() >= deadline:
                                    raise RuntimeError(
                                        "Demo did not acknowledge the sequence"
                                    )
                                time.sleep(0.005)
                            if index:
                                values.append(time.perf_counter() - start)
                        samples[name] = values
                finally:
                    runner.terminate()
                    runner.wait(timeout=20)
    versions = {}
    for tool, flag in [
        ("sway", "--version"),
        ("wayvnc", "--version"),
        ("Xwayland", "-version"),
    ]:
        result = subprocess.run(
            [tool, flag], capture_output=True, text=True, check=False
        )
        versions[tool] = (result.stdout + result.stderr).strip()
    report = {
        "host": platform.platform(),
        "cpu": next(
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        "vncdotool": importlib.metadata.version("vncdotool"),
        "python": sys.version,
        "backend": "x11" if args.x11 else "wayland",
        "versions": versions,
        "logging": True,
        "recording": False,
        "samples_seconds": samples,
        "summary_seconds": {
            name: {
                "p50": statistics.median(values),
                "p95": sorted(values)[math.ceil(len(values) * 0.95) - 1],
            }
            for name, values in samples.items()
        },
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary_seconds"], indent=2))


if __name__ == "__main__":
    main()
