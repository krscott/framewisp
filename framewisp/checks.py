"""Positive conditions over fresh, unambiguous accessibility observations."""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from framewisp.errors import SessionError

MAX_CONDITION_TEXT = 1024


@dataclass(frozen=True)
class Condition:
    role: str | None
    name: str | None
    field: str
    equals: str | float | bool

    @staticmethod
    def parse(raw: object) -> "Condition":
        if not isinstance(raw, dict):
            raise ValueError("condition must be an object.")
        data = cast(dict[str, object], raw)
        if data.keys() - {"role", "name", "field", "equals"}:
            raise ValueError("Unknown condition field.")
        role, name = data.get("role"), data.get("name")
        for selector in (role, name):
            if selector is not None and (
                not isinstance(selector, str)
                or not 1 <= len(selector) <= MAX_CONDITION_TEXT
            ):
                raise ValueError("role/name must be nonempty bounded strings.")
        if role is None and name is None:
            raise ValueError("condition requires role or name.")
        field, equals = data.get("field"), data.get("equals")
        if field in ("text", "name"):
            valid = isinstance(equals, str) and len(equals) <= MAX_CONDITION_TEXT
        elif field in ("checked", "enabled"):
            valid = type(equals) is bool
        elif field == "value":
            valid = type(equals) in (int, float) and math.isfinite(cast(float, equals))
        else:
            valid = False
        if not valid:
            raise ValueError(
                "Use text/name with a string, checked/enabled with a boolean, or value with a finite number."
            )
        return Condition(
            cast(str | None, role),
            cast(str | None, name),
            cast(str, field),
            cast(str | float | bool, equals),
        )

    def parameters(self) -> dict[str, object]:
        result: dict[str, object] = {"field": self.field, "equals": self.equals}
        return result | {
            key: value
            for key, value in (("role", self.role), ("name", self.name))
            if value is not None
        }

    def evaluate(self, observation: dict[str, object]) -> bool | None:
        matches = cast(list[dict[str, object]], observation["matches"])
        if len(matches) > 1:
            raise SessionError("Condition selector is ambiguous; narrow role/name.")
        if observation["status"] != "ok" or not matches:
            return None
        node = matches[0]
        states = cast(list[str], node["states"])
        if "defunct" in states or "stale" in states:
            return None
        if self.field == "checked":
            if (
                node["role"]
                not in (
                    "checkbox",
                    "check box",
                    "check menu item",
                    "radio button",
                    "radio menu item",
                    "toggle button",
                )
                and "checkable" not in states
            ):
                return None
            if "indeterminate" in states:
                return None
            value: object = "checked" in states
        elif self.field == "enabled":
            # GTK reports sensitive without enabled; Qt reports both.
            value = "sensitive" in states or "enabled" in states
        else:
            value = node[self.field]
        return None if value is None else value == self.equals


@dataclass(frozen=True)
class Observation:
    max_nodes: int = 256
    max_depth: int = 8
    timeout: float = 2

    @staticmethod
    def parse(raw: object) -> "Observation":
        if not isinstance(raw, dict):
            raise ValueError("observation must be an object.")
        data = cast(dict[str, object], raw)
        if data.keys() - {"max_nodes", "max_depth", "timeout"}:
            raise ValueError("Unknown observation parameter.")
        defaults = Observation()
        nodes = data.get("max_nodes", defaults.max_nodes)
        depth = data.get("max_depth", defaults.max_depth)
        for name, value, maximum in (
            ("max_nodes", nodes, 4096),
            ("max_depth", depth, 32),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"observation.{name} must be between 1 and {maximum}.")
        timeout = data.get("timeout", defaults.timeout)
        if type(timeout) not in (int, float) or not 0 < cast(float, timeout) <= 10:
            raise ValueError(
                "observation.timeout must be greater than 0 and at most 10 seconds."
            )
        return Observation(cast(int, nodes), cast(int, depth), cast(float, timeout))

    def parameters(self) -> dict[str, object]:
        return {
            "max_nodes": self.max_nodes,
            "max_depth": self.max_depth,
            "timeout": self.timeout,
        }


