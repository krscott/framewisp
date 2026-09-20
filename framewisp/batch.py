"""A bounded sequence of input actions and an optional final screenshot."""

import math
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from framewisp.actions import InputAction

MAX_REQUEST_BYTES = 1024 * 1024
MAX_ACTIONS = 256
MAX_TEXT = 16384
MAX_SCROLL_STEPS = 10000
MAX_PACING_SECONDS = 300

DEFAULTS: dict[str, dict[str, object]] = {
    "move": {},
    "click": {"button": "left", "count": 1, "modifier": []},
    "drag": {"button": "left", "duration": 0.4, "modifier": []},
    "scroll": {"steps": 1},
    "type": {"interval": 0.08},
    "key": {},
}


@dataclass(frozen=True)
class Capture:
    path: Path
    delay: float


@dataclass(frozen=True)
class Batch:
    actions: tuple[InputAction, ...]
    capture: Capture | None

    @staticmethod
    def parse(raw: object, *, directory: Path | None = None) -> "Batch":
        if not isinstance(raw, dict):
            raise ValueError("Batch must be an object.")
        data = cast(dict[str, object], raw)
        if data.keys() - {"actions", "capture"}:
            raise ValueError("Batch supports only actions and capture.")
        entries = data.get("actions")
        if (
            not isinstance(entries, list)
            or not 1 <= len(cast(list[object], entries)) <= MAX_ACTIONS
        ):
            raise ValueError(f"actions must contain 1 to {MAX_ACTIONS} inputs.")
        actions: list[InputAction] = []
        pacing = 0.0
        characters = 0
        scroll_steps = 0
        for index, entry in enumerate(cast(list[object], entries)):
            try:
                if not isinstance(entry, dict):
                    raise ValueError("Expected an action object.")
                item = cast(dict[str, object], entry)
                name = item.get("action")
                if not isinstance(name, str) or name not in DEFAULTS:
                    raise ValueError("Unsupported batch action.")
                parameters = DEFAULTS[name] | {
                    key: value for key, value in item.items() if key != "action"
                }
                action = InputAction.parse(name, parameters)
                if parameters.keys() != action.parameters.keys():
                    raise ValueError("Unknown action parameter.")
                actions.append(action)
                if name == "type":
                    length = len(cast(str, parameters["text"]))
                    characters += length
                    pacing += max(0, length - 1) * cast(float, parameters["interval"])
                elif name == "drag":
                    pacing += cast(float, parameters["duration"])
                elif name == "scroll":
                    scroll_steps += cast(int, parameters["steps"])
            except (ValueError, TypeError, OverflowError) as error:
                raise ValueError(f"actions[{index}]: {error}") from None
        capture = None
        if "capture" in data:
            raw_capture = data["capture"]
            if not isinstance(raw_capture, dict):
                raise ValueError(
                    "capture must be an object with path and optional delay."
                )
            options = cast(dict[str, object], raw_capture)
            if options.keys() - {"path", "delay"}:
                raise ValueError("Unknown capture parameter.")
            path = options.get("path")
            if not isinstance(path, str) or not path or "\0" in path:
                raise ValueError("capture.path must be a nonempty filesystem path.")
            destination = Path(path)
            if directory is not None:
                destination = (directory / destination).resolve()
            if not destination.is_absolute():
                raise ValueError("capture.path must be absolute in runner requests.")
            delay = options.get("delay", 0.0)
            if (
                type(delay) not in {int, float}
                or not isinstance(delay, (int, float))
                or not math.isfinite(delay)
                or delay < 0
            ):
                raise ValueError("capture.delay must be finite and nonnegative.")
            pacing += delay
            capture = Capture(destination, delay)
        if characters > MAX_TEXT or scroll_steps > MAX_SCROLL_STEPS:
            raise ValueError(
                f"Batch exceeds {MAX_TEXT} text characters or {MAX_SCROLL_STEPS} scroll steps."
            )
        if not math.isfinite(pacing) or pacing > MAX_PACING_SECONDS:
            raise ValueError(
                f"Batch exceeds {MAX_PACING_SECONDS} seconds of requested pacing."
            )
        return Batch(tuple(actions), capture)

    def parameters(self) -> dict[str, object]:
        result: dict[str, object] = {
            "actions": [
                {"action": action.action, **action.parameters}
                for action in self.actions
            ]
        }
        if self.capture is not None:
            result["capture"] = {
                "path": str(self.capture.path),
                "delay": self.capture.delay,
            }
        return result
