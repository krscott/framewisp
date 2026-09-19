import json
import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from PIL import Image, ImageChops


@dataclass(frozen=True)
class Demo:
    directory: Path
    process: subprocess.Popen[bytes]
    runtime: Path
    child_pids: list[int]


def wait_until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, "Demo did not reach the expected state"
        time.sleep(0.05)


def cli(directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["framewisp", "--session", str(directory), *arguments],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )


@pytest.fixture
def demo(tmp_path: Path) -> Iterator[Demo]:
    directory = tmp_path / "session"
    runner_log = tmp_path / "runner.log"
    with runner_log.open("w") as output:
        process = subprocess.Popen(
            ["framewisp", "--session", str(directory), "run", "--", "framewisp-demo"],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    try:

        def ready() -> bool:
            assert process.poll() is None, runner_log.read_text()
            return "Session ready:" in runner_log.read_text()

        wait_until(ready)
        state = json.loads((directory / "session.json").read_text())
        yield Demo(
            directory=directory,
            process=process,
            runtime=Path(state["runtime_directory"]),
            child_pids=list(state["processes"].values()),
        )
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=20)


@pytest.mark.integration
def test_agent_can_see_type_and_click(demo: Demo, tmp_path: Path) -> None:
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"

    def app_is_visible() -> bool:
        cli(demo.directory, "screenshot", str(before))
        with Image.open(before) as image:
            assert image.format == "PNG"
            assert image.size == (1280, 720)
            # The label contains text once the app has painted, unlike the empty output.
            return len(image.crop((40, 210, 440, 240)).getcolors() or []) > 1

    wait_until(app_is_visible)
    with Image.open(before) as image:
        original_entry = image.crop((50, 85, 400, 115))
        original_label = image.crop((40, 210, 440, 240))

    cli(demo.directory, "click", "120", "100")
    cli(demo.directory, "type", "Hello Wayland!")
    cli(demo.directory, "key", "BackSpace")
    cli(demo.directory, "key", "Return")
    wait_until(
        lambda: "Entered: Hello Wayland\n" in (demo.directory / "app.log").read_text()
    )
    cli(demo.directory, "click", "120", "170")
    wait_until(
        lambda: "Applied: Hello Wayland\n" in (demo.directory / "app.log").read_text()
    )

    def result_is_visible() -> bool:
        cli(demo.directory, "screenshot", str(after))
        with Image.open(after) as image:
            entry = image.crop((50, 85, 400, 115))
            label = image.crop((40, 210, 440, 240))
        return (
            ImageChops.difference(original_entry, entry).getbbox() is not None
            and ImageChops.difference(original_label, label).getbbox() is not None
        )

    wait_until(result_is_visible)
    # Tab focuses the entry and GTK selects its text, so typing replaces it.
    cli(demo.directory, "key", "Tab")
    cli(demo.directory, "type", "?")
    cli(demo.directory, "key", "Return")
    wait_until(lambda: "Entered: ?\n" in (demo.directory / "app.log").read_text())

    demo.process.send_signal(signal.SIGTERM)
    assert demo.process.wait(timeout=20) == 0
    assert not (demo.directory / "session.json").exists()
    assert not demo.runtime.exists()
    for pid in demo.child_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.integration
def test_screenshot_waits_before_capture(demo: Demo, tmp_path: Path) -> None:
    destination = tmp_path / "delayed.png"
    started = time.time()
    cli(demo.directory, "screenshot", "--delay", "0.5", str(destination))
    # Check the file's write time so sleeping after capture would fail this test.
    assert destination.stat().st_mtime >= started + 0.5
    with Image.open(destination) as image:
        assert image.format == "PNG"
        assert image.size == (1280, 720)


@pytest.mark.integration
def test_interrupt_stops_session(demo: Demo) -> None:
    demo.process.send_signal(signal.SIGINT)
    assert demo.process.wait(timeout=20) == 0
    assert not demo.runtime.exists()
    for pid in demo.child_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
