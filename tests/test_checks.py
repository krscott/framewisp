from collections.abc import Callable
from pathlib import Path

import pytest

from framewisp import inspection
from framewisp.batch import Batch
from framewisp.checks import Check, Condition, Observation, perform_check
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


@pytest.mark.parametrize(
    "options",
    [
        None,
        [],
        True,
        {"limit": 1},
        {"max_nodes": 0},
        {"max_nodes": 4097},
        {"max_nodes": True},
        {"max_nodes": 1.5},
        {"max_nodes": "256"},
        {"max_depth": 0},
        {"max_depth": 33},
        {"max_depth": False},
        {"max_depth": 8.0},
        {"timeout": 0},
        {"timeout": 11},
        {"timeout": True},
        {"timeout": "5"},
        {"timeout": float("nan")},
        {"timeout": float("inf")},
    ],
)
@pytest.mark.parametrize("action", ["baseline", "wait", "assert"])
def test_invalid_observation_options(options: object, action: str) -> None:
    with pytest.raises(ValueError, match=r"actions\[1\].*observation"):
        Batch.parse(
            {
                "actions": [
                    {"action": "key", "chord": "Return"},
                    {
                        "action": action,
                        "condition": condition(),
                        "timeout": 5,
                        "observation": options,
                    },
                ]
            }
        )


def test_observation_round_trip_and_transition_matching() -> None:
    baseline = {
        "action": "baseline",
        "condition": condition(),
        "timeout": 5,
        "observation": {"max_nodes": 1024, "max_depth": 16, "timeout": 5},
    }
    wait = {
        "action": "wait",
        "condition": condition(),
        "timeout": 10,
        "after": 0,
        "observation": {"max_nodes": 4096, "max_depth": 32, "timeout": 10},
    }
    batch = Batch.parse({"actions": [baseline, wait]})
    assert Batch.parse(batch.parameters()) == batch
    assert isinstance(batch.actions[0], Check)
    assert batch.actions[0].observation == Observation(1024, 16, 5)
    assert Observation.parse({}) == Observation()
    with pytest.raises(ValueError, match="300 seconds"):
        Batch.parse(
            {
                "actions": [
                    {key: value for key, value in wait.items() if key != "after"}
                ]
                * 31
            }
        )


def readable_observation(
    *, text: str = "Done", status: str = "ok", reasons: list[str] | None = None
) -> dict[str, object]:
    return {
        "status": status,
        "snapshot_id": "fresh",
        "matches": [{"text": text, "states": [], "role": "label"}],
        "reasons": reasons or [],
    }


@pytest.mark.parametrize("action", ["baseline", "wait", "assert"])
def test_query_settings_and_authoritative_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str
) -> None:
    clock = [0.0]
    monkeypatch.setattr("framewisp.checks.time.monotonic", lambda: clock[0])
    queries: list[inspection.Query] = []
    cancelled = lambda: False

    def inspect(
        session: Path,
        query: inspection.Query,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        assert session == tmp_path
        assert cancelled is not None and not cancelled()
        queries.append(query)
        clock[0] += 3
        return readable_observation(text="Waiting" if action == "baseline" else "Done")

    monkeypatch.setattr(inspection, "inspect_session", inspect)
    check = Check.parse(
        action,
        {
            "condition": condition(),
            "timeout": 4,
            "observation": {"max_nodes": 1024, "max_depth": 16, "timeout": 5},
        },
    )
    result: dict[str, object] = {}
    perform_check(tmp_path, check, result, cancelled=cancelled, wait=lambda _: None)
    assert queries == [
        inspection.Query(
            role="label",
            name="Result",
            limit=2,
            max_nodes=1024,
            max_depth=16,
            timeout=4,
        )
    ]
    assert result["verified"] is (action != "baseline")
    assert result["observation_settings"] == check.observation.parameters()

    clock[0] = 0
    check = Check.parse(
        action, {"condition": condition(), "timeout": 2, "observation": {"timeout": 5}}
    )
    with pytest.raises(SessionError, match="timed out"):
        perform_check(tmp_path, check, {}, cancelled=cancelled, wait=lambda _: None)
    assert queries[-1].timeout == 2


def test_wait_partial_responses_and_remaining_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = [0.0]
    monkeypatch.setattr("framewisp.checks.time.monotonic", lambda: clock[0])
    queries: list[inspection.Query] = []

    def inspect(
        session: Path,
        query: inspection.Query,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        queries.append(query)
        clock[0] += query.timeout
        return readable_observation(
            status="partial" if query.timeout == 2 else "timeout",
            reasons=["max-nodes"] if query.timeout == 2 else ["timeout"],
        )

    def wait(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(inspection, "inspect_session", inspect)
    check = Check.parse("wait", {"condition": condition(), "timeout": 5})
    result: dict[str, object] = {}
    with pytest.raises(
        SessionError, match=r"observation.max_nodes.*longer check timeout alone"
    ):
        perform_check(tmp_path, check, result, cancelled=lambda: False, wait=wait)
    assert [q.timeout for q in queries] == pytest.approx([2, 2, 0.9])
    assert result["observations"] == 3
    assert result["verified"] is False
    assert clock[0] == 5


def test_complete_poll_clears_earlier_caps_and_bus_failure_stops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = [0.0]
    monkeypatch.setattr("framewisp.checks.time.monotonic", lambda: clock[0])
    observations = iter(
        [
            readable_observation(status="partial", reasons=["max-depth"]),
            readable_observation(text="Waiting"),
        ]
    )

    def inspect(
        session: Path,
        query: inspection.Query,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        clock[0] += 1
        return next(observations)

    monkeypatch.setattr(inspection, "inspect_session", inspect)
    check = Check.parse("wait", {"condition": condition(), "timeout": 2})
    with pytest.raises(SessionError, match="timed out") as failure:
        perform_check(
            tmp_path,
            check,
            {},
            cancelled=lambda: False,
            wait=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        )
    assert "max_depth" not in str(failure.value)

    observations = iter(
        [readable_observation(status="unavailable", reasons=["unavailable"])]
    )
    result: dict[str, object] = {}
    with pytest.raises(SessionError, match="bus is unavailable"):
        perform_check(
            tmp_path, check, result, cancelled=lambda: False, wait=lambda _: None
        )
    assert result["observations"] == 1


@pytest.mark.parametrize("action", ["baseline", "assert"])
@pytest.mark.parametrize(
    "reason,field",
    [("max-depth", "max_depth"), ("max-nodes", "max_nodes"), ("timeout", "timeout")],
)
def test_incomplete_check_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
    reason: str,
    field: str,
) -> None:
    def inspect(
        session: Path,
        query: inspection.Query,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        return readable_observation(text="Waiting", status="partial", reasons=[reason])

    monkeypatch.setattr(inspection, "inspect_session", inspect)
    check = Check.parse(action, {"condition": condition(), "timeout": 5})
    with pytest.raises(SessionError, match=f"observation.{field}"):
        perform_check(tmp_path, check, {}, cancelled=lambda: False, wait=lambda _: None)


@pytest.mark.parametrize("state", ["stale", "defunct"])
def test_stale_text_cannot_verify(state: str) -> None:
    assert (
        Condition.parse(condition()).evaluate(
            {"status": "ok", "matches": [{"text": "Done", "states": [state]}]}
        )
        is None
    )


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
