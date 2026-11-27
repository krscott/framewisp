import json
import subprocess
import time
from pathlib import Path

import pytest

from framewisp.actions import ACTION_FIELDS
from framewisp.finalization import (
    POINTER_ACTIONS,
    Caption,
    CaptionOptions,
    captions_for_clip,
    raw_clip_failure,
    subtitle_script,
)
from framewisp.input_log import KEY_ACTIONS, CaptionCapture, log_input
from framewisp.keys import KEYS, parse_chord
from framewisp.layout import plain_layout


# R-LOG-1, R-LOG-9
@pytest.mark.parametrize("private", [False, True])
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
    tmp_path: Path, private: bool, failure: BaseException | None
) -> None:
    text = "café 日本語 😀"
    capture = CaptionCapture()

    def perform() -> None:
        with log_input(
            tmp_path,
            "type",
            {"text": text, "interval": 0.1, "private": private},
            caption_capture=capture,
        ) as result:
            if failure is not None:
                raise failure
            result.returncode = 0

    with capture.clip(enabled=True) as literal_captions:
        if failure is None:
            perform()
        else:
            with pytest.raises(type(failure)):
                perform()
    raw = (tmp_path / "inputs.jsonl").read_text()
    events = [json.loads(line) for line in raw.splitlines()]
    assert text not in raw
    assert all(event["parameters"]["interval"] == 0.1 for event in events)
    captions = captions_for_clip(
        tmp_path / "inputs.jsonl",
        origin=events[0]["time"] - 0.1,
        stopped=events[-1]["time"] + 1,
        captions=CaptionOptions(key="all"),
        literal_captions=literal_captions,
    )
    if private:
        assert literal_captions == {}
        assert captions == []
    else:
        assert captions[0].text == f"Type: {text}" + (" (failed)" if failure else "")
    if isinstance(failure, subprocess.CalledProcessError):
        assert "7" in events[-1]["error"]
        assert events[-1]["returncode"] == 7


# R-LOG-9
@pytest.mark.parametrize(
    "captions", [CaptionOptions(key="all"), CaptionOptions(key="non-text")]
)
def test_private_key_omits_shortcut_and_caption(
    tmp_path: Path, captions: CaptionOptions
) -> None:
    capture = CaptionCapture()
    log = tmp_path / "inputs.jsonl"
    with capture.clip(enabled=True) as literal_captions:
        with log_input(
            tmp_path,
            "key",
            {"chord": "Ctrl+a", "private": True},
            caption_capture=capture,
        ) as result:
            result.returncode = 0
        with log_input(
            tmp_path, "key", {"chord": "Return"}, caption_capture=capture
        ) as result:
            result.returncode = 0
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert [event["parameters"] for event in events[:2]] == [{"private": True}] * 2
    assert events[1]["returncode"] == 0
    entries = captions_for_clip(
        log,
        origin=0,
        stopped=time.monotonic() + 1,
        captions=captions,
        literal_captions=literal_captions,
    )
    assert [entry.text for entry in entries] == ["Return"]


# Named keys that do not type a character, so `key` may log them as shortcuts.
CONTROL_KEYS = {
    "enter",
    "tab",
    "bsp",
    "esc",
    "delete",
    "left",
    "right",
    "up",
    "down",
    "home",
    "end",
    "pgup",
    "pgdn",
    *(f"f{number}" for number in range(1, 13)),
}


# R-LOG-1
@pytest.mark.parametrize("mode", [CaptionOptions(), CaptionOptions(key="non-text")])
def test_no_literal_character_reaches_a_default_caption(
    tmp_path: Path, mode: CaptionOptions
) -> None:
    names = [name for name, symbol in KEYS.items() if symbol not in CONTROL_KEYS]
    assert {"a", "7", "space", "comma", ",", "plus", "+"} <= set(names)
    chords = [
        chord
        for name in names
        for chord in (name, name.upper(), f"Shift+{name}", f"shift+{name.upper()}")
    ]
    for chord in chords:
        assert parse_chord(chord) is not None
        with log_input(tmp_path, "key", {"chord": chord}) as result:
            result.returncode = 0
    text = "pass word 7, ünï"
    with log_input(tmp_path, "type", {"text": text, "interval": 0}) as result:
        result.returncode = 0

    log = tmp_path / "inputs.jsonl"
    assert text not in log.read_text()
    assert '"chord"' not in log.read_text()
    captions = captions_for_clip(
        log, origin=0, stopped=time.monotonic() + 1, captions=mode
    )
    assert captions == []


