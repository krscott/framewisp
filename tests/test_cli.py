import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from framewisp import cli
from framewisp.connection import (
    COMMAND_PROTOCOL,
    DesktopKind,
    DesktopSessionMetadata,
    HeadlessSessionMetadata,
    parse_session_metadata,
    read_session_metadata,
)
from framewisp.input_log import log_input as original_log_input
from framewisp.layout import ConsolePanel
from framewisp.lib import session_command


def headless_metadata(session_dir: Path, **overrides: object) -> dict[str, object]:
    return {
        "kind": "headless",
        "command_protocol": COMMAND_PROTOCOL,
        "accessibility_bus": f"unix:path={session_dir / 'accessibility.sock'}",
        "command_socket": str(session_dir / "command.sock"),
        "runtime_directory": str(session_dir),
        "wayland_display": "wayland-1",
        "width": 1280,
        "height": 720,
        "x11_display": None,
        "clip_destination": None,
        **overrides,
    }


def desktop_metadata(
    session_dir: Path, kind: DesktopKind = "attached", **overrides: object
) -> dict[str, object]:
    return {
        "kind": kind,
        "command_protocol": COMMAND_PROTOCOL,
        "command_socket": str(session_dir / "command.sock"),
        "share_token": "share-token",
        **overrides,
    }


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


def framewisp(session_dir: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "framewisp", str(session_dir), *arguments],
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
        ["run", "--record", "clip.mp4", "--console-cell-scale", "1.1"],
        ["record-start", "--console-cell-scale", "1.1", "clip.mp4"],
    ],
)
def test_console_options_require_console(tmp_path: Path, command: list[str]) -> None:
    session_dir = tmp_path / "session"
    tail = ["--", "framewisp-demo"] if command[0] == "run" else []
    result = framewisp(
        session_dir,
        *[str(tmp_path / part) if part == "clip.mp4" else part for part in command],
        *tail,
    )
    assert result.returncode == 2
    assert (
        "--console-position, --console-size and --console-cell-scale require --console"
        in result.stderr
    )
    assert not session_dir.exists()
    assert not (tmp_path / "clip.mp4").exists()


def test_console_options_with_console_still_require_recording(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    result = framewisp(
        session_dir, "run", "--console", "--console-position", "left", "--", "app"
    )
    assert result.returncode == 2
    assert "--console requires --record" in result.stderr
    assert not session_dir.exists()


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
        (["--console-size", "310"], "at least 312"),
        (["--console-position", "top", "--console-size", "116"], "at least 118"),
        (["--console-cell-scale", "many"], "--console-cell-scale"),
        (["--console-cell-scale", "0.99"], "from 1 to 58"),
        (["--console-cell-scale", "nan"], "from 1 to 58"),
        (["--console-cell-scale", "inf"], "from 1 to 58"),
        (
            ["--console-size", "338", "--console-cell-scale", "1.1"],
            "at --console-cell-scale 1.1; a right console panel needs at least 340",
        ),
    ],
)
@pytest.mark.parametrize("command", ["run", "record-start"])
def test_recording_rejects_invalid_console_panels_before_any_clip(
    tmp_path: Path, command: str, options: list[str], message: str
) -> None:
    session_dir = tmp_path / "session"
    clip = tmp_path / "clip.mp4"
    arguments = (
        ["run", "--record", str(clip), "--console", *options, "--", "framewisp-demo"]
        if command == "run"
        else ["record-start", "--console", *options, str(clip)]
    )
    result = framewisp(session_dir, *arguments)
    assert result.returncode == 2
    assert message in result.stderr
    assert not session_dir.exists()
    assert not clip.exists()


