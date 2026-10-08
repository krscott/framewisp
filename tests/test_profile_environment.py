import subprocess
import sys
from pathlib import Path

import pytest

from framewisp.errors import SessionError
from framewisp.profiles import fresh_environment


def test_missing_profile_fonts(tmp_path: Path) -> None:
    with pytest.raises(SessionError, match="FRAMEWISP_PROFILE_FONTCONFIG_FILE"):
        fresh_environment(tmp_path, {})


def test_profile_preserves_pixbuf_runtime_paths(tmp_path: Path) -> None:
    fonts = tmp_path / "fonts.conf"
    fonts.touch()
    env = fresh_environment(
        tmp_path,
        {
            "GDK_PIXBUF_MODULE_FILE": "/caller/loaders.cache",
            "GDK_PIXBUF_MODULEDIR": "/caller/loaders",
            "GDK_SCALE": "3",
        },
        fonts=str(fonts),
    )
    assert env["GDK_PIXBUF_MODULE_FILE"] == "/caller/loaders.cache"
    assert env["GDK_PIXBUF_MODULEDIR"] == "/caller/loaders"
    assert env["GDK_SCALE"] == "1"


def test_service_directories_require_profile(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path / "session"),
            "run",
            "--dbus-service-dir",
            str(tmp_path),
            "--",
            "unused-app",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "--dbus-service-dir requires --profile fresh" in result.stderr
    assert not (tmp_path / "session").exists()
