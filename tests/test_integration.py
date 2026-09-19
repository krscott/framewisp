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
        ["framewisp", "Alice"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Hello, Alice!" in result.stdout


@pytest.mark.integration
def test_cli_default_name() -> None:
    """Test CLI with no arguments uses default name."""
    result = subprocess.run(
        ["framewisp"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Hello, World!" in result.stdout


@pytest.mark.integration
def test_cli_verbose_flag() -> None:
    """Test CLI with --verbose flag shows debug output."""
    result = subprocess.run(
        ["framewisp", "--verbose", "Bob"],
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
        ["framewisp", "-v", "Charlie"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Hello, Charlie!" in result.stdout
    assert "Greeting user..." in result.stderr


@pytest.mark.integration
def test_cli_verbose_env_var() -> None:
    """Test CLI with FRAMEWISP_VERBOSE environment variable."""
    env = os.environ.copy()
    env["FRAMEWISP_VERBOSE"] = "1"
    result = subprocess.run(
        ["framewisp", "David"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    assert "Hello, David!" in result.stdout
    assert "Greeting user..." in result.stderr


@pytest.mark.integration
def test_cli_verbose_env_var_false() -> None:
    """Test that FRAMEWISP_VERBOSE=0 does not enable debug output."""
    env = os.environ.copy()
    env["FRAMEWISP_VERBOSE"] = "0"
    result = subprocess.run(
        ["framewisp", "David"],
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
    env.pop("FRAMEWISP_VERBOSE", None)
    result = subprocess.run(
        ["framewisp", "--verbose", "Eve"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    assert "Hello, Eve!" in result.stdout
    assert "Greeting user..." in result.stderr
