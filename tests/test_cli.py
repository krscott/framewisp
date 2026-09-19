import subprocess
import sys
from pathlib import Path

import pytest


def test_cli_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0
    assert "--session" in result.stdout
    assert "screenshot" in result.stdout


@pytest.mark.parametrize("delay", ["-1", "nan", "inf", "nope"])
def test_invalid_screenshot_delay(delay: str, tmp_path: Path) -> None:
    destination = tmp_path / "capture.png"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            "--session",
            str(tmp_path / "session"),
            "screenshot",
            f"--delay={delay}",
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert "--delay" in result.stderr
    assert not destination.exists()


def test_recording_does_not_overwrite(tmp_path: Path) -> None:
    destination = tmp_path / "existing.mp4"
    destination.write_bytes(b"keep this recording")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            "--session",
            str(tmp_path / "session"),
            "run",
            "--record",
            str(destination),
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert destination.read_bytes() == b"keep this recording"


@pytest.mark.parametrize(
    "name", ["session.json", "sway.log", "wayvnc.log", "recorder.log", "app.log"]
)
def test_recording_does_not_use_session_files(tmp_path: Path, name: str) -> None:
    session = tmp_path / "session"
    destination = session / name
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            "--session",
            str(session),
            "run",
            "--record",
            str(destination),
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode != 0
    assert "reserved for session files" in result.stderr
    assert not session.exists()


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "nope"])
@pytest.mark.parametrize(
    "action,option,arguments",
    [("drag", "--duration", ["0", "0", "100", "100"]), ("type", "--interval", ["abc"])],
)
def test_invalid_input_timing(
    value: str, action: str, option: str, arguments: list[str], tmp_path: Path
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            "--session",
            str(tmp_path / "missing"),
            action,
            f"{option}={value}",
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert option in result.stderr
    assert "session.json" not in result.stderr


@pytest.mark.parametrize(
    "chord",
    [
        "",
        "Ctrl",
        "Ctrl+",
        "+a",
        "Ctrl++a",
        "Ctrl+CTRL+a",
        "Super+a",
        "Ctrl+unknown",
        "a+b",
        "Ctrl+é",
    ],
)
def test_invalid_key_combination(chord: str, tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            "--session",
            str(tmp_path / "missing"),
            "key",
            chord,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert "unsupported key combination" in result.stderr
    assert "session.json" not in result.stderr


@pytest.mark.parametrize("steps", ["0", "-1", "1.5", "nope"])
def test_invalid_scroll_steps(steps: str, tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            "--session",
            str(tmp_path / "missing"),
            "scroll",
            "100",
            "100",
            "down",
            f"--steps={steps}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert "--steps" in result.stderr
    assert "session.json" not in result.stderr


@pytest.mark.parametrize(
    "option,value",
    [
        ("--button", "middle"),
        ("--button", "nope"),
        ("--count", "0"),
        ("--count", "-1"),
        ("--count", "3"),
        ("--count", "1.5"),
        ("--count", "nope"),
    ],
)
def test_invalid_click_options(option: str, value: str, tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            "--session",
            str(tmp_path / "missing"),
            "click",
            f"{option}={value}",
            "100",
            "100",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert option in result.stderr
    assert "session.json" not in result.stderr


@pytest.mark.parametrize(
    "action,coordinates",
    [("click", ["100", "100"]), ("drag", ["100", "100", "200", "200"])],
)
@pytest.mark.parametrize(
    "options",
    [
        ["--modifier", "super"],
        ["--modifier", "Ctrl", "--modifier", "ctrl"],
        ["--button", "middle"],
    ],
)
def test_invalid_pointer_gesture(
    action: str, coordinates: list[str], options: list[str], tmp_path: Path
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            "--session",
            str(tmp_path / "missing"),
            action,
            *options,
            *coordinates,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert options[0] in result.stderr
    assert "session.json" not in result.stderr
