import re
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import test_integration
from test_integration import Demo, wait_for_input_probe_window, wait_until


@pytest.fixture
def short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(test_integration, "WAIT_TIMEOUT_SECONDS", 0.2)


@pytest.mark.usefixtures("short_timeout")
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
    assert re.match(r"Timed out after \d+\.\d s at test_wait_until\.py:\d+ ", message)
    assert (
        "waiting for: any((line == 'Never written' for line in "
        "log.read_text().splitlines()))\n" in message
    )
    assert message.endswith("Last returned: False")


@pytest.mark.usefixtures("short_timeout")
def test_timeout_shows_a_named_predicate_source_and_its_last_value() -> None:
    def ready() -> list[int]:
        return []

    with pytest.raises(AssertionError) as failure:
        wait_until(ready)

    assert (
        "waiting for: def ready() -> list[int]:\n    return []\nLast returned: []"
        in str(failure.value)
    )


@pytest.mark.usefixtures("short_timeout")
def test_timeout_falls_back_when_the_call_cannot_be_found() -> None:
    with pytest.raises(AssertionError, match="waiting for: the condition\n"):
        test_integration.wait_until(lambda: False)


@pytest.mark.parametrize("error", [ValueError("Bad state"), AssertionError("App died")])
def test_predicate_exception_propagates_on_the_first_call(error: Exception) -> None:
    polls: list[int] = []

    def raises() -> bool:
        polls.append(1)
        raise error

    with pytest.raises(type(error)) as failure:
        wait_until(raises)

    assert failure.value is error
    assert polls == [1]


def test_truthy_value_ends_the_wait() -> None:
    values = iter([[], 0, None, [1]])

    wait_until(lambda: next(values))

    assert list(values) == []


@pytest.mark.usefixtures("short_timeout")
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
