import json
import os
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
    assert "SESSION" in result.stdout
    assert "screenshot" in result.stdout
    assert "--agent-skill" in result.stdout


def test_agent_skill_without_runtime_dependencies(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    script = (
        "import runpy, sys; "
        f"sys.path.insert(0, {str(root)!r}); "
        "sys.argv = ['framewisp', '--agent-skill']; "
        "runpy.run_module('framewisp', run_name='__main__'); "
        "assert 'gi' not in sys.modules; "
        "assert 'framewisp.cli' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-E", "-S", "-c", script],
        cwd=tmp_path,
        env={"PATH": ""},
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == (root / "framewisp" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert result.stderr == ""
    assert not list(tmp_path.iterdir())


def test_agent_skill_rejects_session_command(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            "--agent-skill",
            str(tmp_path / "session"),
            "run",
            "--",
            "framewisp-demo",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert "--agent-skill must be used alone" in result.stderr
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("action", ["status", "stop", "record-stop"])
def test_control_commands_reject_legacy_session_before_connecting(
    tmp_path: Path, action: str
) -> None:
    # A legacy runner treats any request with destination=null as record-stop.
    # No runtime path is needed: rejection must precede even resolving its socket.
    (tmp_path / "session.json").write_text(json.dumps({"processes": {"recorder": 123}}))
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", str(tmp_path), action],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert "older or unsupported control protocol" in result.stderr
    assert "start a new session" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("delay", ["-1", "nan", "inf", "nope"])
def test_invalid_screenshot_delay(delay: str, tmp_path: Path) -> None:
    destination = tmp_path / "capture.png"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
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
    "name",
    [
        "session.json",
        ".session.json",
        "sway.log",
        "wayvnc.log",
        "recorder.log",
        "app.log",
        "inputs.jsonl",
        "captions.log",
        "dbus.log",
        "accessibility.log",
        "registry.log",
    ],
)
def test_recording_does_not_use_session_files(tmp_path: Path, name: str) -> None:
    session = tmp_path / "session"
    destination = session / name
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
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


@pytest.mark.parametrize("option", ["--width", "--height"])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "nope"])
def test_invalid_display_size(option: str, value: str, tmp_path: Path) -> None:
    session = tmp_path / "session"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(session),
            "run",
            f"{option}={value}",
            "--",
            "framewisp-demo",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert option in result.stderr
    assert not session.exists()


@pytest.mark.parametrize("option", ["--width", "--height"])
def test_recording_rejects_odd_dimensions(option: str, tmp_path: Path) -> None:
    session = tmp_path / "session"
    recording = tmp_path / "recording.mp4"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(session),
            "run",
            option,
            "901",
            "--record",
            str(recording),
            "--",
            "framewisp-demo",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert "even" in result.stderr
    assert not session.exists()
    assert not recording.exists()


@pytest.mark.parametrize(
    "text", ["line\nbreak", "tab\tstop", "escape\x1b", "join\u200der"]
)
def test_type_rejects_nonprintable_text(text: str, tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path / "missing"),
            "type",
            text,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert "printable" in result.stderr
    assert "session.json" not in result.stderr


def test_compositor_failure_includes_log_excerpt(tmp_path: Path) -> None:
    shim = tmp_path / "bin"
    shim.mkdir()
    sway = shim / "sway"
    sway.write_text(
        f"#!{sys.executable}\nimport sys\nprint('Unable to open wayland socket', file=sys.stderr)\nsys.exit(1)\n"
    )
    sway.chmod(0o755)
    # Invoke the source CLI directly: installed wrappers deliberately pin PATH.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path / "failed"),
            "run",
            "--",
            "framewisp-demo",
        ],
        env=os.environ | {"PATH": str(shim) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 1
    assert "Unable to open wayland socket" in result.stderr
    assert "sway.log" in result.stderr and "sandbox" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "failed" / "session.json").exists()


@pytest.mark.parametrize(
    "state,expected",
    [({"kind": "attached"}, "headless sessions only"), ({}, "Restart the session")],
)
@pytest.mark.parametrize("options", [["--json"], ["--region", "0", "0", "10", "10"]])
def test_screenshot_options_reject_unsupported_sessions(
    tmp_path: Path, state: dict[str, str], expected: str, options: list[str]
) -> None:
    (tmp_path / "session.json").write_text(json.dumps(state))
    destination = tmp_path / "capture.png"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path),
            "screenshot",
            str(destination),
            *options,
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert expected in result.stderr
    assert "Traceback" not in result.stderr
    assert not result.stdout and not destination.exists()


def test_screenshot_json_rejects_png_stdout(tmp_path: Path) -> None:
    (tmp_path / "session.json").write_text("{}")
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", str(tmp_path), "screenshot", "--json", "-"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert "requires a file path" in result.stderr
    assert not result.stdout
