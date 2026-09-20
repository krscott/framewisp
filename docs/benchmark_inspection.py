"""Compare complete inspection and screenshot CLI calls on an already running demo."""

import argparse
import json
import math
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--samples", type=int, default=40)
    args = parser.parse_args()
    if args.samples < 20:
        parser.error("use at least 20 samples for a p95")
    command = [sys.executable, "-m", "framewisp", str(args.session)]

    def run(*arguments: str) -> bytes:
        return subprocess.run(
            [*command, *arguments], capture_output=True, check=True, timeout=15
        ).stdout

    run("click", "700", "513")
    run("type", "--interval", "0", "HelloGUI")
    run("click", "120", "170")
    capture = args.output.with_suffix(".png")
    cases = {
        "find_button": (
            "inspect",
            "--json",
            "--role",
            "button",
            "--name",
            "Apply text",
        ),
        "read_result": (
            "inspect",
            "--json",
            "--role",
            "label",
            "--text",
            "Applied: HelloGUI",
        ),
        "screenshot": ("screenshot", str(capture)),
    }
    measurements: dict[str, dict[str, object]] = {}
    for name, arguments in cases.items():
        run(*arguments)
        times: list[float] = []
        sizes: list[int] = []
        for _ in range(args.samples):
            start = time.perf_counter()
            output = run(*arguments)
            times.append((time.perf_counter() - start) * 1000)
            if name == "screenshot":
                sizes.append(capture.stat().st_size)
            else:
                assert json.loads(output)["match_count"] == 1
                sizes.append(len(output))
        measurements[name] = {
            "p50_ms": statistics.median(times),
            "p95_ms": sorted(times)[math.ceil(len(times) * 0.95) - 1],
            "median_bytes": statistics.median(sizes),
            "samples_ms": times,
            "samples_bytes": sizes,
        }
    versions = {}
    for tool, flag in [("sway", "--version"), ("wayvnc", "--version"), ("grim", "-h")]:
        result = subprocess.run([tool, flag], capture_output=True, text=True, timeout=5)
        versions[tool] = (result.stdout + result.stderr).strip()
    args.output.write_text(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "versions": versions,
                "samples_per_case": args.samples,
                "measurements": measurements,
            },
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                name: {
                    key: value
                    for key, value in data.items()
                    if not key.startswith("samples_")
                }
                for name, data in measurements.items()
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
