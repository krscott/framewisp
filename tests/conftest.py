"""Split installed-package tests between isolated CI runners."""

from typing import cast

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--test-shards", type=int, default=1)
    parser.addoption("--test-shard", type=int, default=1)


def pytest_configure(config: pytest.Config) -> None:
    shards = cast(int, config.getoption("test_shards"))
    shard = cast(int, config.getoption("test_shard"))
    if not 1 <= shard <= shards:
        raise pytest.UsageError("Require 1 <= --test-shard <= --test-shards")


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    shards = cast(int, config.getoption("test_shards"))
    shard = cast(int, config.getoption("test_shard"))
    if shards == 1:
        return
    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for index, item in enumerate(items):
        (selected if index % shards == shard - 1 else deselected).append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)
