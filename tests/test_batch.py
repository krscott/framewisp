import json
import subprocess
import sys
from pathlib import Path

import pytest

from framewisp.batch import MAX_REQUEST_BYTES, MAX_STEPS, Batch
from framewisp.connection import COMMAND_PROTOCOL


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {},
        {"steps": []},
        {"steps": [{"action": "key", "chord": "a"}] * (MAX_STEPS + 1)},
        {"steps": [None]},
        {"steps": [{"action": "stop"}]},
        {"steps": [{"action": "click", "x": True, "y": 1}]},
        {"steps": [{"action": "click", "x": -1, "y": 1}]},
        {"steps": [{"action": "type", "text": "a\nb"}]},
        {"steps": [{"action": "type", "text": "abc", "interval": float("nan")}]},
        {"steps": [{"action": "type", "text": "abc", "interval": 151}]},
        {"steps": [{"action": "type", "text": "a" * 16385, "interval": 0}]},
        {
            "steps": [
                {"action": "scroll", "x": 1, "y": 1, "direction": "up", "steps": 10001}
            ]
        },
        {"steps": [{"action": "key", "chord": "Ctrl+Ctrl+a"}]},
        {"steps": [{"action": "type", "text": "abc", "intevral": 0}]},
        {"steps": [{"action": "key", "chord": "a"}], "repeat": 2},
        {"steps": [{"action": "key", "chord": "a"}], "screenshot": None},
        {"steps": [{"action": "key", "chord": "a"}], "screenshot": {"path": ""}},
        {"steps": [{"action": "key", "chord": "a"}], "screenshot": {"path": "a\0b"}},
        {
            "steps": [{"action": "key", "chord": "a"}],
            "screenshot": {"path": "/tmp/a", "delay": -1},
        },
        {
            "steps": [{"action": "key", "chord": "a"}],
            "screenshot": {"path": "/tmp/a", "delay": True},
        },
        {
            "steps": [{"action": "key", "chord": "a"}],
            "screenshot": {"path": "/tmp/a", "delay": 301},
        },
        {
            "steps": [{"action": "key", "chord": "a"}],
            "screenshot": {"path": "/tmp/a", "delai": 1},
        },
    ],
)
def test_reject_batch(raw: object) -> None:
    with pytest.raises(ValueError):
        Batch.parse(raw)


def test_defaults_and_screenshot_path_resolution(tmp_path: Path) -> None:
    batch = Batch.parse(
        {
            "steps": [
                {"action": "click", "x": 12, "y": 34},
                {"action": "type", "text": "é"},
            ],
            "screenshot": {"path": "image.png"},
        },
        directory=tmp_path,
    )
    assert batch.steps[0].parameters == {
        "x": 12,
        "y": 34,
        "button": "left",
        "count": 1,
        "modifier": [],
        "move_duration": 0.3,
    }
    assert batch.steps[1].parameters["interval"] == 0.08
    assert batch.screenshot is not None
    assert batch.screenshot.path == tmp_path / "image.png"
    assert Batch.parse(batch.parameters()) == batch


@pytest.mark.parametrize(
    "step, message",
    [
        ({}, "exactly one of action or check"),
        ({"action": "key", "check": "wait", "chord": "a"}, "exactly one of"),
        ({"action": "wait", "timeout": 1}, 'wait is a check; write "check": "wait"'),
        (
            {"check": "click", "timeout": 1},
            "check must be one of assert, wait, baseline",
        ),
    ],
)
def test_step_names_an_action_or_a_check(step: object, message: str) -> None:
    with pytest.raises(ValueError, match=rf"^steps\[0\]: .*{message}"):
        Batch.parse({"steps": [step]})


def test_pacing_limit_is_for_whole_batch() -> None:
    with pytest.raises(ValueError, match="300 seconds"):
        Batch.parse({"steps": [{"action": "type", "text": "ab", "interval": 151}] * 2})


# R-INPUT-3
@pytest.mark.parametrize("value", [-1, True, float("nan"), float("inf"), 301])
def test_invalid_move_duration(value: object) -> None:
    with pytest.raises(ValueError, match="move_duration"):
        Batch.parse(
            {"steps": [{"action": "move", "x": 1, "y": 1, "move_duration": value}]}
        )


def test_move_duration_counts_toward_batch_budget() -> None:
    with pytest.raises(ValueError, match="300 seconds"):
        Batch.parse(
            {"steps": [{"action": "move", "x": 1, "y": 1, "move_duration": 151}] * 2}
        )


@pytest.mark.parametrize(
    "content",
    [
        b"{",
        b"x" * (MAX_REQUEST_BYTES + 1),
        b'{"steps":[{"action":"key","chord":"bad"}]}',
    ],
    ids=["malformed-json", "too-large", "invalid-key"],
)
def test_invalid_file_before_session_access(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "batch.json"
    path.write_bytes(content)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path / "missing"),
            "batch",
            "--file",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert "Invalid batch" in result.stderr
    assert "disconnected" not in result.stderr


def test_batch_rejects_attached_session(tmp_path: Path) -> None:
    (tmp_path / "session.json").write_text(
        json.dumps({"kind": "attached", "command_protocol": COMMAND_PROTOCOL})
    )
    path = tmp_path / "batch.json"
    path.write_text(json.dumps({"steps": [{"action": "key", "chord": "a"}]}))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path),
            "batch",
            "--file",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert "headless sessions only" in result.stderr
