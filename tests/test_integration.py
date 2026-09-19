"""Black-box integration tests for the CLI using subprocess.

These tests invoke the CLI as a real process to verify the end-to-end user experience.
"""

import os
import subprocess

import pytest


@pytest.mark.integration
def test_cli_basic_argument() -> None:
    """Test CLI with a basic name argument."""
    result = subprocess.run(
        ["pystart", "Alice"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Hello, Alice!" in result.stdout


@pytest.mark.integration
def test_cli_default_name() -> None:
    """Test CLI with no arguments uses default name."""
    result = subprocess.run(
        ["pystart"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Hello, World!" in result.stdout


@pytest.mark.integration
def test_cli_verbose_flag() -> None:
    """Test CLI with --verbose flag shows debug output."""
    result = subprocess.run(
        ["pystart", "--verbose", "Bob"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Hello, Bob!" in result.stdout
    assert "Greeting user..." in result.stderr


@pytest.mark.integration
def test_cli_verbose_short_flag() -> None:
    """Test CLI with -v short flag shows debug output."""
    result = subprocess.run(
        ["pystart", "-v", "Charlie"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Hello, Charlie!" in result.stdout
    assert "Greeting user..." in result.stderr


@pytest.mark.integration
def test_cli_verbose_env_var() -> None:
    """Test CLI with PYSTART_VERBOSE environment variable."""
    env = os.environ.copy()
    env["PYSTART_VERBOSE"] = "1"
    result = subprocess.run(
        ["pystart", "David"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    assert "Hello, David!" in result.stdout
    assert "Greeting user..." in result.stderr


@pytest.mark.integration
def test_cli_verbose_env_var_false() -> None:
    """Test that PYSTART_VERBOSE=0 does not enable debug output."""
    env = os.environ.copy()
    env["PYSTART_VERBOSE"] = "0"
    result = subprocess.run(
        ["pystart", "David"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    assert "Hello, David!" in result.stdout
    assert "Greeting user..." not in result.stderr


@pytest.mark.integration
def test_cli_flag_overrides_env_var() -> None:
    """Test that command line flag works even when env var is not set."""
    env = os.environ.copy()
    # Ensure the env var is not set
    env.pop("PYSTART_VERBOSE", None)
    result = subprocess.run(
        ["pystart", "--verbose", "Eve"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    assert "Hello, Eve!" in result.stdout
    assert "Greeting user..." in result.stderr
