import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from framewisp.layout import ConsolePanel
from framewisp.lib import session_command


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


def framewisp(session: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "framewisp", str(session), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )


@pytest.mark.parametrize(
    "command",
    [
        ["run", "--record", "clip.mp4", "--console-position", "left"],
        ["run", "--record", "clip.mp4", "--console-size", "640"],
        ["run", "--console-position", "top"],
        ["record-start", "--console-position", "left", "clip.mp4"],
        ["record-start", "--console-size", "640", "clip.mp4"],
    ],
)
def test_console_options_require_console(tmp_path: Path, command: list[str]) -> None:
    session = tmp_path / "session"
    tail = ["--", "framewisp-demo"] if command[0] == "run" else []
    result = framewisp(
        session,
        *[str(tmp_path / part) if part == "clip.mp4" else part for part in command],
        *tail,
    )
    assert result.returncode == 2
    assert "--console-position and --console-size require --console" in result.stderr
    assert not session.exists()
    assert not (tmp_path / "clip.mp4").exists()


def test_console_options_with_console_still_require_recording(tmp_path: Path) -> None:
    session = tmp_path / "session"
    result = framewisp(
        session, "run", "--console", "--console-position", "left", "--", "app"
    )
    assert result.returncode == 2
    assert "--console requires --record" in result.stderr
    assert not session.exists()


# R-REC-16
@pytest.mark.parametrize(
    ("options", "message"),
    [
        (["--console-position", "middle"], "invalid choice"),
        (["--console-size", "many"], "--console-size"),
        (["--console-size", "0"], "positive integer"),
        (["--console-size", "-2"], "--console-size"),
        (["--console-size", "641"], "use 642 instead of 641"),
        (["--console-size", "16386"], "at most 16384"),
        pytest.param(
            ["--console-size", "9" * 4299 + "8"], "at most 16384", id="unformattable"
        ),
        (["--console-size", "388"], "at least 390"),
        (["--console-position", "top", "--console-size", "134"], "at least 136"),
    ],
)
@pytest.mark.parametrize("action", ["run", "record-start"])
def test_recording_rejects_invalid_panels_before_any_clip(
    tmp_path: Path, action: str, options: list[str], message: str
) -> None:
    session = tmp_path / "session"
    clip = tmp_path / "clip.mp4"
    command = (
        ["run", "--record", str(clip), "--console", *options, "--", "framewisp-demo"]
        if action == "run"
        else ["record-start", "--console", *options, str(clip)]
    )
    result = framewisp(session, *command)
    assert result.returncode == 2
    assert message in result.stderr
    assert not session.exists()
    assert not clip.exists()


# R-REC-16
@pytest.mark.parametrize(
    ("options", "named"),
    [
        (["--height", "100"], ["1280x100", "taller"]),
        (
            ["--width", "320", "--height", "240", "--console-position", "bottom"],
            ["320x240", "wider"],
        ),
        (["--console-size", "16000"], ["17280x720", "smaller --console-size"]),
    ],
)
def test_run_rejects_a_panel_the_display_cannot_hold(
    tmp_path: Path, options: list[str], named: list[str]
) -> None:
    session = tmp_path / "session"
    clip = tmp_path / "clip.mp4"
    result = framewisp(
        session,
        "run",
        "--record",
        str(clip),
        "--console",
        *options,
        "--",
        "framewisp-demo",
    )
    assert result.returncode == 2
    assert all(text in result.stderr for text in named), result.stderr
    assert not session.exists()
    assert not clip.exists()


def test_console_requires_recording(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path / "session"),
            "run",
            "--console",
            "--",
            "framewisp-demo",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert "--console requires --record" in result.stderr
    assert not (tmp_path / "session").exists()


def test_compact_requires_recording(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path / "session"),
            "run",
            "--compact",
            "--",
            "framewisp-demo",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert "--compact requires --record" in result.stderr
    assert not (tmp_path / "session").exists()


def test_compact_rejects_unsupported_runner(tmp_path: Path) -> None:
    (tmp_path / "session.json").write_text(
        json.dumps({"control_protocol": 1, "caption_modes": True})
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path),
            "record-start",
            "--compact",
            str(tmp_path / "clip.mp4"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert "does not support compact recordings. Restart" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("capability", [None, False, 1])
def test_console_rejects_unsupported_runner_before_connecting(
    tmp_path: Path, capability: object
) -> None:
    state: dict[str, object] = {
        "control_protocol": 1,
        "caption_modes": True,
        "console_recording": True,
    }
    if capability is not None:
        state["console_panels"] = capability
    (tmp_path / "session.json").write_text(json.dumps(state))
    destination = tmp_path / "clip.mp4"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path),
            "record-start",
            "--console",
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert "does not support console panel options. Restart" in result.stderr
    assert "Traceback" not in result.stderr
    assert not destination.exists()


