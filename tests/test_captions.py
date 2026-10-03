import json
import os
import subprocess
import time
from io import BytesIO
from pathlib import Path

import pytest

from framewisp.captions import (
    Caption,
    InputEvent,
    caption_text,
    captions_for_clip,
    capture_origin,
    filter_recorder_output,
    log_input,
    recorder_output,
    retained_parameters,
    subtitle_script,
)


def test_recorder_filter_keeps_origin_and_diagnostics(tmp_path: Path) -> None:
    origin = b"[00:15:39.8] {Default Queue} zwlr_screencopy_frame_v1#6.ready(0, 123, 250000000)\n"
    later = b"[00:15:40.8] {Default Queue} zwlr_screencopy_frame_v1#7.ready(0, 124, 250000000)\n"
    diagnostic = b"[libx264 @ 0x123] encoding diagnostic\nUnable to open output file\n"
    trace = b"[00:15:39.7] {Default Queue}  -> wl_display#1.get_registry(new id wl_registry#2)\n"
    discarded = b"[00:15:40.9] {Default Queue} discarded wl_buffer#12.release()\n"
    output = BytesIO()
    filter_recorder_output(
        BytesIO(trace + origin + (later + discarded) * 10000 + diagnostic), output
    )
    assert output.getvalue() == origin + diagnostic
    log = tmp_path / "recorder.log"
    log.write_bytes(output.getvalue())
    assert capture_origin(log) == 123.25


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


@pytest.mark.parametrize("retain", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        None,
        RuntimeError("café 日本語 😀"),
        InterruptedError("café 日本語 😀"),
        subprocess.CalledProcessError(7, ["wtype", "café 日本語 😀"]),
    ],
)
def test_content_retention_and_captions(
    tmp_path: Path, retain: bool, failure: BaseException | None
) -> None:
    text = "café 日本語 😀"

    def perform() -> None:
        with log_input(
            tmp_path,
            "type",
            {"text": text, "interval": 0.1},
            retain_input_content=retain,
        ) as result:
            if failure is not None:
                raise failure
            result.returncode = 0

    if failure is None:
        perform()
    else:
        with pytest.raises(type(failure)):
            perform()
    raw = (tmp_path / "inputs.jsonl").read_text()
    events = [json.loads(line) for line in raw.splitlines()]
    assert (text in raw) is retain
    assert all(event["parameters"]["interval"] == 0.1 for event in events)
    captions = captions_for_clip(
        tmp_path / "inputs.jsonl",
        origin=events[0]["time"] - 0.1,
        stopped=events[-1]["time"] + 1,
    )
    assert (text in captions[0].text) is retain
    assert ("(failed)" in captions[0].text) is (failure is not None)
    if isinstance(failure, subprocess.CalledProcessError):
        assert "7" in events[-1]["error"]


@pytest.mark.parametrize(
    "chord,visible",
    [
        ("a", False),
        ("Shift+a", False),
        ("7", False),
        ("Shift+7", False),
        ("Space", False),
        ("Shift+Space", False),
        ("Ctrl+a", True),
        ("Alt+7", True),
        ("Return", True),
        ("Shift+Left", True),
    ],
)
@pytest.mark.parametrize("retain", [False, True])
def test_literal_key_retention(chord: str, visible: bool, retain: bool) -> None:
    p = retained_parameters("key", {"chord": chord}, retain_input_content=retain)
    assert ("chord" in p) is (visible or retain)
    event: InputEvent = {
        "id": "test",
        "event": "start",
        "time": 1,
        "action": "key",
        "parameters": p,
        "returncode": 0,
        "error": None,
    }
    assert caption_text(event) == (
        chord
        if visible or retain
        else ("Shift+Key" if chord.startswith("Shift") else "Key")
    )


def test_clip_excludes_setup_and_keeps_paced_and_unfinished_actions(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []
    for identifier, start, end, text in [
        ("setup", 9.0, 10.5, "not in clip"),
        ("paced", 10.2, 12.2, "café 日本語 😀"),
        ("late", 13.8, None, "still typing"),
        ("after", 15.0, 16.0, "between clips"),
    ]:
        entry: dict[str, object] = dict(
            id=identifier,
            action="type",
            parameters={"text": text},
            returncode=None,
            error=None,
        )
        events.append(entry | {"event": "start", "time": start})
        if end is not None:
            events.append(entry | {"event": "end", "time": end, "returncode": 0})
    log = tmp_path / "inputs.jsonl"
    log.write_text("\n".join(json.dumps(event) for event in events))
    captions = captions_for_clip(log, origin=10.0, stopped=14.0)
    assert len(captions) == 2
    assert captions[0].start == pytest.approx(0.2)
    assert captions[0].end == pytest.approx(2.2)
    assert captions[0].text == "Type: café 日本語 😀"
    assert captions[1].start == pytest.approx(3.8)
    assert captions[1].end == 4.0
    assert captions[1].text == "Type: still typing"
    assert captions_for_clip(log, origin=17.0, stopped=20.0) == []


def test_caption_text_cannot_inject_ass_formatting() -> None:
    script = subtitle_script([Caption(0.25, 1.5, r"Type: {\pos(0,0)}\N日本語")])
    assert "0:00:00.25,0:00:01.50" in script
    assert r"\pos" not in script
    assert r"\N" not in script
    assert "｛＼pos(0,0)｝＼N日本語" in script


@pytest.mark.parametrize("failure", [None, "cleanup failed: PID 123"])
def test_recorder_output_does_not_wait_forever_for_inherited_writer(
    tmp_path: Path, failure: str | None
) -> None:
    duplicate: int | None = None
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match=failure or "inherited log pipe"):
            with recorder_output(tmp_path / "recorder.log") as sink:
                duplicate = os.dup(sink.fileno())
                sink.write(b"partial diagnostic")
                sink.flush()
                if failure is not None:
                    raise RuntimeError(failure)
        assert time.monotonic() - started < 2
        assert (tmp_path / "recorder.log").read_bytes() == b"partial diagnostic"
    finally:
        if duplicate is not None:
            os.close(duplicate)
