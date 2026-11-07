"""Exercise the launcher boundary without depending on a particular Nix wrapper."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from framewisp.environment import caller_environment, sanitized_environment
from framewisp.lib import app_command, session_environment_for_run


def test_unwrapped_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALLER_ONLY", "editable install")
    env = caller_environment()
    assert env["CALLER_ONLY"] == "editable install"
    env["CALLER_ONLY"] = "changed copy"
    assert os.environ["CALLER_ONLY"] == "editable install"


# R-ENV-1
@pytest.mark.parametrize("minimal", [False, True])
def test_launcher_captures_all_variables(tmp_path: Path, minimal: bool) -> None:
    source = Path(__file__).parents[1]
    python = str(Path("/proc/self/exe").resolve())
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
            python,
            "-I",
            str(source / "framewisp" / "_launch.py"),
            python,
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


@pytest.mark.parametrize("value", [None, "0", "1", ""])
def test_session_accessibility_default(tmp_path: Path, value: str | None) -> None:
    source = {} if value is None else {"ACCESSIBILITY_ENABLED": value}
    env = session_environment_for_run(tmp_path, source)
    assert env["ACCESSIBILITY_ENABLED"] == ("1" if value is None else value)
    assert source == ({} if value is None else {"ACCESSIBILITY_ENABLED": value})


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
    monkeypatch.chdir(caller)
    assert app_command(command, {"PATH": ""}) == command


def test_sanitizing_keeps_lookup_paths_and_app_settings(tmp_path: Path) -> None:
    kept = {
        "PATH": "/caller/bin",
        "PYTHONPATH": "/caller/python",
        "XDG_DATA_DIRS": "/caller/share",
        "GDK_PIXBUF_MODULE_FILE": "/caller/loaders.cache",
        "GDK_PIXBUF_MODULEDIR": "/caller/loaders",
        "QT_PLUGIN_PATH": "/caller/plugins",
        "QML_IMPORT_PATH": "/caller/qml",
        "MY_APP_SETTING": "value",
    }
    env = sanitized_environment(tmp_path, kept | {"GDK_SCALE": "3"}, fonts="fonts.conf")
    assert kept.items() <= env.items()
    assert env["GDK_SCALE"] == "1"


# R-ENV-1, R-ENV-4
@pytest.mark.parametrize(
    "name",
    [
        "XDG_CURRENT_DESKTOP",
        "XDG_SESSION_DESKTOP",
        "DESKTOP_SESSION",
        "GNOME_DESKTOP_SESSION_ID",
        "KDE_FULL_SESSION",
        "KDE_SESSION_VERSION",
    ],
)
def test_sanitizing_removes_desktop_identity(tmp_path: Path, name: str) -> None:
    assert name not in sanitized_environment(tmp_path, {name: "x"}, fonts="fonts.conf")


# R-ENV-1, R-ENV-4
def test_sanitizing_fixes_time_zone(tmp_path: Path) -> None:
    env = sanitized_environment(tmp_path, {"TZ": "Asia/Tokyo"}, fonts="fonts.conf")
    assert env["TZ"] == "UTC"


# R-ENV-7
@pytest.mark.parametrize("value", [None, "", "missing.conf"])
def test_run_refuses_a_missing_font_file(tmp_path: Path, value: str | None) -> None:
    env = os.environ.copy()
    env.pop("FRAMEWISP_FONTCONFIG_FILE", None)
    if value is not None:
        env["FRAMEWISP_FONTCONFIG_FILE"] = value and str(tmp_path / value)
    session = tmp_path / "session"
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", str(session), "run", "--", "unused-app"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "FRAMEWISP_FONTCONFIG_FILE" in result.stderr
    assert "set it to" in result.stderr
    assert not session.exists()
