"""What a target app can see of its caller, measured through real sessions."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast

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


# R-ENV-4, R-ENV-6
@pytest.mark.integration
@pytest.mark.parametrize("x11", [False, True], ids=["wayland", "x11"])
@pytest.mark.parametrize("demo", [False, True], ids=["native-gtk", "bundled-demo"])
def test_gtk_app_ignores_conflicting_caller(
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
    services = tmp_path / "services"
    services.mkdir()
    (services / "ca.desrt.dconf.service").write_text(
        Path(os.environ["FRAMEWISP_TEST_DCONF_SERVICE"]).read_text()
    )
    bus_config = tmp_path / "app-bus.conf"
    bus_config.write_text(
        "<busconfig><type>session</type><auth>EXTERNAL</auth>"
        "<listen>unix:tmpdir=/tmp</listen>"
        '<policy context="default"><allow send_destination="*"/>'
        '<allow receive_sender="*"/><allow own="*"/></policy>'
        f"<servicedir>{services}</servicedir></busconfig>"
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
        ]
        if x11:
            command.append("--x11")
        command.extend(
            [
                "--",
                "env",
                "INJECTED_FOR_SERVICE=1",
                "dbus-run-session",
                f"--config-file={bus_config}",
                "--",
                sys.executable,
                str(Path(__file__).with_name("environment_probe.py")),
            ]
        )
        if demo:
            command.append("--demo")
        env = os.environ | {"FRAMEWISP_TEST_REPORT": str(report)}
        # Only the second caller conflicts, so a leak changes its pixels.
        if number == 1:
            env |= {
                "HOME": str(host),
                "XDG_CONFIG_HOME": str(config),
                "XDG_CACHE_HOME": str(host / "cache"),
                "XDG_DATA_HOME": str(host / "data"),
                "GTK_THEME": "NonexistentHostTheme",
                "GDK_SCALE": "3",
                "GDK_DPI_SCALE": "2",
                "QT_SCALE_FACTOR": "3",
                "QT_FONT_DPI": "192",
                "LC_ALL": "C",
                "LANGUAGE": "de",
                "FONTCONFIG_FILE": "/dev/null",
                "FONTCONFIG_SYSROOT": str(host / "nonexistent-sysroot"),
                "FC_LANG": "ja",
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
            assert b"INJECTED_FOR_SERVICE=1" in service_env
            display_key = "DISPLAY" if x11 else "WAYLAND_DISPLAY"
            display_value = state["x11_display"] if x11 else state["wayland_display"]
            assert f"{display_key}={display_value}".encode() in service_env
            # Activated services must retain the caller's dependency lookup paths.
            for key in (
                "PATH",
                "GI_TYPELIB_PATH",
                "XDG_DATA_DIRS",
                "GST_PLUGIN_SYSTEM_PATH_1_0",
                "GIO_EXTRA_MODULES",
                "GDK_PIXBUF_MODULE_FILE",
                "GDK_PIXBUF_MODULEDIR",
                "PYTHONNOUSERSITE",
            ):
                observed_values = [
                    item
                    for item in service_env
                    if item.startswith((key + "=").encode())
                ]
                expected_values = (
                    [os.fsencode(f"{key}={env[key]}")] if key in env else []
                )
                assert observed_values == expected_values, key
            # Neither private bus includes default desktop activation directories.
            runtime = Path(state["runtime_directory"])
            assert "servicedir" not in (runtime / "bus.conf").read_text()
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


def session_env(tmp_path: Path, name: str, caller: dict[str, str], *app: str) -> str:
    """Run a command that exits by itself and return what it printed."""
    session = tmp_path / f"session-{name}"
    result = subprocess.run(
        ["framewisp", str(session), "run", "--", *app],
        env=os.environ | caller,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return (session / "app.log").read_text()


def stable_screenshot(session: Path, path: Path) -> Image.Image:
    """Wait for the window to map and settle before trusting a capture."""
    previous: bytes | None = None

    def settled() -> bool:
        nonlocal previous
        cli(session, "screenshot", str(path))
        current = path.read_bytes()
        with Image.open(path) as image:
            mapped = (
                image.convert("L").getextrema()[0] != image.convert("L").getextrema()[1]
            )
        same = current == previous
        previous = current
        path.unlink()
        return mapped and same

    wait_until(settled)
    cli(session, "move", "1279", "719")
    cli(session, "screenshot", "--delay", "1", str(path))
    with Image.open(path) as image:
        return image.convert("RGB")


def qt_probe_capture(
    tmp_path: Path, name: str, caller: dict[str, str], *, x11: bool = False
) -> Image.Image:
    session = tmp_path / f"session-{name}"
    log = tmp_path / f"runner-{name}.log"
    command = ["framewisp", str(session), "run"]
    if x11:
        command.append("--x11")
    probe = Path(__file__).with_name("environment_probe.qml")
    command.extend(["--", "qml", str(probe)])
    with log.open("w") as output:
        process = subprocess.Popen(
            command,
            env=os.environ | caller,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    try:
        wait_until(lambda: "Session ready:" in log.read_text())
        return stable_screenshot(session, tmp_path / f"{name}.png")
    finally:
        process.terminate()
        process.wait(timeout=30)


def empty_fontconfig(tmp_path: Path) -> dict[str, str]:
    config = tmp_path / "caller-fonts.conf"
    config.write_text("<fontconfig></fontconfig>")
    return {"FONTCONFIG_FILE": str(config), "FC_LANG": "ja"}


LOCALE = {"LANG": "de_DE.UTF-8", "LC_ALL": "de_DE.UTF-8", "LANGUAGE": "de"}
TOOLKIT_THEME = {
    "QT_STYLE_OVERRIDE": "Windows",
    "QT_QUICK_CONTROLS_STYLE": "Material",
    "QT_QPA_PLATFORMTHEME": "gtk3",
}
SCALE = {"QT_SCALE_FACTOR": "2", "QT_FONT_DPI": "192"}


# R-ENV-4
@pytest.mark.integration
@pytest.mark.parametrize(
    "caller",
    [
        LOCALE,
        {"TZ": "Asia/Tokyo"},
        TOOLKIT_THEME,
        SCALE,
        {"XDG_CURRENT_DESKTOP": "GNOME"},
        {"DESKTOP_SESSION": "gnome"},
        {"GNOME_DESKTOP_SESSION_ID": "this-is-deprecated"},
    ],
    ids=[
        "locale",
        "time-zone",
        "toolkit-theme",
        "scale",
        "desktop-current",
        "desktop-session",
        "desktop-gnome-id",
    ],
)
def test_qt_app_ignores_caller_difference(
    tmp_path: Path, caller: dict[str, str]
) -> None:
    base = qt_probe_capture(tmp_path, "base", {"TZ": "America/New_York"})
    other = qt_probe_capture(tmp_path, "other", {"TZ": "America/New_York"} | caller)
    assert ImageChops.difference(base, other).getbbox() is None


# R-ENV-4
@pytest.mark.integration
def test_qt_app_ignores_caller_fonts(tmp_path: Path) -> None:
    base = qt_probe_capture(tmp_path, "base", {})
    other = qt_probe_capture(tmp_path, "other", empty_fontconfig(tmp_path))
    assert ImageChops.difference(base, other).getbbox() is None


# R-ENV-4
@pytest.mark.integration
@pytest.mark.parametrize("x11", [False, True], ids=["wayland", "x11"])
def test_qt_app_ignores_every_caller_difference_at_once(
    tmp_path: Path, conflicting_home: Path, x11: bool
) -> None:
    base = qt_probe_capture(tmp_path, "base", {}, x11=x11)
    other = qt_probe_capture(
        tmp_path,
        "other",
        LOCALE
        | TOOLKIT_THEME
        | SCALE
        | empty_fontconfig(tmp_path)
        | {
            "TZ": "Asia/Tokyo",
            "XDG_CURRENT_DESKTOP": "GNOME",
            "DESKTOP_SESSION": "gnome",
            "HOME": str(conflicting_home),
            "XDG_CONFIG_HOME": str(conflicting_home / ".config"),
        },
        x11=x11,
    )
    assert ImageChops.difference(base, other).getbbox() is None


# R-ENV-4
@pytest.mark.integration
def test_caller_fonts_stay_out_and_generic_families_resolve(tmp_path: Path) -> None:
    host = tmp_path / "host fonts"
    listed = session_env(tmp_path, "list", {}, "fc-list", ":", "file")
    bundled = Path(
        next(line for line in listed.splitlines() if line.startswith("/")).rstrip(": ")
    )
    for directory in (
        "share/fonts",
        "data/fonts",
        "home/.fonts",
        "home/.local/share/fonts",
    ):
        (host / directory).mkdir(parents=True)
        shutil.copy(bundled, host / directory / "host-font.ttf")
    (host / "home" / ".config" / "fontconfig").mkdir(parents=True)
    (host / "home" / ".config" / "fontconfig" / "fonts.conf").write_text(
        f"<fontconfig><dir>{host}</dir></fontconfig>"
    )
    caller = {
        "HOME": str(host / "home"),
        "XDG_DATA_HOME": str(host / "data"),
        "XDG_DATA_DIRS": f"{host / 'share'}:{os.environ.get('XDG_DATA_DIRS', '')}",
    }
    fonts = session_env(tmp_path, "host-fonts", caller, "fc-list", ":", "file")
    assert "host-font.ttf" not in fonts
    assert fonts.count("\n") > 1
    families = {
        generic: session_env(
            tmp_path, generic, caller, "fc-match", "--format=%{family}\n", generic
        )
        for generic in ("sans-serif", "serif", "monospace")
    }
    assert "Serif" not in families["sans-serif"]
    assert "Mono" not in families["sans-serif"]
    assert "Serif" in families["serif"]
    assert "Mono" in families["monospace"]


# R-ENV-5
@pytest.mark.integration
def test_app_command_injects_and_removes_variables(tmp_path: Path) -> None:
    output = session_env(
        tmp_path,
        "injected",
        {"INHERITED": "kept"},
        "env",
        "-u",
        "TZ",
        "-u",
        "INHERITED",
        "GTK_THEME=Injected",
        "HOME=/injected-home",
        "FONTCONFIG_FILE=/injected-fonts",
        "env",
    )
    seen = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    assert seen["GTK_THEME"] == "Injected"
    assert seen["HOME"] == "/injected-home"
    assert seen["FONTCONFIG_FILE"] == "/injected-fonts"
    assert "TZ" not in seen
    assert "INHERITED" not in seen


# R-ENV-6
@pytest.mark.integration
def test_private_directories_hold_no_caller_data_and_are_removed(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    home = tmp_path / "caller home"
    config = home / "caller config"
    config.mkdir(parents=True)
    (home / "caller-file").touch()
    (config / "caller-file").touch()
    # TMPDIR holds the runtime sockets, so it needs a short path.
    temporary_directory = tempfile.TemporaryDirectory(dir="/tmp")
    request.addfinalizer(temporary_directory.cleanup)
    temporary = Path(temporary_directory.name)
    (temporary / "caller-file").touch()
    script = """