# R-REC-16
@pytest.mark.parametrize(
    ("options", "named"),
    [
        (["--height", "100"], ["1280x100", "taller"]),
        (
            ["--width", "310", "--height", "240", "--console-position", "bottom"],
            ["310x240", "wider"],
        ),
        (["--console-size", "16000"], ["17280x720", "smaller --console-size"]),
    ],
)
def test_run_rejects_a_console_panel_the_display_cannot_hold(
    tmp_path: Path, options: list[str], named: list[str]
) -> None:
    session_dir = tmp_path / "session"
    clip = tmp_path / "clip.mp4"
    result = framewisp(
        session_dir,
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
    assert not session_dir.exists()
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


@pytest.mark.parametrize(
    ("console_panel", "expected"),
    [
        (None, None),
        (
            ConsolePanel("top", 200),
            {"position": "top", "size": 200, "cell_scale": 1.0},
        ),
        (
            ConsolePanel("top", 200, 1.5),
            {"position": "top", "size": 200, "cell_scale": 1.5},
        ),
    ],
)
def test_record_start_sends_console_panel(
    console_panel: ConsolePanel | None, expected: dict[str, object] | None
) -> None:
    received: list[dict[str, object]] = []

    def runner(server: socket.socket) -> None:
        connection, _ = server.accept()
        with connection, connection.makefile("rw") as stream:
            received.append(json.loads(stream.readline()))
            stream.write(json.dumps({"error": None, "data": None}) + "\n")

    # Pytest's own paths can exceed the Unix socket path limit.
    with tempfile.TemporaryDirectory() as directory:
        session_dir = Path(directory)
        session = HeadlessSessionMetadata(
            session_directory=session_dir,
            command_socket=session_dir / "command.sock",
            runtime_directory=session_dir,
            accessibility_bus=f"unix:path={session_dir / 'accessibility.sock'}",
            wayland_display="wayland-1",
            width=1280,
            height=720,
        )
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(session_dir / "command.sock"))
            server.listen()
            thread = threading.Thread(target=runner, args=(server,), daemon=True)
            thread.start()
            result = session_command(
                session,
                "record-start",
                session_dir / "clip.mp4",
                console_panel=console_panel,
            )
            thread.join(timeout=5)
    assert result == 0
    assert received[0]["console_panel"] == expected


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


@pytest.mark.parametrize("flag", ["--pointer-captions", "--key-captions"])
def test_run_captions_requires_record(tmp_path: Path, flag: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path / "session"),
            "run",
            flag,
            "all",
            "--",
            "framewisp-demo",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert "--key-captions require --record" in result.stderr
    assert not (tmp_path / "session").exists()


@pytest.mark.parametrize("command", ["run", "record-start"])
@pytest.mark.parametrize("flag", ["--pointer-captions", "--key-captions"])
def test_captions_rejects_unknown_mode(tmp_path: Path, command: str, flag: str) -> None:
    destination = tmp_path / "clip.mp4"
    arguments = (
        ["--record", str(destination), flag, "mouse", "--", "framewisp-demo"]
        if command == "run"
        else [flag, "mouse", str(destination)]
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path / "session"),
            command,
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


# R-CONTROL-3
@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"command_protocol": COMMAND_PROTOCOL - 1},
        {"command_protocol": COMMAND_PROTOCOL + 1},
        {"command_protocol": float(COMMAND_PROTOCOL)},
        {"command_protocol": True},
        {"command_protocol": str(COMMAND_PROTOCOL)},
        {"kind": "attached"},
        {"kind": "teaching"},
        # The previous version published teaching sessions as attached.
        {"kind": "attached", "command_protocol": 3},
    ],
)
@pytest.mark.parametrize(
    "arguments",
    [
        ("status",),
        ("stop",),
        ("record-stop",),
        ("click", "1", "1"),
        ("type", "a"),
        ("screenshot", "screenshot.png"),
        ("inspect", "--json"),
        ("teach-note", "a note"),
    ],
)
def test_session_commands_refuse_another_command_protocol(
    tmp_path: Path, metadata: dict[str, object], arguments: tuple[str, ...]
) -> None:
    # The metadata names no socket or display, so a command that went on to use
    # the session would fail in another way.
    (tmp_path / "session.json").write_text(json.dumps(metadata))
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", str(tmp_path), *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert "another version of framewisp" in result.stderr
    desktop = metadata.get("kind") in {"attached", "teaching"}
    assert ("Ask the user" in result.stderr) is desktop
    recover = shlex.join(["framewisp", str(tmp_path), "recover"])
    assert (f"`{recover}` with the framewisp version" in result.stderr) is not desktop
    assert "again with this version" in result.stderr
    assert ("a new lesson directory" in result.stderr) is (
        metadata.get("kind") in {"attached", "teaching"}
    )
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "screenshot.png").exists()


