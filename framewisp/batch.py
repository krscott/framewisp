"""A bounded sequence of steps and an optional final screenshot."""

import math
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from framewisp.actions import InputAction
from framewisp.checks import CHECK_KINDS, Check
from framewisp.connection import MAX_REQUEST_BYTES as MAX_REQUEST_BYTES
from framewisp.pointer import DEFAULT_MOVE_DURATION

MAX_STEPS = 256
MAX_TEXT = 16384
MAX_SCROLL_TICKS = 10000
MAX_PACING_SECONDS = 300

DEFAULTS: dict[str, dict[str, object]] = {
    "move": {"move_duration": DEFAULT_MOVE_DURATION},
    "click": {
        "button": "left",
        "count": 1,
        "modifier": [],
        "move_duration": DEFAULT_MOVE_DURATION,
    },
    "drag": {
        "button": "left",
        "duration": 0.4,
        "modifier": [],
        "move_duration": DEFAULT_MOVE_DURATION,
    },
    "scroll": {"ticks": 1, "move_duration": DEFAULT_MOVE_DURATION},
    "type": {"interval": 0.08},
    "key": {},
}


@dataclass(frozen=True)
class Screenshot:
    path: Path
    delay: float


Step = InputAction | Check


@dataclass(frozen=True)
class Batch:
    steps: tuple[Step, ...]
    screenshot: Screenshot | None
    failure_screenshot: Screenshot | None = None

    @staticmethod
    def parse(raw: object, *, directory: Path | None = None) -> "Batch":
        if not isinstance(raw, dict):
            raise ValueError("Batch must be an object.")
        data = cast(dict[str, object], raw)
        if data.keys() - {"steps", "screenshot", "failure_screenshot"}:
            raise ValueError(
                "Batch supports only steps, screenshot, and failure_screenshot."
            )
        raw_steps = data.get("steps")
        if (
            not isinstance(raw_steps, list)
            or not 1 <= len(cast(list[object], raw_steps)) <= MAX_STEPS
        ):
            raise ValueError(f"steps must contain 1 to {MAX_STEPS} steps.")
        steps: list[Step] = []
        pacing = 0.0
        characters = 0
        scroll_ticks = 0
        for index, raw_step in enumerate(cast(list[object], raw_steps)):
            try:
                if not isinstance(raw_step, dict):
                    raise ValueError("Expected a step object.")
                item = cast(dict[str, object], raw_step)
                if ("action" in item) == ("check" in item):
                    raise ValueError("A step needs exactly one of action or check.")
                if "check" in item:
                    check = Check.parse(item)
                    if check.after is not None:
                        if check.after >= index:
                            raise ValueError(
                                "after must reference an earlier baseline."
                            )
                        baseline = steps[check.after]
                        if (
                            not isinstance(baseline, Check)
                            or baseline.kind != "baseline"
                            or baseline.condition != check.condition
                        ):
                            raise ValueError(
                                "after must reference a baseline with the same condition."
                            )
                    steps.append(check)
                    pacing += check.timeout
                    continue
                name = item["action"]
                if name in CHECK_KINDS:
                    raise ValueError(f'{name} is a check; write "check": "{name}".')
                if not isinstance(name, str) or name not in DEFAULTS:
                    raise ValueError("Unsupported batch action.")
                parameters = DEFAULTS[name] | {
                    key: value for key, value in item.items() if key != "action"
                }
                action = InputAction.parse(name, parameters)
                if parameters.keys() != action.parameters.keys():
                    raise ValueError("Unknown action parameter.")
                steps.append(action)
                if name in {"move", "click", "drag", "scroll"}:
                    pacing += cast(float, parameters["move_duration"])
                if name == "type":
                    length = len(cast(str, parameters["text"]))
                    characters += length
                    pacing += max(0, length - 1) * cast(float, parameters["interval"])
                elif name == "drag":
                    pacing += cast(float, parameters["duration"])
                elif name == "scroll":
                    scroll_ticks += cast(int, parameters["ticks"])
            except (ValueError, TypeError, OverflowError) as error:
                raise ValueError(f"steps[{index}]: {error}") from None
        screenshots: dict[str, Screenshot] = {}
        for kind in ("screenshot", "failure_screenshot"):
            if kind not in data:
                continue
            raw_screenshot = data[kind]
            if not isinstance(raw_screenshot, dict):
                raise ValueError(
                    f"{kind} must be an object with path and optional delay."
                )
            options = cast(dict[str, object], raw_screenshot)
            if options.keys() - {"path", "delay"}:
                raise ValueError(f"Unknown {kind} parameter.")
            path = options.get("path")
            if not isinstance(path, str) or not path or "\0" in path:
                raise ValueError(f"{kind}.path must be a nonempty filesystem path.")
            destination = Path(path)
            if directory is not None:
                destination = (directory / destination).resolve()
            if not destination.is_absolute():
                raise ValueError(f"{kind}.path must be absolute in session requests.")
            delay = options.get("delay", 0.0)
            if (
                type(delay) not in {int, float}
                or not isinstance(delay, (int, float))
                or not math.isfinite(delay)
                or delay < 0
            ):
                raise ValueError(f"{kind}.delay must be finite and nonnegative.")
            pacing += delay
            screenshots[kind] = Screenshot(destination, delay)
        if characters > MAX_TEXT or scroll_ticks > MAX_SCROLL_TICKS:
            raise ValueError(
                f"Batch exceeds {MAX_TEXT} text characters or {MAX_SCROLL_TICKS} scroll ticks."
            )
        if not math.isfinite(pacing) or pacing > MAX_PACING_SECONDS:
            raise ValueError(
                f"Batch exceeds {MAX_PACING_SECONDS} seconds of requested pacing."
            )
        return Batch(
            tuple(steps),
            screenshots.get("screenshot"),
            screenshots.get("failure_screenshot"),
        )

    def parameters(self) -> dict[str, object]:
        result: dict[str, object] = {
            "steps": [step_parameters(step) for step in self.steps]
        }
        for name, screenshot in (
            ("screenshot", self.screenshot),
            ("failure_screenshot", self.failure_screenshot),
        ):
            if screenshot is not None:
                result[name] = {"path": str(screenshot.path), "delay": screenshot.delay}
        return result


def step_name(step: Step) -> dict[str, str]:
    """The key that names a step's kind: action for input, check for a check."""
    if isinstance(step, Check):
        return {"check": step.kind}
    return {"action": step.action}


def step_parameters(step: Step) -> dict[str, object]:
    return {**step_name(step), **step.parameters}
