# pyright: reportMissingModuleSource=false

import json
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from gi.repository import Gio, GLib

from framewisp.errors import SessionError
from framewisp.inspection import ROOT, Bus, Query, inspect_bus, inspect_session


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


@pytest.mark.parametrize(
    "query", [Query(), Query(role="button"), Query(name="Apply"), Query(text="Applied")]
)
def test_defunct_object_cannot_become_complete_no_match(
    monkeypatch: pytest.MonkeyPatch, query: Query
) -> None:
    def init(self: Bus, address: str, timeout: float) -> None:
        pass

    def close(self: Bus) -> None:
        pass

    def call(
        self: Bus,
        ref: tuple[str, str],
        interface: str,
        method: str,
        signature: str = "()",
        arguments: tuple[object, ...] = (),
    ) -> object:
        if method == "Get":
            return 1 if arguments[-1] == "ChildCount" else ""
        if method == "GetChildAtIndex":
            return (":1.2", "/defunct")
        if method == "GetState":
            return [1 << 6, 0]
        if method == "GetRoleName":
            return "unknown"
        if method == "GetInterfaces":
            return []
        raise AssertionError(method)

    monkeypatch.setattr(Bus, "__init__", init)
    monkeypatch.setattr(Bus, "call", call)
    monkeypatch.setattr(Bus, "close", close)
    observation = inspect_bus("test", query)
    assert observation["status"] == "partial"
    assert observation["matches"] == []
    assert observation["reasons"] == ["stale-object"]


@pytest.mark.parametrize("text_filter", [None, "Applied"])
def test_failed_text_keeps_readable_matching_fields(
    monkeypatch: pytest.MonkeyPatch, text_filter: str | None
) -> None:
    def init(self: Bus, address: str, timeout: float) -> None:
        self.deadline = float("inf")
        self.cancel = Gio.Cancellable()

    def close(self: Bus) -> None:
        pass

    def call(
        self: Bus,
        ref: tuple[str, str],
        interface: str,
        method: str,
        signature: str = "()",
        arguments: tuple[object, ...] = (),
    ) -> object:
        if method == "GetChildAtIndex":
            return (":1.2", "/label")
        if method == "GetState":
            return [0, 0]
        if method == "GetRoleName":
            return "label"
        if method == "GetInterfaces":
            return ["org.a11y.atspi.Text"]
        if method == "Get":
            if arguments[-1] == "ChildCount":
                return 1 if ref == ROOT else 0
            if arguments[-1] == "Name":
                return "Apply"
            if arguments[-1] == "CharacterCount":
                raise GLib.Error("Text became unavailable")
        raise AssertionError(method)

    monkeypatch.setattr(Bus, "__init__", init)
    monkeypatch.setattr(Bus, "call", call)
    monkeypatch.setattr(Bus, "close", close)
    observation = inspect_bus("test", Query(name="Apply", text=text_filter))
    assert observation["status"] == "partial"
    nodes = cast(list[dict[str, object]], observation["matches"])
    if text_filter is None:
        assert len(nodes) == 1
        assert nodes[0]["name"] == "Apply"
        assert nodes[0]["text"] is None
    else:
        assert nodes == []


@pytest.fixture
def private_bus(tmp_path: Path) -> Iterator[tuple[subprocess.Popen[bytes], str]]:
    with tempfile.TemporaryDirectory(prefix="fw-test-bus-") as directory:
        path = Path(directory) / "bus.sock"
        address = f"unix:path={path}"
        config = tmp_path / "bus.conf"
        config.write_text(
            "<busconfig><type>session</type><listen>unix:tmpdir=/tmp</listen>"
            '<auth>EXTERNAL</auth><policy context="default">'
            '<allow send_destination="*"/><allow receive_sender="*"/>'
            '<allow own="*"/></policy></busconfig>'
        )
        log = tmp_path / "bus.log"
        with log.open("wb") as output:
            daemon = subprocess.Popen(
                [
                    "dbus-daemon",
                    f"--config-file={config}",
                    "--nofork",
                    f"--address={address}",
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        try:
            deadline = time.monotonic() + 5
            while not path.exists():
                assert daemon.poll() is None, log.read_text()
                assert time.monotonic() < deadline
                time.sleep(0.01)
            yield daemon, address
        finally:
            if daemon.poll() is None:
                daemon.terminate()
            daemon.wait(timeout=5)


def test_inspection_cleanup_after_bus_disconnect(
    private_bus: tuple[subprocess.Popen[bytes], str],
) -> None:
    daemon, address = private_bus
    bus = Bus(address, 2)
    daemon.terminate()
    daemon.wait(timeout=5)
    # Reading from the dead bus records closure before cleanup, as in inspection.
    with pytest.raises(GLib.Error):
        bus.call(ROOT, "org.a11y.atspi.Accessible", "GetState")
    bus.close()
    bus.close()