@pytest.mark.parametrize("kind", [[], {}, "other"])
@pytest.mark.parametrize("protocol", [COMMAND_PROTOCOL, None])
@pytest.mark.parametrize("arguments", [("stop",), ("type", "a")])
def test_session_commands_refuse_an_unknown_session_kind(
    tmp_path: Path, kind: object, protocol: int | None, arguments: tuple[str, ...]
) -> None:
    (tmp_path / "session.json").write_text(
        json.dumps({"kind": kind, "command_protocol": protocol})
    )
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", str(tmp_path), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert (
        "publishes no known session kind"
        if protocol == COMMAND_PROTOCOL
        else "another version of framewisp"
    ) in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "inputs.jsonl").exists()


# R-CONTROL-3
def test_screenshot_checks_the_command_protocol_after_its_delay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    metadata = tmp_path / "session.json"
    metadata.write_text(json.dumps({"command_protocol": COMMAND_PROTOCOL}))
    waits: list[float] = []

    def another_version_takes_the_directory(seconds: float) -> None:
        waits.append(seconds)
        metadata.write_text(json.dumps({"command_protocol": COMMAND_PROTOCOL + 1}))

    monkeypatch.setattr(time, "sleep", another_version_takes_the_directory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["framewisp", str(tmp_path), "screenshot", "--delay", "3", "screenshot.png"],
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1
    assert waits == [3.0]
    assert "another version of framewisp" in capsys.readouterr().err
    assert not (tmp_path / "screenshot.png").exists()


# R-INPUT-3
@pytest.mark.parametrize("delay", ["-1", "nan", "inf", "nope"])
def test_invalid_screenshot_delay(delay: str, tmp_path: Path) -> None:
    destination = tmp_path / "screenshot.png"
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


@pytest.mark.parametrize("flag", ["--key-captions", "--pointer-captions"])
def test_record_start_requires_an_explicit_caption_mode(
    tmp_path: Path, flag: str
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path),
            "record-start",
            str(tmp_path / "clip.mp4"),
            flag,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert "expected one argument" in result.stderr


# R-REC-1
def test_recording_does_not_overwrite(tmp_path: Path) -> None:
    destination = tmp_path / "existing.mp4"
    destination.write_bytes(b"keep this file")
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
    assert destination.read_bytes() == b"keep this file"


@pytest.mark.parametrize(
    "name",
    [
        ".session.lock",
        ".recovery-journal.json",
        ".recovery-journal.tmp",
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
        "finalization.log",
        "dbus.log",
        "accessibility.log",
        "registry.log",
    ],
)
def test_recording_does_not_use_session_files(tmp_path: Path, name: str) -> None:
    session_dir = tmp_path / "session"
    destination = session_dir / name
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(session_dir),
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
    assert not session_dir.exists()


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
@pytest.mark.parametrize("ticks", ["0", "-1", "1.5", "nope"])
def test_invalid_scroll_ticks(ticks: str, tmp_path: Path) -> None:
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
            f"--ticks={ticks}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert "--ticks" in result.stderr
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
    session_dir = tmp_path / "session"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(session_dir),
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
    assert not session_dir.exists()


# R-REC-2
@pytest.mark.parametrize("option", ["--width", "--height"])
def test_recording_rejects_odd_dimensions(option: str, tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    clip = tmp_path / "clip.mp4"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(session_dir),
            "run",
            option,
            "901",
            "--record",
            str(clip),
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
    assert not session_dir.exists()
    assert not clip.exists()


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


@pytest.mark.parametrize("options", [["--json"], ["--region", "0", "0", "10", "10"]])
def test_screenshot_options_reject_attached_sessions(
    tmp_path: Path, options: list[str]
) -> None:
    (tmp_path / "session.json").write_text(json.dumps(desktop_metadata(tmp_path)))
    destination = tmp_path / "screenshot.png"
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
    assert "headless sessions only" in result.stderr
    assert "Traceback" not in result.stderr
    assert not result.stdout and not destination.exists()


def test_screenshot_json_rejects_png_stdout(tmp_path: Path) -> None:
    (tmp_path / "session.json").write_text(json.dumps(headless_metadata(tmp_path)))
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", str(tmp_path), "screenshot", "--json", "-"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert "requires a file path" in result.stderr
    assert not result.stdout


def test_parse_valid_session_metadata(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    headless = parse_session_metadata(
        headless_metadata(tmp_path, x11_display=":42", clip_destination=str(clip)),
        tmp_path,
    )
    assert headless == HeadlessSessionMetadata(
        session_directory=tmp_path,
        command_socket=tmp_path / "command.sock",
        runtime_directory=tmp_path,
        accessibility_bus=f"unix:path={tmp_path / 'accessibility.sock'}",
        wayland_display="wayland-1",
        width=1280,
        height=720,
        x11_display=":42",
        clip_destination=clip,
    )
    for kind in ("attached", "teaching"):
        desktop = parse_session_metadata(desktop_metadata(tmp_path, kind), tmp_path)
        assert desktop == DesktopSessionMetadata(
            session_directory=tmp_path,
            kind=kind,
            command_socket=tmp_path / "command.sock",
            share_token="share-token",
        )


def test_read_session_metadata_handles_missing_and_malformed_files(
    tmp_path: Path,
) -> None:
    missing = read_session_metadata(tmp_path)
    assert isinstance(missing, str) and "Session is disconnected" in missing
    (tmp_path / "session.json").write_text("{")
    bad_json = read_session_metadata(tmp_path)
    assert isinstance(bad_json, str) and "Cannot read session metadata" in bad_json
    (tmp_path / "session.json").write_text("[]")
    not_object = read_session_metadata(tmp_path)
    assert isinstance(not_object, str) and "must be a JSON object" in not_object


@pytest.mark.parametrize(
    "field",
    [
        "command_socket",
        "runtime_directory",
        "accessibility_bus",
        "wayland_display",
        "width",
        "height",
        "x11_display",
        "clip_destination",
    ],
)
def test_headless_metadata_refuses_missing_field(tmp_path: Path, field: str) -> None:
    raw = headless_metadata(tmp_path)
    del raw[field]
    result = parse_session_metadata(raw, tmp_path)
    assert isinstance(result, str)
    assert field in result and "missing" in result


@pytest.mark.parametrize("kind", ["attached", "teaching"])
@pytest.mark.parametrize("field", ["command_socket", "share_token"])
def test_desktop_metadata_refuses_missing_field(
    tmp_path: Path, kind: DesktopKind, field: str
) -> None:
    raw = desktop_metadata(tmp_path, kind)
    del raw[field]
    result = parse_session_metadata(raw, tmp_path)
    assert isinstance(result, str)
    assert field in result and "missing" in result


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command_socket", ""),
        ("command_socket", "relative.sock"),
        ("command_socket", "/tmp/bad\0.sock"),
        ("command_socket", 123),
        ("runtime_directory", "relative"),
        ("runtime_directory", None),
        ("accessibility_bus", ""),
        ("accessibility_bus", 42),
        ("wayland_display", ""),
        ("wayland_display", False),
        ("width", 0),
        ("width", -10),
        ("width", True),
        ("width", 1280.0),
        ("width", "1280"),
        ("height", 0),
        ("height", False),
        ("x11_display", ""),
        ("x11_display", 0),
        ("clip_destination", ""),
        ("clip_destination", "relative.mp4"),
        ("clip_destination", 5),
    ],
)
def test_headless_metadata_refuses_wrongly_typed_field(
    tmp_path: Path, field: str, value: object
) -> None:
    raw = headless_metadata(tmp_path, **{field: value})
    result = parse_session_metadata(raw, tmp_path)
    assert isinstance(result, str)
    assert field in result and "invalid" in result


@pytest.mark.parametrize("kind", ["attached", "teaching"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command_socket", "relative.sock"),
        ("command_socket", None),
        ("share_token", ""),
        ("share_token", 123),
    ],
)
def test_desktop_metadata_refuses_wrongly_typed_field(
    tmp_path: Path, kind: DesktopKind, field: str, value: object
) -> None:
    raw = desktop_metadata(tmp_path, kind, **{field: value})
    result = parse_session_metadata(raw, tmp_path)
    assert isinstance(result, str)
    assert field in result and "invalid" in result


@pytest.mark.parametrize(
    ("metadata_kind", "field", "value", "arguments"),
    [
        ("headless", "width", None, ("screenshot", "--json", "out.png")),
        ("headless", "command_socket", "relative.sock", ("status",)),
        ("headless", "runtime_directory", 123, ("inspect", "--json")),
        ("attached", "share_token", None, ("click", "1", "1")),
        ("teaching", "command_socket", None, ("teach-note", "note")),
    ],
)
def test_cli_refuses_metadata_with_missing_or_wrongly_typed_field(
    tmp_path: Path,
    metadata_kind: str,
    field: str,
    value: object,
    arguments: tuple[str, ...],
) -> None:
    if metadata_kind == "headless":
        raw = headless_metadata(tmp_path)
    else:
        raw = desktop_metadata(
            tmp_path, "teaching" if metadata_kind == "teaching" else "attached"
        )
    if value is None:
        del raw[field]
    else:
        raw[field] = value
    (tmp_path / "session.json").write_text(json.dumps(raw))
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", str(tmp_path), *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert field in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "out.png").exists()


# R-CONTROL-3
def test_attached_input_does_not_reread_metadata_after_input_log_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replacing session.json while waiting on inputs.jsonl cannot redirect a command."""
    received_original: list[dict[str, object]] = []

    def serve(server: socket.socket) -> None:
        server.settimeout(5)
        connection, _ = server.accept()
        with connection, connection.makefile("rw") as stream:
            received_original.append(json.loads(stream.readline()))
            stream.write(json.dumps({"status": 0, "error": None}) + "\n")

    with tempfile.TemporaryDirectory(prefix="fw-toctou-") as directory:
        sock_dir = Path(directory)
        original_sock = sock_dir / "original.sock"
        replacement_sock = sock_dir / "replacement.sock"
        (tmp_path / "session.json").write_text(
            json.dumps(
                desktop_metadata(
                    tmp_path,
                    "attached",
                    command_socket=str(original_sock),
                    share_token="original-token",
                )
            )
        )
        (tmp_path / "inputs.jsonl").write_text("")

        @contextmanager
        def log_input_during_takeover(
            session_dir: Path, action: str, parameters: dict[str, object]
        ) -> Generator[object, None, None]:
            with original_log_input(session_dir, action, parameters) as logged:
                # Another version takes over session.json while this command
                # was waiting on the input log lock.
                (tmp_path / "session.json").write_text(
                    json.dumps(
                        desktop_metadata(
                            tmp_path,
                            "attached",
                            command_protocol=COMMAND_PROTOCOL + 1,
                            command_socket=str(replacement_sock),
                            share_token="replacement-token",
                        )
                    )
                )
                yield logged

        with (
            socket.socket(socket.AF_UNIX) as original_server,
            socket.socket(socket.AF_UNIX) as replacement_server,
        ):
            original_server.bind(str(original_sock))
            original_server.listen(1)
            replacement_server.bind(str(replacement_sock))
            replacement_server.listen(1)
            replacement_server.setblocking(False)
            original_thread = threading.Thread(
                target=serve, args=(original_server,), daemon=True
            )
            original_thread.start()
            monkeypatch.setattr(cli, "log_input", log_input_during_takeover)
            monkeypatch.setattr(
                sys, "argv", ["framewisp", str(tmp_path), "click", "1", "1"]
            )
            with pytest.raises(SystemExit) as stopped:
                cli.main()
            original_thread.join(timeout=5)
            with pytest.raises(BlockingIOError):
                replacement_server.accept()
    assert stopped.value.code == 0
    assert len(received_original) == 1
    assert received_original[0]["share_token"] == "original-token"


def test_screenshot_reads_session_metadata_once_after_delay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    metadata_path = tmp_path / "session.json"
    metadata_path.write_text(json.dumps(headless_metadata(tmp_path)))
    events: list[str] = []
    original_read_text = Path.read_text

    def counted_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == metadata_path:
            events.append("read")
        return original_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    def record_sleep(seconds: float) -> None:
        events.append(f"sleep:{seconds:g}")

    def fake_display_command(
        session_dir: Path,
        command: list[str],
        *,
        runtime_dir: Path,
        display: str,
        timeout: float,
        env: dict[str, str] | None = None,
        cancelled: object = None,
    ) -> int:
        assert session_dir == tmp_path
        assert runtime_dir == tmp_path
        assert display == "wayland-1"
        events.append("grim")
        return 0

    monkeypatch.setattr(Path, "read_text", counted_read_text)
    monkeypatch.setattr(time, "sleep", record_sleep)
    monkeypatch.setattr("framewisp.lib.display_command", fake_display_command)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "framewisp",
            str(tmp_path),
            "screenshot",
            "--delay",
            "2",
            "--region",
            "10",
            "20",
            "100",
            "50",
            "--json",
            str(tmp_path / "shot.png"),
        ],
    )
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 0
    assert events == ["sleep:2", "read", "grim"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["width"] == 100
    assert payload["height"] == 50
    assert payload["display_width"] == 1280
    assert payload["display_height"] == 720
