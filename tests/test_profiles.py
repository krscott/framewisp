import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageChops
from test_integration import cli, wait_until


@pytest.fixture
def conflicting_home(tmp_path: Path) -> Path:
    host = tmp_path / "host"
    config = host / ".config"
    for version in ("3.0", "4.0"):
        directory = config / f"gtk-{version}"
        directory.mkdir(parents=True)
        (directory / "settings.ini").write_text(
            "[Settings]\ngtk-font-name=monospace 30\ngtk-xft-dpi=196608\n"
            "gtk-theme-name=NonexistentHostTheme\n"
        )
    (config / "glib-2.0" / "settings").mkdir(parents=True)
    (config / "glib-2.0" / "settings" / "keyfile").write_text(
        "[org/gtk/Demo4/Application]\ncolor='green'\n"
    )
    return host


@pytest.mark.integration
@pytest.mark.parametrize("x11", [False, True], ids=["wayland", "x11"])
@pytest.mark.parametrize("demo", [False, True], ids=["native-gtk", "bundled-demo"])
def test_fresh_real_app(
    tmp_path: Path, conflicting_home: Path, x11: bool, demo: bool
) -> None:
    app = os.environ.get("FRAMEWISP_TEST_GTK_APP")
    assert app and Path(app).is_file(), "Run inside nix develop for GTK's demo app"
    host = conflicting_home
    config = host / ".config"
    original = {
        path.relative_to(host): path.read_bytes()
        for path in host.rglob("*")
        if path.is_file()
    }
    services = tmp_path / "services & apps"
    services.mkdir()
    (services / "ca.desrt.dconf.service").write_text(
        Path(os.environ["FRAMEWISP_TEST_DCONF_SERVICE"]).read_text()
    )
    captures: list[Image.Image] = []
    homes: list[Path] = []
    text_sizes: list[list[int]] = []
    for number in range(2):
        directory = tmp_path / f"session-{number}"
        report = tmp_path / f"report-{number}.json"
        command = [
            "framewisp",
            str(directory),
            "run",
            "--profile",
            "fresh",
            "--dbus-service-dir",
            str(services),
        ]
        if x11:
            command.append("--x11")
        command.extend(
            ["--", sys.executable, str(Path(__file__).with_name("profile_probe.py"))]
        )
        if demo:
            command.append("--demo")
        env = os.environ | {
            "HOME": str(host),
            "XDG_CONFIG_HOME": str(config),
            "XDG_CACHE_HOME": str(host / "cache"),
            "XDG_DATA_HOME": str(host / "data"),
            "GTK_THEME": "NonexistentHostTheme",
            "GDK_SCALE": str(number + 2),
            "GDK_DPI_SCALE": "2",
            "QT_SCALE_FACTOR": "3",
            "QT_FONT_DPI": "192",
            "LC_ALL": "C",
            "LANGUAGE": "de",
            "FONTCONFIG_FILE": "/dev/null",
            "FONTCONFIG_SYSROOT": str(host / "nonexistent-sysroot"),
            "FC_LANG": "ja",
            "FRAMEWISP_TEST_REPORT": str(report),
        }
        log = tmp_path / f"runner-{number}.log"
        with log.open("w") as output:
            process = subprocess.Popen(
                command, env=env, stdout=output, stderr=subprocess.STDOUT
            )
        state: dict[str, Any] = {}
        try:

            def ready() -> bool:
                assert process.poll() is None, (
                    log.read_text()
                    + "\n"
                    + "\n".join(path.read_text() for path in directory.glob("*.log"))
                )
                return (
                    report.exists()
                    and "Session ready:" in log.read_text()
                    and (
                        not demo or "Demo ready" in (directory / "app.log").read_text()
                    )
                )

            wait_until(ready)
            state = json.loads((directory / "session.json").read_text())
            observed = json.loads(report.read_text())
            assert observed["color"] == "red"
            assert observed["font"] == "DejaVu Sans 11"
            assert observed["font_family"] == "DejaVu Sans"
            assert observed["locale"] == "C.UTF-8"
            text_sizes.append(observed["text_size"])
            assert observed["dpi"] == 98304
            assert observed["theme"] == "Adwaita"
            assert "ca.desrt.dconf" in observed["bus_names"]
            assert "org.freedesktop.portal.Desktop" not in observed["bus_names"]
            homes.append(Path(observed["home"]))
            assert homes[-1].is_relative_to(Path(state["runtime_directory"]))
            assert state["app_profile"] == "fresh"
            time.sleep(1)
            if demo:
                # Move focus off the entry so cursor blinking cannot change pixels.
                cli(directory, "key", "Tab")
            # Move the cursor away from app widgets to avoid hover state.
            cli(directory, "move", "1279", "719")
            capture = tmp_path / f"capture-{number}.png"
            cli(directory, "screenshot", "--delay", "1", str(capture))
            with Image.open(capture) as image:
                assert image.size == (1280, 720)
                captures.append(image.convert("RGB"))
            assert ImageChops.difference(
                captures[-1], Image.new("RGB", (1280, 720))
            ).getbbox()
            private_keyfile = (
                homes[-1].parent / "config" / "glib-2.0" / "settings" / "keyfile"
            )
            assert "blue" in private_keyfile.read_text()
            assert (homes[-1].parent / "config" / "dconf" / "user").is_file()
            service_env = (
                Path(f"/proc/{observed['service_pid']}/environ")
                .read_bytes()
                .split(b"\0")
            )
            assert f"HOME={homes[-1]}".encode() in service_env
            display_key = "DISPLAY" if x11 else "WAYLAND_DISPLAY"
            display_value = state["x11_display"] if x11 else state["wayland_display"]
            assert f"{display_key}={display_value}".encode() in service_env
            # Neither bus includes default desktop activation directories.
            runtime = Path(state["runtime_directory"])
            assert "servicedir" not in (runtime / "bus.conf").read_text()
            assert (
                str(services).replace("&", "&amp;")
                in (runtime / "app-bus.conf").read_text()
            )
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=30)
        assert process.returncode == 0, log.read_text()
        assert not homes[-1].exists()
        assert not Path(f"/proc/{observed['service_pid']}").exists()
        assert not Path(f"/proc/{observed['gui_pid']}").exists()
        assert all(
            not Path(f"/proc/{pid}").exists() for pid in state["processes"].values()
        )
    assert homes[0] != homes[1]
    assert text_sizes[0] == text_sizes[1]
    # GTK's title-bar SVG icons can vary by a few antialiased pixels on X11.
    # Compare content pixels; keep full captures and display geometry checks.
    region = (0, 50, 1280, 720) if not demo else (0, 0, 1280, 720)
    assert (
        ImageChops.difference(*(image.crop(region) for image in captures)).getbbox()
        is None
    )
    assert {
        path.relative_to(host): path.read_bytes()
        for path in host.rglob("*")
        if path.is_file()
    } == original