import json, os
from pathlib import Path
paths = {}
for name in (
    "HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
    "TMPDIR",
):
    path = Path(os.environ[name])
    assert not any(path.rglob("caller-file")), name
    (path / "app-file").touch()
    paths[name] = str(path)
print(json.dumps(paths))
"""
    output = session_env(
        tmp_path,
        "private",
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config),
            "XDG_CACHE_HOME": str(home / "cache"),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_STATE_HOME": str(home / "state"),
            "TMPDIR": str(temporary),
        },
        sys.executable,
        "-I",
        "-c",
        script,
    )
    paths = json.loads(output.splitlines()[-1] if output.strip() else "{}")
    assert len({*paths.values()}) == 6
    assert not any(Path(path).exists() for path in paths.values())
    assert not any(home.rglob("app-file"))
    assert not any(temporary.rglob("app-file"))


def overlay_frame(tmp_path: Path, name: str, caller: dict[str, str]) -> Image.Image:
    """Record a caption over a console panel and return a frame showing both."""
    session = tmp_path / f"session-{name}"
    clip = tmp_path / f"{name}.mp4"
    frame = tmp_path / f"{name}.png"
    log = tmp_path / f"runner-{name}.log"
    with log.open("w") as output:
        process = subprocess.Popen(
            [
                "framewisp",
                str(session),
                "run",
                "--record",
                str(clip),
                "--console",
                "--",
                sys.executable,
                "-I",
                "-c",
                "import time; print('Console text', flush=True); time.sleep(60)",
            ],
            env=os.environ | caller,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    try:
        wait_until(lambda: "Session ready:" in log.read_text())
        wait_until(lambda: "Console text" in (session / "app.log").read_text())
        # Repeat the key so the caption stays visible until the clip ends.
        for _ in range(6):
            cli(session, "key", "F5")
            time.sleep(0.4)
    finally:
        process.terminate()
        process.wait(timeout=60)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-sseof",
            "-1",
            "-i",
            str(clip),
            "-frames:v",
            "1",
            str(frame),
        ],
        check=True,
        timeout=20,
    )
    with Image.open(frame) as image:
        return image.convert("RGB")


# R-REC-23
@pytest.mark.integration
def test_caller_font_rules_leave_recording_text_unchanged(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = home / ".config" / "fontconfig"
    config.mkdir(parents=True)
    (config / "fonts.conf").write_text(
        '<?xml version="1.0"?><fontconfig><match target="pattern">'
        '<edit name="family" mode="assign" binding="strong">'
        "<string>Noto Serif</string></edit></match></fontconfig>"
    )
    base = overlay_frame(tmp_path, "base", {})
    other = overlay_frame(
        tmp_path,
        "other",
        {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")},
    )
    # Encoding varies faintly between clips; a different typeface does not.
    difference = ImageChops.difference(base, other).convert("L")
    assert cast(tuple[int, int], difference.getextrema())[1] <= 24
    # Both overlays must show bright text for the comparison to mean anything.
    for region in ((0, 0, 1280, 720), (1280, 0, base.width, 720)):
        assert sum(base.crop(region).convert("L").histogram()[200:]) > 20
