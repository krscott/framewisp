import json
import subprocess
import sys
from pathlib import Path

import pytest

from framewisp.batch import MAX_ACTIONS, MAX_REQUEST_BYTES, Batch


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {},
        {"actions": []},
        {"actions": [{"action": "key", "chord": "a"}] * (MAX_ACTIONS + 1)},
        {"actions": [None]},
        {"actions": [{"action": "stop"}]},
        {"actions": [{"action": "click", "x": True, "y": 1}]},
        {"actions": [{"action": "click", "x": -1, "y": 1}]},
        {"actions": [{"action": "type", "text": "a\nb"}]},
        {"actions": [{"action": "type", "text": "abc", "interval": float("nan")}]},
        {"actions": [{"action": "type", "text": "abc", "interval": 151}]},
        {"actions": [{"action": "type", "text": "a" * 16385, "interval": 0}]},
        {
            "actions": [
                {"action": "scroll", "x": 1, "y": 1, "direction": "up", "steps": 10001}
            ]
        },
        {"actions": [{"action": "key", "chord": "Ctrl+Ctrl+a"}]},
        {"actions": [{"action": "type", "text": "abc", "intevral": 0}]},
        {"actions": [{"action": "key", "chord": "a"}], "repeat": 2},
        {"actions": [{"action": "key", "chord": "a"}], "capture": None},
        {"actions": [{"action": "key", "chord": "a"}], "capture": {"path": ""}},
        {"actions": [{"action": "key", "chord": "a"}], "capture": {"path": "a\0b"}},
        {
            "actions": [{"action": "key", "chord": "a"}],
            "capture": {"path": "/tmp/a", "delay": -1},
        },
        {
            "actions": [{"action": "key", "chord": "a"}],
            "capture": {"path": "/tmp/a", "delay": True},
        },
        {
            "actions": [{"action": "key", "chord": "a"}],
            "capture": {"path": "/tmp/a", "delay": 301},
        },
        {
            "actions": [{"action": "key", "chord": "a"}],
            "capture": {"path": "/tmp/a", "delai": 1},
        },
    ],
)
def test_reject_batch(raw: object) -> None:
    with pytest.raises(ValueError):
        Batch.parse(raw)


def test_defaults_and_capture_resolution(tmp_path: Path) -> None:
    batch = Batch.parse(
        {
            "actions": [
                {"action": "click", "x": 12, "y": 34},
                {"action": "type", "text": "é"},
            ],
            "capture": {"path": "image.png"},
        },
        directory=tmp_path,
    )
    assert batch.actions[0].parameters == {
        "x": 12,
        "y": 34,
        "button": "left",
        "count": 1,
        "modifier": [],
    }
    assert batch.actions[1].parameters["interval"] == 0.08
    assert batch.capture is not None
    assert batch.capture.path == tmp_path / "image.png"
    assert Batch.parse(batch.parameters()) == batch


def test_pacing_limit_is_for_whole_batch() -> None:
    with pytest.raises(ValueError, match="300 seconds"):
        Batch.parse(
            {"actions": [{"action": "type", "text": "ab", "interval": 151}] * 2}
        )


@pytest.mark.parametrize(
    "content",
    [
        b"{",
        b"x" * (MAX_REQUEST_BYTES + 1),
        b'{"actions":[{"action":"key","chord":"bad"}]}',
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
    (tmp_path / "session.json").write_text(json.dumps({"kind": "attached"}))
    path = tmp_path / "batch.json"
    path.write_text(json.dumps({"actions": [{"action": "key", "chord": "a"}]}))
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


def test_batch_rejects_older_runner_before_connecting(tmp_path: Path) -> None:
    (tmp_path / "session.json").write_text(
        json.dumps({"control_protocol": 1, "persistent_input": True})
    )
    plan = tmp_path / "batch.json"
    plan.write_text(json.dumps({"actions": [{"action": "key", "chord": "a"}]}))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path),
            "batch",
            "--file",
            str(plan),
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert "does not support batches" in result.stderr
    assert "Traceback" not in result.stderr