@dataclass(frozen=True)
class Check:
    action: str
    condition: Condition
    timeout: float
    after: int | None = None
    observation: Observation = Observation()

    @staticmethod
    def parse(action: str, data: dict[str, object]) -> "Check":
        if data.keys() - {"action", "condition", "timeout", "after", "observation"}:
            raise ValueError("Unknown check parameter.")
        timeout = data.get("timeout")
        if type(timeout) not in (int, float) or not 0 < cast(float, timeout) <= 10:
            raise ValueError(
                "Checks require a timeout greater than 0 and at most 10 seconds."
            )
        after = data.get("after")
        if "after" in data and (
            type(after) is not int or after < 0 or action != "wait"
        ):
            raise ValueError("Only wait accepts after, a prior baseline action index.")
        return Check(
            action,
            Condition.parse(data.get("condition")),
            cast(float, timeout),
            cast(int | None, after),
            Observation.parse(data.get("observation", {})),
        )

    @property
    def parameters(self) -> dict[str, object]:
        result: dict[str, object] = {
            "condition": self.condition.parameters(),
            "timeout": self.timeout,
            "observation": self.observation.parameters(),
        }
        if self.after is not None:
            result["after"] = self.after
        return result


def perform_check(
    session: Path,
    check: Check,
    result: dict[str, object],
    *,
    cancelled: Callable[[], bool],
    wait: Callable[[float], None],
) -> None:
    # Batch parsing also serves lightweight CLI commands; load Gio only for execution.
    from framewisp.inspection import Query, inspect_session

    deadline = time.monotonic() + check.timeout
    result.update(
        condition=check.condition.parameters(),
        observation=None,
        observations=0,
        verified=False,
        observation_settings=check.observation.parameters(),
    )
    reasons: set[str] = set()

    def failure(message: str) -> SessionError:
        hints: list[str] = []
        for reason, field, maximum in (
            ("max-nodes", "max_nodes", 4096),
            ("max-depth", "max_depth", 32),
        ):
            if reason in reasons:
                current = check.observation.parameters()[field]
                if current == maximum:
                    hints.append(
                        f"observation.{field} reached its maximum ({maximum}); narrow the app tree or use a screenshot."
                    )
                else:
                    hints.append(
                        f"Raise observation.{field} (currently {current}, maximum {maximum}); a longer check timeout alone cannot fix this traversal cap."
                    )
        if "timeout" in reasons:
            hints.append(
                f"Observation time budget exhausted (observation.timeout={check.observation.timeout}, check timeout={check.timeout}); each query is capped by the remaining check deadline."
            )
            if "app-unresponsive" in reasons:
                hints.append("An app call was unresponsive; retry or use a screenshot.")
            elif check.observation.timeout == 10 and check.timeout == 10:
                hints.append(
                    "Both time budgets are at their maximum; retry or use a screenshot."
                )
            else:
                hints.append(
                    "Increase observation.timeout and the check timeout if needed (maximum 10 seconds each), or use a screenshot."
                )
        return SessionError(" ".join([message, *hints]))

    while True:
        wait(0)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise failure("Condition timed out before a complete matching observation.")
        observation = inspect_session(
            session,
            Query(
                role=check.condition.role,
                name=check.condition.name,
                limit=2,
                max_nodes=check.observation.max_nodes,
                max_depth=check.observation.max_depth,
                timeout=min(check.observation.timeout, remaining),
            ),
            cancelled=cancelled,
        )
        result["observation"] = observation
        result["observations"] = cast(int, result["observations"]) + 1
        # A final short query must not hide structural caps from earlier polls.
        if observation["status"] == "ok":
            reasons.clear()
        reasons.update(cast(list[str], observation["reasons"]))
        wait(0)
        matched = check.condition.evaluate(observation)
        if time.monotonic() > deadline:
            raise failure("Condition timed out before a complete matching observation.")
        if check.action == "baseline":
            if matched is not False:
                raise failure(
                    "Transition baseline requires one complete, readable, nonmatching control."
                )
            result["baseline_snapshot_id"] = observation["snapshot_id"]
            return
        if matched is True:
            result["verified"] = True
            return
        if check.action == "assert":
            raise failure(
                "Assertion failed: condition is false or observation is incomplete."
            )
        if observation["status"] == "unavailable":
            raise SessionError("Accessibility bus is unavailable.")
        wait(min(0.05, max(0, deadline - time.monotonic())))
