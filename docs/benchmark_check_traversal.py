"""Compare default and explicit check budgets on the same large accessible app."""

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--app", choices=("gtk", "qt"), default="gtk")
    parser.add_argument("--x11", action="store_true")
    parser.add_argument("--samples", type=int, default=3)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("samples must be positive")
    budgets = {"max_nodes": 1024, "max_depth": 16, "timeout": 5}
    measurements: list[dict[str, Any]] = []
    logs: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="fw-traversal-bench-") as directory:
        root = Path(directory)
        session = root / "session"
        command = [sys.executable, "-m", "framewisp", str(session)]
        tests = Path(__file__).resolve().parents[1] / "tests"
        app = (
            [
                sys.executable,
                str(tests / "wait_probe.py"),
                "--nodes",
                "300",
                "--depth",
                "10",
            ]
            if args.app == "gtk"
            else ["qml", str(tests / "inspection_probe.qml")]
        )
        with (root / "runner.log").open("w") as log:
            runner = subprocess.Popen(
                [*command, "run", *(["--x11"] if args.x11 else []), "--", *app],
                env=os.environ
                | {
                    "QT_QUICK_BACKEND": "software",
                    "QT_QPA_PLATFORMTHEME": "",
                    "QT_IM_MODULE": "",
                    "QT_LOGGING_RULES": "qml.debug=true",
                    "QT_LOGGING_TO_CONSOLE": "1",
                },
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        try:
            ready = (
                "Wait probe ready" if args.app == "gtk" else "Inspection probe ready"
            )
            deadline = time.monotonic() + 15
            while (
                not (session / "app.log").exists()
                or ready not in (session / "app.log").read_text()
            ):
                if runner.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError((root / "runner.log").read_text())
                time.sleep(0.05)
            condition = (
                {
                    "role": "label",
                    "name": "Result",
                    "field": "text",
                    "equals": "Waiting",
                }
                if args.app == "gtk"
                else {
                    "name": "Control 0",
                    "field": "enabled",
                    "equals": True,
                }
            )
            plan = root / "plan.json"
            for sample in range(args.samples):
                for mode in ("default", "explicit"):
                    for action in ("inspect", "baseline", "wait", "assert"):
                        request: dict[str, object] = {
                            "action": action,
                            "timeout": 5,
                            "condition": condition,
                        }
                        if action == "baseline":
                            request["condition"] = condition | {
                                "equals": "Never" if args.app == "gtk" else False
                            }
                        if mode == "explicit":
                            request["observation"] = budgets
                        plan.write_text(json.dumps({"actions": [request]}))
                        arguments = (
                            [
                                "inspect",
                                "--json",
                                *(
                                    ["--role", str(condition["role"])]
                                    if "role" in condition
                                    else []
                                ),
                                "--name",
                                str(condition["name"]),
                                *(
                                    [
                                        "--max-nodes",
                                        "1024",
                                        "--max-depth",
                                        "16",
                                        "--timeout",
                                        "5",
                                    ]
                                    if mode == "explicit"
                                    else []
                                ),
                            ]
                            if action == "inspect"
                            else ["batch", "--file", str(plan)]
                        )
                        started = time.monotonic()
                        response = subprocess.run(
                            [*command, *arguments],
                            capture_output=True,
                            text=True,
                            timeout=15,
                        )
                        wall = time.monotonic() - started
                        if response.returncode not in (0, 1):
                            raise RuntimeError(response.stderr)
                        result = cast(dict[str, Any], json.loads(response.stdout))
                        measurements.append(
                            {
                                "sample": sample,
                                "mode": mode,
                                "action": action,
                                "cli_wall_seconds": wall,
                                "request": request,
                                "exit_code": response.returncode,
                                "stderr": response.stderr,
                                "arguments": arguments,
                                "success": result["status"] in ("ok", "completed"),
                                "response": result,
                            }
                        )
        finally:
            runner.terminate()
            runner.wait(timeout=15)
            logs = {
                str(path.relative_to(root)): path.read_text()
                for path in root.rglob("*.log")
            }
    summary: dict[str, object] = {}
    for mode in ("default", "explicit"):
        for action in ("inspect", "baseline", "wait", "assert"):
            rows = [
                row
                for row in measurements
                if row["mode"] == mode and row["action"] == action
            ]
            summary[f"{mode}_{action}"] = {
                "successes": sum(row["success"] for row in rows),
                "samples": len(rows),
                "median_cli_wall_seconds": statistics.median(
                    row["cli_wall_seconds"] for row in rows
                ),
            }
    args.output.write_text(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "app": app,
                "x11": args.x11,
                "limits": budgets,
                "check_timeout": 5,
                "summary": summary,
                "samples": measurements,
                "logs": logs,
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