def input_log(path: Path, *entries: tuple[str, dict[str, object], float]) -> Path:
    """Write finished input spans, each given as action, parameters, and start."""
    log = path / "inputs.jsonl"
    log.write_text(
        "".join(
            json.dumps(
                {
                    "id": str(index),
                    "action": action,
                    "parameters": parameters,
                    "event": event,
                    "time": timestamp,
                    "returncode": 0,
                    "error": None,
                }
            )
            + "\n"
            for index, (action, parameters, start) in enumerate(entries)
            for event, timestamp in (("start", start), ("end", start + 0.1))
        )
    )
    return log


# R-LOG-4
MOVE = "Move (1, 2)"
TYPE = "Type: café"
CLICK = "left click (3, 4)"
DRAG = "Shift+left drag (5, 6) to (7, 8)"
KEY = "Return"
SCROLL = "Scroll up 1 at (9, 10)"


@pytest.mark.parametrize(
    "mode,expected",
    [
        (CaptionOptions(), []),
        (CaptionOptions(key="non-text"), [KEY]),
        (CaptionOptions(key="all"), [TYPE, KEY]),
        (CaptionOptions(pointer="all"), [MOVE, CLICK, DRAG, SCROLL]),
        (CaptionOptions(pointer="modifiers"), [DRAG]),
        (
            CaptionOptions(pointer="all", key="all"),
            [MOVE, TYPE, CLICK, DRAG, KEY, SCROLL],
        ),
        (CaptionOptions(pointer="modifiers", key="non-text"), [DRAG, KEY]),
    ],
)
def test_caption_mode_selects_input_kinds(
    tmp_path: Path, mode: CaptionOptions, expected: list[str]
) -> None:
    log = input_log(
        tmp_path,
        ("move", {"x": 1, "y": 2}, 1),
        ("type", {"interval": 0}, 2),
        ("click", {"x": 3, "y": 4, "button": "left", "count": 1}, 3),
        (
            "drag",
            {
                "x1": 5,
                "y1": 6,
                "x2": 7,
                "y2": 8,
                "button": "left",
                "modifier": ["shift"],
            },
            4,
        ),
        ("key", {"chord": "Return"}, 5),
        ("scroll", {"x": 9, "y": 10, "direction": "up", "ticks": 1}, 6),
    )
    captions = captions_for_clip(
        log, origin=0, stopped=9, captions=mode, literal_captions={"1": TYPE}
    )
    assert [caption.text for caption in captions] == expected


def test_every_input_action_has_exactly_one_caption_kind() -> None:
    assert KEY_ACTIONS | POINTER_ACTIONS == set(ACTION_FIELDS)
    assert not KEY_ACTIONS & POINTER_ACTIONS


def test_caption_ends_at_next_captioned_input_only(tmp_path: Path) -> None:
    log = input_log(
        tmp_path,
        ("key", {"chord": "Return"}, 1),
        ("move", {"x": 1, "y": 2}, 1.1),
        ("key", {"chord": "Tab"}, 1.5),
        ("click", {"x": 1, "y": 2, "button": "left", "count": 1}, 1.6),
    )
    first, second = captions_for_clip(
        log, origin=0, stopped=9, captions=CaptionOptions(key="non-text")
    )
    assert first.end == pytest.approx(1.5)
    assert second.end == pytest.approx(2.3)
    assert (
        captions_for_clip(
            log, origin=0, stopped=9, captions=CaptionOptions(pointer="all", key="all")
        )[0].end
        == 1.1
    )


