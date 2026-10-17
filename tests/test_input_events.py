import json
from pathlib import Path

import pytest
from test_integration import read_input_events


def test_input_events_wait_for_complete_records(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text(
        'GTK warning\n{"event": "ready", "time": 1}\n{"event": "press", "ctrl": f'
    )
    assert read_input_events(log) == [{"event": "ready", "time": 1}]

    with log.open("a") as output:
        output.write("alse}\n")
    assert read_input_events(log) == [
        {"event": "ready", "time": 1},
        {"event": "press", "ctrl": False},
    ]


def test_input_events_reject_malformed_complete_records(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text('{"event": broken}\n')
    with pytest.raises(json.JSONDecodeError):
        read_input_events(log)
