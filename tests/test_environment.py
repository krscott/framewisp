"""Exercise the launcher boundary without depending on a particular Nix wrapper."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from framewisp.environment import caller_environment
from framewisp.lib import app_command, session_environment_for_run


def test_unwrapped_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALLER_ONLY", "editable install")
    env = caller_environment()
    assert env["CALLER_ONLY"] == "editable install"
    env["CALLER_ONLY"] = "changed copy"
    assert os.environ["CALLER_ONLY"] == "editable install"


@pytest.mark.parametrize("minimal", [False, True])
def test_launcher_captures_all_variables(tmp_path: Path, minimal: bool) -> None:
    source = Path(__file__).parents[1]
    probe = tmp_path / "wrapper.py"
    probe.write_text(
        "import json, os, sys\n"
        "fd = int(os.environ['FRAMEWISP_CALLER_ENV_FD'])\n"
        "os.environ.update(PATH='/wrapper/bin', GI_TYPELIB_PATH='/wrapper/gi', "
        "UNSET='wrapper default', EMPTY='wrapper override', "
        "FUTURE_WRAPPER_VARIABLE='new dependency')\n"
        f"sys.path.insert(0, {str(source)!r})\n"
        "from framewisp.environment import caller_environment, initialize_environment\n"
        "initialize_environment()\n"
        "print(json.dumps(caller_environment()))\n"
        "assert 'FRAMEWISP_CALLER_ENV_FD' not in os.environ\n"
        "try:\n"
        "    os.fstat(fd)\n"
        "except OSError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('Snapshot descriptor still open')\n"
    )
    env = os.environ | {
        "EMPTY": "",
        "GI_TYPELIB_PATH": "/caller/gi",
        "SPECIAL": "quotes ' \" =\ncolon: and Unicode 日本語",
        "NON_UTF8": os.fsdecode(b"\xff"),
        # Larger than Linux's per-string exec limit if encoded as one variable.
        "LARGE_A": "a" * 70000,
        "LARGE_B": "b" * 70000,
    }
    for key in ("UNSET", "FUTURE_WRAPPER_VARIABLE", "FRAMEWISP_CALLER_ENV_FD"):
        env.pop(key, None)
    if minimal:
        env = {"PATH": "/caller/bin"}
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(source / "framewisp" / "_launch.py"),
            sys.executable,
            "-I",
            str(probe),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == env


def test_session_environment_keeps_source_separate(tmp_path: Path) -> None:
    source = {
        "PATH": "/caller/bin",
        "GI_TYPELIB_PATH": "",
        "DISPLAY": ":123",
        "DBUS_SESSION_BUS_ADDRESS": "host-bus",
        "AT_SPI_BUS_ADDRESS": "host-accessibility-bus",
    }
    env = session_environment_for_run(tmp_path, source)
    assert env["PATH"] == "/caller/bin"
    assert env["GI_TYPELIB_PATH"] == ""
    assert env["XDG_RUNTIME_DIR"] == str(tmp_path)
    assert env["GDK_BACKEND"] == "wayland"
    assert "DISPLAY" not in env
    assert "DBUS_SESSION_BUS_ADDRESS" not in env
    assert "AT_SPI_BUS_ADDRESS" not in env
    assert source["DISPLAY"] == ":123"
    assert "XDG_RUNTIME_DIR" not in source


def test_bundled_demo_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    caller = tmp_path / "caller"
    runtime = tmp_path / "runtime"
    for directory in (caller, runtime):
        directory.mkdir()
        demo = directory / "framewisp-demo"
        demo.touch()
        demo.chmod(0o755)
    monkeypatch.setenv("PATH", str(runtime))
    command = ["framewisp-demo", "argument"]
    env = {"PATH": str(caller)}
    assert app_command(command, env) == command
    assert env == {"PATH": str(caller)}
    assert app_command(command, {"PATH": ""}) == [
        str(runtime / "framewisp-demo"),
        "argument",
    ]
    assert app_command(["python"], {"PATH": ""}) == ["python"]
    absolute = [str(caller / "framewisp-demo")]
    assert app_command(absolute, {"PATH": ""}) == absolute
