# pyright: reportMissingModuleSource=false

import json
from pathlib import Path
from typing import cast

import pytest
from gi.repository import GLib

from framewisp.errors import SessionError
from framewisp.inspection import Bus, Query, inspect_bus, inspect_session


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_depth", 0),
        ("max_depth", 33),
        ("limit", 101),
        ("max_nodes", 4097),
        ("timeout", 0),
        ("timeout", float("inf")),
        ("timeout", 11),
        ("name", "x" * 1025),
    ],
)
def test_query_rejects_unbounded_arguments(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        Query(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "state",
    [
        {"kind": "attached"},
        {},
        {
            "inspection_protocol": 1,
            "runtime_directory": "/tmp/private",
            "accessibility_bus": "unix:path=/run/user/1000/at-spi/bus",
        },
    ],
)
def test_inspection_never_falls_back_to_desktop(
    tmp_path: Path, state: dict[str, object]
) -> None:
    (tmp_path / "session.json").write_text(json.dumps(state))
    with pytest.raises(SessionError):
        inspect_session(tmp_path, Query())


def test_disappearing_object_is_partial_not_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only D-Bus I/O is replaced. A child disappears between enumeration and read.
    def init(self: Bus, address: str, timeout: float) -> None:
        self.deadline = float("inf")

    def call(
        self: Bus,
        ref: tuple[str, str],
        interface: str,
        method: str,
        signature: str = "()",
        arguments: tuple[object, ...] = (),
    ) -> object:
        if method == "Get":
            return 1
        if method == "GetChildAtIndex":
            return (":1.2", "/vanished")
        raise GLib.Error("Object no longer exists")

    class Cancel:
        def is_cancelled(self) -> bool:
            return False

    def close(self: Bus) -> None:
        pass

    monkeypatch.setattr(Bus, "__init__", init)
    monkeypatch.setattr(Bus, "call", call)
    monkeypatch.setattr(Bus, "close", close)
    monkeypatch.setattr(Bus, "cancel", cast(object, Cancel()), raising=False)
    observation = inspect_bus("test", Query())
    assert observation["status"] == "partial"
    assert observation["matches"] == []
    assert observation["reasons"] == ["unavailable-object"]
    assert observation["errors"]
