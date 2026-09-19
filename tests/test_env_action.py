import argparse
from pathlib import Path

import pytest
from dotenv import load_dotenv

from py_start.__main__ import EnvAction


def test_env_action_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that EnvAction handles environment variables correctly."""
    monkeypatch.setenv("TEST_ENV_VAR", "env_value")

    # Setup a test ArgumentParser
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test-option",
        action=EnvAction,
        env_var="TEST_ENV_VAR",
        required=False,
        help="Test option",
    )

    # Test with environment variable set
    args = parser.parse_args([])
    assert args.test_option == "env_value"

    # Test with command line override
    args = parser.parse_args(["--test-option", "cli_value"])
    assert args.test_option == "cli_value"


def test_env_action_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that EnvAction works with dotenv loaded variables."""
    monkeypatch.delenv("DOTENV_TEST_VAR", raising=False)

    # Create a temporary .env file
    env_file = tmp_path / ".env"
    env_file.write_text("DOTENV_TEST_VAR=dotenv_value")

    # Load the .env file
    load_dotenv(dotenv_path=env_file)

    # Setup parser
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test-option",
        action=EnvAction,
        env_var="DOTENV_TEST_VAR",
        required=False,
        help="Test option",
    )

    # Test that the value from .env is used
    args = parser.parse_args([])
    assert args.test_option == "dotenv_value"

    # Test with command line override
    args = parser.parse_args(["--test-option", "cli_value"])
    assert args.test_option == "cli_value"


def test_env_action_boolean_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that EnvAction works with boolean flags."""
    monkeypatch.delenv("VERBOSE", raising=False)

    # Setup a test ArgumentParser
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--verbose", action=EnvAction, env_var="VERBOSE", nargs=0, help="Verbose mode"
    )

    # Test without flag or env var
    args = parser.parse_args([])
    assert args.verbose is None

    # Test with environment variable set
    monkeypatch.setenv("VERBOSE", "1")
    # Force the parser to create a new instance to pick up the environment change
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--verbose", action=EnvAction, env_var="VERBOSE", nargs=0, help="Verbose mode"
    )
    args = parser.parse_args([])
    assert args.verbose is True

    # Test with flag
    args = parser.parse_args(["--verbose"])
    assert args.verbose is True


def test_env_action_boolean_flag_false_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that common false-like env values disable boolean flags."""
    for value in ["", "0", "false", "no", "off"]:
        monkeypatch.setenv("VERBOSE", value)
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--verbose",
            action=EnvAction,
            env_var="VERBOSE",
            nargs=0,
            help="Verbose mode",
        )

        args = parser.parse_args([])
        if value == "":
            assert args.verbose is None
        else:
            assert args.verbose is False


def test_env_action_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that EnvAction respects default values."""
    monkeypatch.delenv("TEST_DEFAULT_VAR", raising=False)

    # Setup a test ArgumentParser
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test-option",
        action=EnvAction,
        env_var="TEST_DEFAULT_VAR",
        default="default_value",
        help="Test option",
    )

    # Test with default value
    args = parser.parse_args([])
    assert args.test_option == "default_value"

    # Test with environment variable overriding default
    monkeypatch.setenv("TEST_DEFAULT_VAR", "env_value")
    # Force the parser to create a new instance to pick up the environment change
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test-option",
        action=EnvAction,
        env_var="TEST_DEFAULT_VAR",
        default="default_value",
        help="Test option",
    )
    args = parser.parse_args([])
    assert args.test_option == "env_value"

    # Test with command line overriding both
    args = parser.parse_args(["--test-option", "cli_value"])
    assert args.test_option == "cli_value"
