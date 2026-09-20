"""Validation shared by individual and batched headless inputs."""

import math
from dataclasses import dataclass
from typing import cast

from framewisp.keys import CLICK_BUTTONS, MODIFIERS, SCROLL_BUTTONS, key_commands


@dataclass(frozen=True)
class InputAction:
    action: str
    parameters: dict[str, object]

    @staticmethod
    def parse(action: object, raw: object) -> "InputAction":
        if not isinstance(raw, dict) or not isinstance(action, str):
            raise ValueError("Input requires an action and parameters object.")
        p = cast(dict[str, object], raw)
        fields = {
            "move": ("x", "y"),
            "click": ("x", "y", "button", "count", "modifier"),
            "drag": ("x1", "y1", "x2", "y2", "duration", "button", "modifier"),
            "scroll": ("x", "y", "direction", "steps"),
            "type": ("text", "interval"),
            "key": ("chord",),
        }
        if action not in fields or any(name not in p for name in fields[action]):
            raise ValueError("Unsupported or incomplete input action.")
        p = {name: p[name] for name in fields[action]}
        for name, value in p.items():
            if name in {"x", "y", "x1", "y1", "x2", "y2", "count", "steps"}:
                if type(value) is not int:
                    raise ValueError(f"{name} must be an integer.")
                if (
                    name in {"x", "y", "x1", "y1", "x2", "y2"}
                    and not 0 <= value <= 65535
                ):
                    raise ValueError("Coordinates must be between 0 and 65535.")
            elif name in {"duration", "interval"}:
                if (
                    type(value) not in {int, float}
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ValueError(f"{name} must be finite and nonnegative.")
            elif name == "modifier":
                if not isinstance(value, list):
                    raise ValueError("modifier must be a list.")
                values = cast(list[object], value)
                if any(
                    not isinstance(item, str) or item not in MODIFIERS
                    for item in values
                ) or len(set(cast(list[str], values))) != len(values):
                    raise ValueError("Invalid or duplicate modifier.")
            elif not isinstance(value, str):
                raise ValueError(f"{name} must be a string.")
        if "button" in p and p["button"] not in CLICK_BUTTONS:
            raise ValueError("Unsupported button.")
        if "count" in p and p["count"] not in (1, 2):
            raise ValueError("Click count must be 1 or 2.")
        if "steps" in p and cast(int, p["steps"]) < 1:
            raise ValueError("Scroll steps must be positive.")
        if "direction" in p and p["direction"] not in SCROLL_BUTTONS:
            raise ValueError("Unsupported scroll direction.")
        if action == "type" and not all(c.isprintable() for c in cast(str, p["text"])):
            raise ValueError("type supports printable characters only.")
        if action == "key" and key_commands(cast(str, p["chord"])) is None:
            raise ValueError("Unsupported key combination.")
        if action == "drag" and not math.isfinite(cast(float, p["duration"]) * 60):
            raise ValueError("Drag duration is too large.")
        if action == "type":
            interval = cast(float, p["interval"])
            length = len(cast(str, p["text"]))
            if not math.isfinite(interval * 1000) or not math.isfinite(
                15 + max(0, length - 1) * interval + length * 0.004
            ):
                raise ValueError("Typing interval or total duration is too large.")
        return InputAction(action, p)
