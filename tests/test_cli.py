import subprocess
import sys
from pathlib import Path

import pytest


def test_cli_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0
    assert "--session" in result.stdout
    assert "screenshot" in result.stdout


@pytest.mark.parametrize("delay", ["-1", "nan", "inf", "nope"])
def test_invalid_screenshot_delay(delay: str, tmp_path: Path) -> None:
    destination = tmp_path / "capture.png"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            "--session",
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
