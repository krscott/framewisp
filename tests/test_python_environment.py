"""Installed entry points must ignore Python settings needed by target apps."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("FRAMEWISP_TEST_ISOLATED_PYTHON") != "1",
        reason="Requires the installed Nix entry points (nix flake check)",
    ),
]


@pytest.fixture
def foreign_python(tmp_path: Path) -> Path:
    directory = tmp_path / "foreign Python environment"
    (directory / "gi").mkdir(parents=True)
    (directory / "gi" / "__init__.py").write_text(
        "raise RuntimeError('Caller PyGObject loaded')\n"
    )
    (directory / "sitecustomize.py").write_text(
        "raise SystemExit('Caller Python startup customization loaded')\n"
    )
    (directory / "caller_only.py").write_text("VALUE = 'caller module'\n")
    return directory


@pytest.mark.parametrize("x11", [False, True])
def test_packaged_demo_ignores_caller_python_settings(
    tmp_path: Path, foreign_python: Path, x11: bool
) -> None:
    session = tmp_path / "session"
    runner_log = tmp_path / "runner.log"
    env = os.environ | {
        "PYTHONPATH": str(foreign_python),
        "PYTHONHOME": str(tmp_path / "missing Python home"),
    }
    command = ["framewisp", str(session), "run"]
    if x11:
        command.append("--x11")
    command.extend(["--", "framewisp-demo"])
    with runner_log.open("w") as output:
        process = subprocess.Popen(
            command, env=env, stdout=output, stderr=subprocess.STDOUT
        )
    try:
        deadline = time.monotonic() + 20
        while "Session ready:" not in runner_log.read_text():
            assert process.poll() is None, runner_log.read_text()
            assert time.monotonic() < deadline, runner_log.read_text()
            time.sleep(0.05)
        # Wait for GTK initialization, since runner readiness precedes rendering.
        backend = "X11Display" if x11 else "WaylandDisplay"
        while "Demo ready" not in (session / "app.log").read_text():
            assert process.poll() is None, (session / "app.log").read_text()
            assert time.monotonic() < deadline, (session / "app.log").read_text()
            time.sleep(0.05)
        assert backend in (session / "app.log").read_text()
        result = subprocess.run(
            ["framewisp", str(session), "stop"],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert process.wait(timeout=20) == 0, runner_log.read_text()
        assert not (session / "session.json").exists()
        assert not (session / ".headless-owner.json").exists()
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=20)


@pytest.mark.parametrize("pythonpath", ["unset", "empty", "foreign"])
@pytest.mark.parametrize("fresh", [False, True])
def test_target_app_inherits_caller_pythonpath(
    tmp_path: Path, foreign_python: Path, pythonpath: str, fresh: bool
) -> None:
    session = tmp_path / "session"
    env = os.environ.copy()
    value = {"unset": None, "empty": "", "foreign": str(foreign_python)}[pythonpath]
    if value is None:
        env.pop("PYTHONPATH", None)
    else:
        env["PYTHONPATH"] = value
    env["GI_TYPELIB_PATH"] = str(foreign_python)
    script = (
        "import json, os; "
        "print(json.dumps({key: os.environ.get(key) "
        "for key in ('PYTHONPATH', 'GI_TYPELIB_PATH')}), flush=True)"
    )
    if pythonpath == "foreign":
        script += "; import caller_only; print(caller_only.VALUE, flush=True)"
    command = ["framewisp", str(session), "run"]
    if fresh:
        command.extend(["--profile", "fresh"])
    # Skip sitecustomize in the probe while retaining PYTHONPATH imports.
    command.extend(["--", sys.executable, "-S", "-c", script])
    result = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Session ready:" in result.stdout
    lines = (session / "app.log").read_text().splitlines()
    observed = json.loads(lines[0])
    assert observed["PYTHONPATH"] == value
    assert observed["GI_TYPELIB_PATH"] == str(foreign_python)
    if pythonpath == "foreign":
        assert lines[1] == "caller module"
    assert not (session / "session.json").exists()
    assert not (session / ".headless-owner.json").exists()


@pytest.mark.parametrize("value", [None, "", "/caller/sentinel"])
@pytest.mark.parametrize("fresh", [False, True])
def test_target_app_preserves_wrapper_variables(
    tmp_path: Path, value: str | None, fresh: bool
) -> None:
    framewisp = shutil.which("framewisp")
    assert framewisp is not None
    keys = (
        "PATH",
        "GI_TYPELIB_PATH",
        "XDG_DATA_DIRS",
        "GST_PLUGIN_SYSTEM_PATH_1_0",
        "GIO_EXTRA_MODULES",
        "GDK_PIXBUF_MODULE_FILE",
        "GDK_PIXBUF_MODULEDIR",
        "PYTHONNOUSERSITE",
    )
    env = os.environ.copy()
    for key in keys:
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    session = tmp_path / "session"
    command = [framewisp, str(session), "run"]
    if fresh:
        command.extend(["--profile", "fresh"])
    command.extend(
        [
            "--",
            sys.executable,
            "-S",
            "-c",
            f"import json, os; print(json.dumps({{k: os.environ.get(k) for k in {(*keys, 'FRAMEWISP_CALLER_ENV_FD')!r}}}))",
        ]
    )
    result = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads((session / "app.log").read_text())
    assert observed.pop("FRAMEWISP_CALLER_ENV_FD") is None
    assert observed == dict.fromkeys(keys, value)


def test_target_app_resolves_caller_venv_python(tmp_path: Path) -> None:
    framewisp = shutil.which("framewisp")
    assert framewisp is not None
    venv = tmp_path / "caller venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        check=True,
        capture_output=True,
        timeout=20,
    )
    (tmp_path / "myapp.py").write_text("import sys; print(sys.prefix)\n")
    session = tmp_path / "session"
    result = subprocess.run(
        [framewisp, str(session), "run", "--", "python", "-m", "myapp"],
        env=os.environ | {"PATH": str(venv / "bin")},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (session / "app.log").read_text().strip() == str(venv)


def test_target_app_receives_minimal_caller_environment(tmp_path: Path) -> None:
    framewisp = shutil.which("framewisp")
    assert framewisp is not None
    session = tmp_path / "session"
    script = (
        "import json, os; from pathlib import Path; "
        "entries = Path('/proc/self/environ').read_bytes().split(b'\\0'); "
        "print(json.dumps({os.fsdecode(k): os.fsdecode(v) "
        "for entry in entries if entry for k, v in [entry.split(b'=', 1)]}))"
    )
    result = subprocess.run(
        [framewisp, str(session), "run", "--", sys.executable, "-I", "-c", script],
        env={},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads((session / "app.log").read_text())
    for key in ("PATH", "LC_CTYPE", "LANG", "GI_TYPELIB_PATH", "XDG_DATA_DIRS"):
        assert key not in observed
    assert not any(key.startswith("FRAMEWISP_") for key in observed)
    assert observed["GDK_BACKEND"] == "wayland"
