import fcntl
import json
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import BinaryIO

import pytest

from framewisp.compact import activity_events
from framewisp.errors import SessionError
from framewisp.input_log import (
    CaptionCapture,
    input_error,
    log_input,
    retained_parameters,
)


def test_log_records_nonzero_and_exception_outcomes(tmp_path: Path) -> None:
    with log_input(tmp_path, "key", {"chord": "Ctrl+s"}) as result:
        result.returncode = 1
    with pytest.raises(RuntimeError, match="input failed"):
        with log_input(tmp_path, "type", {"text": "café 日本語 😀"}):
            raise RuntimeError("input failed")
    events = [
        json.loads(line)
        for line in (tmp_path / "inputs.jsonl").read_text().splitlines()
    ]
    first, second = events[:2], events[2:]
    assert first[0]["event"] == "start"
    assert first[1]["event"] == "end"
    assert first[0]["id"] == first[1]["id"]
    assert first[1]["time"] >= first[0]["time"]
    assert first[1]["returncode"] == 1
    assert second[1]["error"] == "RuntimeError: input details omitted"
    assert "text" not in second[0]["parameters"]
    assert second[0]["id"] != first[0]["id"]


def test_pointer_failure_keeps_exception_type(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        with log_input(tmp_path, "click", {"x": 1, "y": 2}):
            raise ValueError("bad coordinates")
    events = [
        json.loads(line)
        for line in (tmp_path / "inputs.jsonl").read_text().splitlines()
    ]
    assert events[-1]["error"] == "ValueError: bad coordinates"


def test_input_error_keeps_structured_exit_code_only() -> None:
    text = "secret (exit 123456)"
    for error in [RuntimeError(text), SessionError(text)]:
        assert "123456" not in input_error("type", error)
    assert (
        input_error("type", SessionError(text, returncode=7))
        == "SessionError: input details omitted; exit 7"
    )
    assert (
        input_error(
            "type",
            SessionError(text, input_message="Display connection failed"),
        )
        == "SessionError: Display connection failed"
    )


# R-LOG-1
@pytest.mark.parametrize(
    "chord,visible",
    [
        ("a", False),
        ("Shift+a", False),
        ("7", False),
        ("Shift+7", False),
        ("Space", False),
        ("Shift+Space", False),
        ("comma", False),
        (",", False),
        ("Shift+comma", False),
        ("plus", False),
        ("+", False),
        ("Ctrl++", True),
        ("Ctrl+comma", True),
        ("Alt+slash", True),
        ("Page_Up", True),
        ("F12", True),
        ("Ctrl+a", True),
        ("Alt+7", True),
        ("Return", True),
        ("Shift+Left", True),
    ],
)
def test_literal_key_retention(chord: str, visible: bool) -> None:
    p = retained_parameters("key", {"chord": chord})
    assert ("chord" in p) is visible


# R-LOG-7, R-LOG-8
@pytest.mark.parametrize(
    "prefix",
    [b"", b'{"id":"saved"}\n', b"bad json\n"],
    ids=["empty", "complete", "corrupt-complete"],
)
@pytest.mark.parametrize(
    "tail",
    [b"", b'{"id":', b'{"text":"caf\xc3', b'{"id":"uncommitted"}', b"x" * 20000],
    ids=[
        "empty",
        "json-fragment",
        "split-utf8",
        "json-without-newline",
        "multi-chunk-tail",
    ],
)
def test_append_discards_only_unterminated_bytes(
    tmp_path: Path, prefix: bytes, tail: bytes
) -> None:
    log = tmp_path / "inputs.jsonl"
    log.write_bytes(prefix + tail)
    for index in range(2):
        with log_input(tmp_path, "key", {"chord": "Ctrl+s"}) as result:
            result.returncode = 0
        data = log.read_bytes()
        assert data.startswith(prefix)
        appended = [json.loads(line) for line in data[len(prefix) :].split(b"\n")[:-1]]
        assert len(appended) == 2 * (index + 1)
        assert [entry["event"] for entry in appended[-2:]] == ["start", "end"]
        assert appended[-2]["id"] == appended[-1]["id"]
        # Simulate another interrupted write before the next action.
        with log.open("ab") as output:
            output.write(tail)


def test_append_waits_for_live_writer_before_repairing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "inputs.jsonl"
    capture = CaptionCapture()
    waiting = Event()
    flock = fcntl.flock

    def acquire(output: BinaryIO, operation: int) -> None:
        assert operation == fcntl.LOCK_EX
        if not waiting.is_set():
            # Prove contention on a real file lock before allowing the live
            # writer to finish. No scheduler timing decides whether they race.
            with pytest.raises(BlockingIOError):
                flock(output, operation | fcntl.LOCK_NB)
            waiting.set()
        flock(output, operation)

    monkeypatch.setattr(fcntl, "flock", acquire)

    def append() -> None:
        with log_input(
            tmp_path, "key", {"chord": "s"}, caption_capture=capture
        ) as result:
            result.returncode = 0

    with (
        ThreadPoolExecutor(max_workers=2) as executor,
        capture.clip(enabled=True) as literal_captions,
    ):
        with log.open("ab") as live:
            flock(live, fcntl.LOCK_EX)
            fragment = b'{"id":"live","event":"start","time":1'
            live.write(fragment)
            live.flush()
            task = executor.submit(append)
            try:
                assert waiting.wait(timeout=5), "Appender did not contend on the lock"
                assert log.read_bytes() == fragment
                stopped = executor.submit(capture.stop).result(timeout=5)
            finally:
                live.write(b"}\n")
                # Close flushes the complete live record before releasing flock.
        task.result(timeout=5)
        assert literal_captions == {}
    monkeypatch.setattr(fcntl, "flock", flock)
    events = activity_events(log)
    assert events[0]["id"] == "live"
    assert [event["event"] for event in events[1:]] == ["start", "end"]
    assert events[1]["time"] >= stopped


# R-LOG-7, R-LOG-8
def test_reader_snapshot_excludes_concurrent_tail_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "inputs.jsonl"
    saved = {"id": "saved", "event": "start", "time": 1}
    log.write_bytes(json.dumps(saved).encode() + b'\n{"id":"partial')
    waiting = Event()
    flock = fcntl.flock
    task: Future[None] | None = None

    def append() -> None:
        with log_input(tmp_path, "key", {"chord": "Ctrl+s"}) as result:
            result.returncode = 0

    with ThreadPoolExecutor(max_workers=1) as executor:

        def acquire(output: BinaryIO, operation: int) -> None:
            nonlocal task
            if operation == fcntl.LOCK_SH:
                flock(output, operation)
                # Attempt repair while the reader owns its snapshot. The real
                # writer must wait until reading and descriptor close finish.
                task = executor.submit(append)
                assert waiting.wait(timeout=5), "Writer did not contend on the lock"
            else:
                assert operation == fcntl.LOCK_EX
                if not waiting.is_set():
                    with pytest.raises(BlockingIOError):
                        flock(output, operation | fcntl.LOCK_NB)
                    waiting.set()
                flock(output, operation)

        monkeypatch.setattr(fcntl, "flock", acquire)
        assert activity_events(log) == [saved]
        assert task is not None, "Reader did not lock its snapshot"
        task.result(timeout=5)
    monkeypatch.setattr(fcntl, "flock", flock)
    events = activity_events(log)
    assert events[0] == saved
    assert [event["event"] for event in events[1:]] == ["start", "end"]