@pytest.mark.parametrize(
    ("panel", "expected"),
    [(None, None), (ConsolePanel("top", 200), {"position": "top", "size": 200})],
)
def test_record_start_sends_console_only_for_a_panel(
    panel: ConsolePanel | None, expected: dict[str, object] | None
) -> None:
    received: list[dict[str, object]] = []

    def runner(server: socket.socket) -> None:
        connection, _ = server.accept()
        with connection, connection.makefile("rw") as stream:
            received.append(json.loads(stream.readline()))
            stream.write(json.dumps({"error": None, "data": None}) + "\n")

    # Pytest's own paths can exceed the Unix socket path limit.
    with tempfile.TemporaryDirectory() as directory:
        session = Path(directory)
        (session / "session.json").write_text(
            json.dumps(
                {
                    "control_protocol": 1,
                    "caption_modes": True,
                    "click_pings_recording": True,
                    "console_panels": True,
                    "runtime_directory": directory,
                }
            )
        )
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(session / "control.sock"))
            server.listen()
            thread = threading.Thread(target=runner, args=(server,), daemon=True)
            thread.start()
            result = session_command(
                session, "record-start", session / "clip.mp4", console=panel
            )
            thread.join(timeout=5)
    assert result == 0
    # An older runner rejects a null `console`.
    assert ("console" in received[0]) is (panel is not None)
    assert received[0].get("console") == expected


@pytest.mark.parametrize("capability", [None, False, 1])
def test_default_clip_rejects_runner_without_click_pings(
    tmp_path: Path, capability: object
) -> None:
    state: dict[str, object] = {
        "control_protocol": 1,
        "caption_modes": True,
        "runtime_directory": str(tmp_path),
    }
    if capability is not None:
        state["click_pings_recording"] = capability
    (tmp_path / "session.json").write_text(json.dumps(state))
    destination = tmp_path / "clip.mp4"

    def record(*options: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "framewisp",
                str(tmp_path),
                "record-start",
                *options,
                str(destination),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )

    rejected = record()
    assert rejected.returncode == 1
    assert "does not support click pings" in rejected.stderr
    assert "Restart the session" in rejected.stderr
    assert "--no-click-pings" in rejected.stderr
    assert "Traceback" not in rejected.stderr
    # The opt-out reaches the runner's socket, which this directory lacks.
    assert "click pings" not in record("--no-click-pings").stderr
    assert not destination.exists()


@pytest.mark.parametrize("capability", [None, False, 1])
def test_caption_modes_reject_unsupported_runner_before_connecting(
    tmp_path: Path, capability: object
) -> None:
    state: dict[str, object] = {"control_protocol": 1}
    if capability is not None:
        state["caption_modes"] = capability
    (tmp_path / "session.json").write_text(json.dumps(state))
    destination = tmp_path / "clip.mp4"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path),
            "record-start",
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert "does not support caption modes. Restart the session" in result.stderr
    assert "Traceback" not in result.stderr
    assert not destination.exists()


def test_run_no_click_pings_requires_record(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path / "session"),
            "run",
            "--no-click-pings",
            "--",
            "framewisp-demo",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert "--no-click-pings requires --record" in result.stderr
    assert not (tmp_path / "session").exists()


@pytest.mark.parametrize("mode", ["all", "none"])
def test_run_captions_requires_record(tmp_path: Path, mode: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path / "session"),
            "run",
            "--captions",
            mode,
            "--",
            "framewisp-demo",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert "--captions requires --record" in result.stderr
    assert not (tmp_path / "session").exists()


@pytest.mark.parametrize("action", ["run", "record-start"])
def test_captions_rejects_unknown_mode(tmp_path: Path, action: str) -> None:
    destination = tmp_path / "clip.mp4"
    arguments = (
        ["--record", str(destination), "--captions", "mouse", "--", "framewisp-demo"]
        if action == "run"
        else ["--captions", "mouse", str(destination)]
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path / "session"),
            action,
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert "invalid choice" in result.stderr and "pointer" in result.stderr
    assert not (tmp_path / "session").exists()


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


# R-INPUT-3
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


# R-REC-1
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
        ".headless.lock",
        ".headless-owner.json",
        ".headless-owner.tmp",
        "session.json",
        ".session.json",
        "app-exit.json",
        ".app-exit.json",
        "sway.log",
        "wayvnc.log",
        "recorder.log",
        "app.log",
        "console.jsonl",
        "inputs.jsonl",
        "activity.jsonl",
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


# R-INPUT-3
@pytest.mark.parametrize("value", ["-1", "nan", "inf", "nope"])
@pytest.mark.parametrize(
    "action,option,arguments",
    [
        ("drag", "--duration", ["0", "0", "100", "100"]),
        ("type", "--interval", ["abc"]),
        ("move", "--move-duration", ["1", "1"]),
        ("click", "--move-duration", ["1", "1"]),
        ("scroll", "--move-duration", ["1", "1", "down"]),
        ("drag", "--move-duration", ["1", "1", "2", "2"]),
    ],
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


# R-INPUT-3
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


# R-INPUT-3
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


# R-INPUT-3
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


# R-INPUT-3
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


# R-INPUT-3
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


# R-REC-2
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


# R-INPUT-3
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
