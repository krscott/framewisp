import json
from io import BytesIO
from pathlib import Path

import pytest

from framewisp.captions import (
    Caption,
    captions_for_clip,
    capture_origin,
    filter_recorder_output,
    log_input,
    subtitle_script,
)


def test_recorder_filter_keeps_origin_and_diagnostics(tmp_path: Path) -> None:
    origin = b"[00:15:39.8] {Default Queue} zwlr_screencopy_frame_v1#6.ready(0, 123, 250000000)\n"
    later = b"[00:15:40.8] {Default Queue} zwlr_screencopy_frame_v1#7.ready(0, 124, 250000000)\n"
    diagnostic = b"[libx264 @ 0x123] encoding diagnostic\nUnable to open output file\n"
    trace = b"[00:15:39.7] {Default Queue}  -> wl_display#1.get_registry(new id wl_registry#2)\n"
    output = BytesIO()
    filter_recorder_output(BytesIO(trace + origin + later * 10000 + diagnostic), output)
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
    assert second[1]["error"] == "RuntimeError: input failed"
    assert second[0]["parameters"]["text"] == "café 日本語 😀"
    assert second[0]["id"] != first[0]["id"]


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
