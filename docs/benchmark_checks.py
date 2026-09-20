"""Compare fixed delays, client polling, and conditional batches on wait_probe.py."""

import argparse
import json
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
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--delay-ms", type=int, default=350)
    args = parser.parse_args()
    if args.samples < 1 or args.delay_ms < 0:
        parser.error("samples must be positive and delay-ms nonnegative")
    measurements: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="fw-check-bench-") as directory:
        root = Path(directory)
        session = root / "session"
        command = [sys.executable, "-m", "framewisp", str(session)]
        with (root / "runner.log").open("w") as log:
            runner = subprocess.Popen(
                [
                    *command,
                    "run",
                    "--",
                    sys.executable,
                    str(
                        Path(__file__).resolve().parents[1] / "tests" / "wait_probe.py"
                    ),
                    str(args.delay_ms),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        try:
            deadline = time.monotonic() + 15
            while (
                not (session / "session.json").exists()
                or "Wait probe ready" not in (session / "app.log").read_text()
            ):
                if runner.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError((root / "runner.log").read_text())
                time.sleep(0.05)

            def run(*arguments: str) -> dict[str, Any]:
                process = subprocess.run(
                    [*command, *arguments], capture_output=True, text=True, timeout=15
                )
                if process.returncode not in (0, 1):
                    raise RuntimeError(process.stderr)
                return cast(dict[str, Any], json.loads(process.stdout))

            plan = root / "plan.json"
            for index in range(args.samples):
                for mode in ("fixed_sleep", "client_polling", "conditional"):
                    text = f"sample{index}{mode}"
                    condition = {
                        "role": "label",
                        "name": "Result",
                        "field": "text",
                        "equals": text,
                    }
                    actions: list[dict[str, object]] = [
                        {"action": "key", "chord": "Ctrl+a"},
                        {"action": "type", "text": text, "interval": 0},
                        {"action": "key", "chord": "Return"},
                    ]
                    if mode == "conditional":
                        actions.append(
                            {"action": "wait", "timeout": 5, "condition": condition}
                        )
                    plan.write_text(json.dumps({"actions": actions}))
                    started = time.monotonic()
                    responses = [run("batch", "--file", str(plan))]
                    wait_started = time.monotonic()
                    calls = 1
                    retries = 0
                    if mode == "conditional":
                        success = responses[0]["verified"] is True
                        local_wait = responses[0]["results"][-1]["duration_seconds"]
                    else:
                        if mode == "fixed_sleep":
                            time.sleep(0.5)
                        success = False
                        while time.monotonic() - wait_started < 5:
                            observation = run(
                                "inspect",
                                "--json",
                                "--role",
                                "label",
                                "--name",
                                "Result",
                            )
                            responses.append(observation)
                            calls += 1
                            success = (
                                observation["status"] == "ok"
                                and observation["match_count"] == 1
                                and observation["matches"][0]["text"] == text
                            )
                            if success or mode == "fixed_sleep":
                                break
                            retries += 1
                            time.sleep(0.05)
                        local_wait = time.monotonic() - wait_started
                    measurements.append(
                        {
                            "mode": mode,
                            "sample": index,
                            "success": success,
                            "retries": retries,
                            "local_wait_seconds": local_wait,
                            "cli_wall_seconds": time.monotonic() - started,
                            "cli_calls": calls,
                            "responses": responses,
                        }
                    )
        finally:
            runner.terminate()
            runner.wait(timeout=15)
    summary = {}
    for mode in ("fixed_sleep", "client_polling", "conditional"):
        samples = [row for row in measurements if row["mode"] == mode]
        summary[mode] = {
            "successes": sum(bool(row["success"]) for row in samples),
            "samples": len(samples),
            "retries": sum(cast(int, row["retries"]) for row in samples),
            **{
                key: statistics.median(cast(float, row[key]) for row in samples)
                for key in ("local_wait_seconds", "cli_wall_seconds", "cli_calls")
            },
        }
    args.output.write_text(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "delay_ms": args.delay_ms,
                "summary": summary,
                "samples": measurements,
                "agent_wall_seconds": None,
                "tool_model_turns": None,
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
