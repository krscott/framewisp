import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import test_integration
from test_integration import Demo, wait_for_input_probe_window, wait_until


@pytest.fixture(autouse=True)
def short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(test_integration, "WAIT_TIMEOUT_SECONDS", 0.2)


def test_timeout_describes_multiline_lambda_and_elapsed_time(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text("Demo ready\n")

    with pytest.raises(AssertionError) as failure:
        wait_until(
            lambda: any(
                line == "Never written" for line in log.read_text().splitlines()
            )
        )

    message = str(failure.value)
    assert message.startswith("Timed out after 0.2 s at test_wait_until.py:")
    assert (
        "waiting for: any((line == 'Never written' for line in "
        "log.read_text().splitlines()))\n" in message
    )
    assert message.endswith("Last returned: False")


def test_timeout_names_a_named_predicate_and_its_last_value() -> None:
    def ready() -> list[int]:
        return []

    with pytest.raises(AssertionError) as failure:
        wait_until(ready)

    assert "waiting for: ready\nLast returned: []" in str(failure.value)


def test_timeout_reports_the_last_exception_and_chains_it(tmp_path: Path) -> None:
    missing = tmp_path / "app.log"

    with pytest.raises(AssertionError) as failure:
        wait_until(lambda: "ready" in missing.read_text())

    message = str(failure.value)
    assert "Last returned: nothing\n" in message
    assert "Last exception: FileNotFoundError(" in message
    assert isinstance(failure.value.__cause__, FileNotFoundError)


def test_exception_is_retried_until_the_predicate_succeeds(tmp_path: Path) -> None:
    log = tmp_path / "app.log"

    def predicate() -> bool:
        if log.exists():
            return True
        log.write_text("ready\n")
        raise ConnectionRefusedError

    wait_until(predicate)


def test_assertion_in_predicate_fails_without_waiting() -> None:
    polls: list[int] = []

    def app_alive() -> bool:
        polls.append(1)
        raise AssertionError("App exited")

    with pytest.raises(AssertionError, match="^App exited$"):
        wait_until(app_alive)

    assert polls == [1]


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
