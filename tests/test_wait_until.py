import subprocess
import sys
from itertools import count
from pathlib import Path
from typing import Any, cast

import pytest
import test_integration
from test_integration import (
    Demo,
    MAX_OBSERVED_CHARS,
    wait_for_input_probe_window,
    wait_until,
)


@pytest.fixture(autouse=True)
def short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(test_integration, "WAIT_TIMEOUT_SECONDS", 0.2)


def test_timeout_describes_multiline_lambda_and_its_observation(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text("Demo ready\n")

    with pytest.raises(AssertionError) as failure:
        wait_until(
            lambda: any(
                line == "Never written" for line in log.read_text().splitlines()
            )
        )

    message = str(failure.value)
    assert "test_wait_until.py:" in message
    assert (
        "waiting for: any((line == 'Never written' for line in "
        "log.read_text().splitlines()))" in message
    )
    assert r"read_text() -> 'Demo ready\n'" in message


def test_timeout_reports_only_the_last_poll() -> None:
    polls = count(1)

    def first_poll_only() -> int:
        return 0

    def latest_poll() -> int:
        return next(polls)

    seen: list[int] = []

    def polled() -> bool:
        seen.append(first_poll_only() if not seen else latest_poll())
        return False

    with pytest.raises(AssertionError) as failure:
        wait_until(polled)

    message = str(failure.value)
    assert f"latest_poll() -> {seen[-1]}" in message
    assert "first_poll_only()" not in message


def test_timeout_names_a_named_predicate_and_keeps_the_observation_tail() -> None:
    text = "old" + "x" * MAX_OBSERVED_CHARS + "new"

    def latest() -> str:
        return text

    def ready() -> bool:
        return latest() == ""

    with pytest.raises(AssertionError) as failure:
        wait_until(ready)

    message = str(failure.value)
    assert "waiting for: ready\n" in message
    assert "new'" in message
    assert "old" not in message


def test_success_does_not_leave_a_profiler_behind() -> None:
    wait_until(lambda: True)

    assert sys.getprofile() is None


def test_input_probe_wait_keeps_its_own_timeout_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.log").write_text('{"event": "ready"}\nGTK mapped it\n')

    def empty_tree(runtime: Path, kind: int) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(test_integration, "sway_request", empty_tree)
    demo = Demo(
        directory=tmp_path,
        process=cast("subprocess.Popen[bytes]", None),
        runtime=tmp_path,
        child_pids=[],
        recording=None,
    )

    with pytest.raises(AssertionError) as failure:
        wait_for_input_probe_window(demo)

    message = str(failure.value)
    assert message.startswith("Sway showed no window titled 'Input probe' within")
    assert "Sway windows: none" in message
    assert "GTK mapped it" in message
    assert (tmp_path / "sway-tree.json").read_text() == "{}"
