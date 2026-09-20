from pathlib import Path

import pytest

from framewisp.batch import Batch
from framewisp.checks import Condition
from framewisp.errors import SessionError


@pytest.mark.parametrize(
    "condition",
    [
        {},
        {"role": "label", "field": "text", "equals": True},
        {"role": "", "field": "text", "equals": "x"},
        {"id": "old-id", "field": "text", "equals": "x"},
        {"role": "checkbox", "field": "checked", "equals": 1},
        {"role": "slider", "field": "value", "equals": float("nan")},
        {"role": "slider", "field": "value", "equals": False},
        {"role": "button", "field": "absent", "equals": True},
        {"role": "label", "field": "text", "equals": "x" * 1025},
    ],
)
def test_invalid_condition(condition: object) -> None:
    with pytest.raises(ValueError):
        Condition.parse(condition)


def condition() -> dict[str, object]:
    return {"role": "label", "name": "Result", "field": "text", "equals": "Done"}


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"timeout": 0},
        {"timeout": True},
        {"timeout": 11},
        {"timeout": float("nan")},
        {"timeout": 1, "after": 0},
        {"timeout": 1, "interval": 0},
    ],
)
def test_invalid_wait(extra: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        Batch.parse(
            {"actions": [{"action": "wait", "condition": condition(), **extra}]}
        )


def test_baseline_reference_and_round_trip(tmp_path: Path) -> None:
    baseline = {"action": "baseline", "condition": condition(), "timeout": 1}
    wait = {"action": "wait", "condition": condition(), "timeout": 2, "after": 0}
    batch = Batch.parse(
        {
            "actions": [baseline, {"action": "key", "chord": "Return"}, wait],
            "failure_capture": {"path": "failed.png"},
        },
        directory=tmp_path,
    )
    assert Batch.parse(batch.parameters()) == batch
    for actions in (
        [wait],
        [{"action": "key", "chord": "Return"}, wait],
        [baseline, wait | {"condition": condition() | {"equals": "Other"}}],
    ):
        with pytest.raises(ValueError):
            Batch.parse({"actions": actions})
    with pytest.raises(ValueError, match="300 seconds"):
        Batch.parse({"actions": [baseline | {"timeout": 10}] * 31})


@pytest.mark.parametrize("status", ["partial", "timeout", "unsupported", "unavailable"])
def test_incomplete_observations_cannot_verify(status: str) -> None:
    check = Condition.parse(condition())
    assert check.evaluate({"status": status, "matches": [{"text": "Done"}]}) is None


def test_missing_and_duplicate_matches() -> None:
    check = Condition.parse(condition())
    assert check.evaluate({"status": "ok", "matches": []}) is None
    with pytest.raises(SessionError, match="ambiguous"):
        check.evaluate({"status": "partial", "matches": [{}, {}]})


@pytest.mark.parametrize(
    "field,expected,role,states,value",
    [
        ("checked", False, "label", [], None),
        ("checked", False, "checkbox", ["indeterminate"], None),
        ("checked", False, "checkbox", [], True),
        ("checked", True, "checkbox", ["checked"], True),
        ("enabled", True, "button", ["sensitive"], True),
        ("enabled", False, "button", [], True),
        ("enabled", False, "button", ["defunct"], None),
    ],
)
def test_state_semantics(
    field: str, expected: bool, role: str, states: list[str], value: bool | None
) -> None:
    check = Condition.parse({"role": role, "field": field, "equals": expected})
    assert (
        check.evaluate({"status": "ok", "matches": [{"role": role, "states": states}]})
        is value
    )
