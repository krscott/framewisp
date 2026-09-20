import json
import os
from pathlib import Path

import pytest

from framewisp.x11 import type_text


@pytest.mark.integration
@pytest.mark.parametrize("mapped", [False, True])
def test_unicode_connection_failure_has_context(tmp_path: Path, mapped: bool) -> None:
    """Exercise X11 helper diagnostics without a live runner."""
    session = tmp_path / "disconnected-x11"
    session.mkdir()
    display = ":framewisp-missing"
    (session / "session.json").write_text(
        json.dumps(
            {
                "runtime_directory": str(tmp_path),
                "wayland_display": "wayland-missing",
                "x11_display": display,
            }
        )
    )
    if mapped:
        (tmp_path / "x11-keymap.json").write_text(json.dumps({"é": 120}))
    with pytest.raises(RuntimeError) as error:
        type_text(
            session,
            "é",
            interval=0,
            env=os.environ
            | {
                "XDG_RUNTIME_DIR": str(tmp_path),
                "DISPLAY": display,
                "XAUTHORITY": "/dev/null",
            },
        )
    message = str(error.value)
    assert ("xdotool" if mapped else "xmodmap") in message
    assert display in message
    assert str(session) in message and str(tmp_path) in message
    assert "sandbox" in message