def test_clip_excludes_setup_and_keeps_paced_and_unfinished_actions(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []
    literal_captions: dict[str, str] = {}
    for identifier, start, end, text in [
        ("setup", 9.0, 10.5, "not in clip"),
        ("paced", 10.2, 12.2, "café 日本語 😀"),
        ("late", 13.8, None, "still typing"),
        ("after", 15.0, 16.0, "between clips"),
    ]:
        entry: dict[str, object] = dict(
            id=identifier,
            action="type",
            parameters={"interval": 0},
            returncode=None,
            error=None,
        )
        literal_captions[identifier] = f"Type: {text}"
        events.append(entry | {"event": "start", "time": start})
        if end is not None:
            events.append(entry | {"event": "end", "time": end, "returncode": 0})
    log = tmp_path / "inputs.jsonl"
    log.write_text("".join(json.dumps(event) + "\n" for event in events))
    captions = captions_for_clip(
        log,
        origin=10.0,
        stopped=14.0,
        captions=CaptionOptions(key="all"),
        literal_captions=literal_captions,
    )
    assert len(captions) == 2
    assert captions[0].start == pytest.approx(0.2)
    assert captions[0].end == pytest.approx(2.2)
    assert captions[0].text == "Type: café 日本語 😀"
    assert captions[1].start == pytest.approx(3.8)
    assert captions[1].end == 4.0
    assert captions[1].text == "Type: still typing"
    assert (
        captions_for_clip(
            log,
            origin=17.0,
            stopped=20.0,
            captions=CaptionOptions(key="all"),
            literal_captions=literal_captions,
        )
        == []
    )


# R-LOG-7
@pytest.mark.parametrize(
    "unfinished",
    [
        b'{"id": "next", "event": "st',
        # The write stopped after the first byte of a UTF-8 character.
        b'{"id": "next", "event": "start", "parameters": {"text": "caf\xc3',
        # Only the newline is missing, so the record is still unfinished.
        b'{"id": "next", "event": "start", "time": 2.0}',
    ],
)
def test_unfinished_last_line_is_ignored(tmp_path: Path, unfinished: bytes) -> None:
    log = input_log(tmp_path, ("key", {"chord": "Return"}, 1.0))
    with log.open("ab") as output:
        output.write(unfinished)
    captions = captions_for_clip(
        log, origin=0, stopped=5, captions=CaptionOptions(key="non-text")
    )
    assert captions == [Caption(1.0, 1.8, "Return")]


def test_literal_caption_content_stays_in_its_clip(tmp_path: Path) -> None:
    capture = CaptionCapture()
    log = tmp_path / "inputs.jsonl"
    with capture.clip(enabled=True) as first:
        with log_input(
            tmp_path,
            "type",
            {"text": "café 日本語 😀", "interval": 0},
            caption_capture=capture,
        ) as result:
            result.returncode = 0
        with log_input(
            tmp_path, "key", {"chord": "Shift+a"}, caption_capture=capture
        ) as result:
            result.returncode = 0
        stopped = capture.stop()
        with log_input(tmp_path, "key", {"chord": "7"}, caption_capture=capture):
            pass
    with capture.clip(enabled=True) as second:
        with log_input(tmp_path, "key", {"chord": "Space"}, caption_capture=capture):
            pass
    assert list(first.values()) == ["Type: café 日本語 😀", "Shift+a"]
    assert list(second.values()) == ["Space"]
    assert [
        entry.text
        for entry in captions_for_clip(
            log,
            origin=0,
            stopped=stopped,
            captions=CaptionOptions(key="all"),
            literal_captions=first,
        )
    ] == ["Type: café 日本語 😀", "Shift+a"]
    assert "café" not in log.read_text()


def test_caption_text_cannot_inject_ass_formatting() -> None:
    script = subtitle_script(
        [Caption(0.25, 1.5, r"Type: {\pos(0,0)}\N日本語")],
        layout=plain_layout((1280, 720)),
    )
    assert "0:00:00.25,0:00:01.50" in script
    assert r"\pos" not in script
    assert r"\N" not in script
    assert "\\{\\\u2060pos(0,0)\\}\\\u2060N日本語" in script


# R-REC-10
def test_raw_clip_failure_names_the_clip_missing_extras_and_logs() -> None:
    error = str(
        raw_clip_failure(
            Path("clip.mp4"),
            ValueError("bad line"),
            extras=True,
            logs=(Path("finalization.log"), Path("inputs.jsonl")),
        )
    )
    assert error == (
        "Finalization failed; raw clip remains at clip.mp4 without overlays, "
        "console panel or compaction. See finalization.log and inputs.jsonl. "
        "Cause: bad line"
    )
    assert str(raw_clip_failure(Path("clip.mp4"), ValueError("x"), extras=False)) == (
        "Finalization failed; raw clip remains at clip.mp4. Cause: x"
    )
