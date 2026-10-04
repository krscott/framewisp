"""Opt-in checks for the versions and layouts in docs/ui-inspection.md."""

import json
import os
import socket
import struct
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_apps,
    pytest.mark.skipif(
        os.environ.get("FRAMEWISP_REAL_APPS") != "1",
        reason="Set FRAMEWISP_REAL_APPS=1 with the documented Flatpaks installed",
    ),
]


@dataclass(frozen=True)
class RealApp:
    directory: Path
    session: Path
    runtime: Path

    def cli(self, *arguments: str, returncode: int = 0) -> str:
        result = subprocess.run(
            ["framewisp", str(self.session), *arguments],
            capture_output=True,
            text=True,
            timeout=30,
        )
        with (self.directory / "commands.jsonl").open("a") as log:
            log.write(
                json.dumps(
                    {
                        "arguments": arguments,
                        "returncode": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    }
                )
                + "\n"
            )
        assert result.returncode == returncode, result.stdout + result.stderr
        return result.stdout

    def capture(self, name: str) -> bool:
        path = self.directory / f"{name}.png"
        self.cli("screenshot", "--delay", "0.5", str(path))
        with Image.open(path) as image:
            assert image.size == (1280, 720)
            low, high = cast(tuple[int, int], image.convert("L").getextrema())
            return high - low > 32

    def batch(
        self, name: str, actions: list[dict[str, Any]], *, success: bool
    ) -> dict[str, Any]:
        path = self.directory / f"{name}.json"
        path.write_text(json.dumps({"actions": actions}))
        result: dict[str, Any] = json.loads(
            self.cli("batch", "--file", str(path), returncode=0 if success else 1)
        )
        (self.directory / f"{name}-result.json").write_text(
            json.dumps(result, indent=2)
        )
        return result


def wait_until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 20
    while not predicate():
        assert (
            time.monotonic() < deadline
        ), "Application did not reach the expected state"
        time.sleep(0.1)


def sway_tree(runtime: Path) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(next(runtime.glob("sway-ipc.*.sock"))))
        connection.sendall(b"i3-ipc" + struct.pack("=II", 0, 4))

        def receive(length: int) -> bytes:
            data = bytearray()
            while len(data) < length:
                chunk = connection.recv(length - len(data))
                assert chunk, "Sway closed the tree connection"
                data.extend(chunk)
            return bytes(data)

        header = receive(14)
        length, kind = struct.unpack("=II", header[6:])
        assert header[:6] == b"i3-ipc" and kind == 4 and length <= 1024 * 1024
        tree: dict[str, Any] = json.loads(receive(length))
        return tree


def windows(tree: dict[str, Any]) -> list[dict[str, Any]]:
    found = [tree] if tree.get("shell") in {"xdg_shell", "xwayland"} else []
    for child in tree.get("nodes", []) + tree.get("floating_nodes", []):
        found.extend(windows(child))
    return found


def descendants(pid: int) -> set[int]:
    found: set[int] = set()
    for path in Path(f"/proc/{pid}/task").glob("*/children"):
        try:
            children = {int(child) for child in path.read_text().split()}
        except OSError:
            continue  # Processes can exit while collecting cleanup evidence.
        for child in children - found:
            found.add(child)
            found.update(descendants(child))
    return found


