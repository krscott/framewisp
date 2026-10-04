import subprocess
import sys
from pathlib import Path

import pytest

from framewisp.errors import SessionError
from framewisp.profiles import fresh_environment


def test_missing_profile_fonts(tmp_path: Path) -> None:
    with pytest.raises(SessionError, match="FRAMEWISP_PROFILE_FONTCONFIG_FILE"):
        fresh_environment(tmp_path, {})


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