@pytest.fixture
def real_app(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[RealApp]:
    app_name, backend = request.param
    writer = app_name == "writer"
    app_id = "org.libreoffice.LibreOffice" if writer else "org.blender.Blender"
    info = subprocess.run(
        ["flatpak", "info", app_id], capture_output=True, text=True, check=True
    )
    (tmp_path / "flatpak-info.txt").write_text(info.stdout)
    session = tmp_path / "session"
    target = [
        "flatpak",
        "run",
        f"--filesystem={tmp_path}",
        "--socket=wayland" if backend == "wayland" else "--socket=x11",
        "--nosocket=x11" if backend == "wayland" else "--nosocket=wayland",
    ]
    if writer:
        target.extend(
            [
                "--env=SAL_USE_VCLPLUGIN=gtk3",
                app_id,
                f"-env:UserInstallation={tmp_path.as_uri()}/profile",
                "--writer",
                "--norestore",
                "--nofirststartwizard",
            ]
        )
    else:
        target.extend(
            [
                "--env=LIBGL_ALWAYS_SOFTWARE=1",
                f"--env=BLENDER_USER_CONFIG={tmp_path}/profile",
                app_id,
                "--factory-startup",
            ]
        )
    command = ["framewisp", str(session), "run"]
    if backend == "x11":
        command.append("--x11")
    command.extend(["--", *target])
    (tmp_path / "launch.json").write_text(json.dumps(command, indent=2))
    runner_log = tmp_path / "runner.log"
    with runner_log.open("w") as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
    state: dict[str, Any] = {}
    app: RealApp | None = None
    pids: set[int] = set()
    try:

        def ready() -> bool:
            assert process.poll() is None, runner_log.read_text()
            return "Session ready:" in runner_log.read_text()

        wait_until(ready)
        state = json.loads((session / "session.json").read_text())
        app = RealApp(tmp_path, session, Path(state["runtime_directory"]))
        pids.update(state["processes"].values())

        def visible() -> bool:
            tree = sway_tree(app.runtime)
            (tmp_path / "sway-tree.json").write_text(json.dumps(tree, indent=2))
            exposed = windows(tree)
            if not exposed:
                return False
            if writer and not any("Untitled 1" in window["name"] for window in exposed):
                return False
            assert all(
                window["shell"] == ("xdg_shell" if backend == "wayland" else "xwayland")
                for window in exposed
            )
            pids.update(window["pid"] for window in exposed)
            return True

        wait_until(visible)
        wait_until(lambda: app.capture("startup"))
        time.sleep(1)
        yield app
    finally:
        pids.update(descendants(process.pid))
        try:
            if process.poll() is None:
                if app is not None:
                    app.cli("stop")
                else:
                    process.terminate()
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                raise
        if app is not None:
            assert not app.runtime.exists()
            assert not (session / "session.json").exists()
            for pid in pids:
                wait_until(lambda: not Path(f"/proc/{pid}").exists())
            if state["x11_display"] is not None:
                assert not (
                    Path("/tmp/.X11-unix") / f"X{state['x11_display'][1:]}"
                ).exists()
            (tmp_path / "cleanup.json").write_text(
                json.dumps({"pids": sorted(pids), "runtime_removed": True})
            )


@pytest.mark.parametrize(
    "real_app",
    [("writer", "wayland"), ("writer", "x11")],
    ids=["wayland", "x11"],
    indirect=True,
)
def test_writer_flatpak(real_app: RealApp) -> None:
    assert real_app.capture("startup")
    # LibreOffice 26.8's first-run welcome has three pages, despite the CLI flag.
    for _ in range(3):
        real_app.cli("click", "944", "521")
        time.sleep(0.3)
    real_app.cli("click", "500", "250")
    time.sleep(0.3)
    real_app.cli("type", "--interval", "0", "Framewisp compatibility café")
    assert real_app.capture("typed")
    default = json.loads(
        real_app.cli("inspect", "--json", "--role", "paragraph", returncode=1)
    )
    (real_app.directory / "default-inspection.json").write_text(
        json.dumps(default, indent=2)
    )
    assert default["status"] == "partial"
    assert "max-nodes" in default["reasons"] or "max-depth" in default["reasons"]
    # Input completion does not imply the app consumed the shortcut.
    real_app.cli("key", "Ctrl+a")
    time.sleep(0.3)
    result = real_app.batch(
        "edit",
        [
            {"action": "type", "text": "Verified Writer café", "interval": 0},
            {
                "action": "wait",
                "timeout": 10,
                "observation": {"max_nodes": 4096, "max_depth": 32, "timeout": 10},
                "condition": {
                    "role": "paragraph",
                    "field": "text",
                    "equals": "Verified Writer café",
                },
            },
        ],
        success=True,
    )
    assert result["verified"] is True
    assert real_app.capture("verified")


@pytest.mark.parametrize(
    "real_app",
    [("blender", "wayland"), ("blender", "x11")],
    ids=["wayland", "x11"],
    indirect=True,
)
def test_blender_canvas(real_app: RealApp) -> None:
    assert real_app.capture("startup")
    real_app.cli("click", "300", "300")  # Dismiss the splash.
    time.sleep(0.3)
    for key in ["g", "x", "2", "Return"]:
        real_app.cli("key", key)
        time.sleep(0.2)
    real_app.cli("scroll", "500", "300", "up", "--steps", "2")
    assert real_app.capture("translated")
    # Function keys are unsupported; choose Python Console through the editor menu.
    real_app.cli("click", "18", "38")
    assert real_app.capture("editor-menu")
    real_app.cli("move", "490", "112")
    time.sleep(0.3)
    real_app.cli("click", "490", "112")
    time.sleep(0.3)
    output = real_app.directory / "cube.txt"
    real_app.cli(
        "type",
        "--interval",
        "0.02",
        f"from pathlib import Path; Path({str(output)!r}).write_text(str(tuple(bpy.data.objects['Cube'].location)))",
    )
    time.sleep(0.3)
    real_app.cli("key", "Return")
    assert real_app.capture("console")
    wait_until(output.exists)
    assert output.read_text() == "(2.0, 0.0, 0.0)"
    assert real_app.capture("console")
    result = real_app.batch(
        "unsupported-check",
        [
            {
                "action": "assert",
                "timeout": 1,
                "condition": {
                    "role": "label",
                    "name": "Cube",
                    "field": "name",
                    "equals": "Cube",
                },
            },
        ],
        success=False,
    )
    assert result["verified"] is False
    assert result["failed_phase"] == "check"
    assert result["results"][0]["observation"]["status"] == "unsupported"
