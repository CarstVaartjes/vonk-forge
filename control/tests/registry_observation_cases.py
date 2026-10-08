"""Failure-to-recovery assertions for local inventory consumers."""

from unittest.mock import patch

import pytest

from . import registry_storage


def assert_replacement_recovers(loader, path, broken, replacement):
    registry_storage.write_registry(path, broken)
    observed = []
    read = registry_storage._read_once

    def observe(target):
        observed.append(target)
        return read(target)

    with patch.object(registry_storage, "_read_once", observe):
        with pytest.raises(RuntimeError):
            loader(path)
        assert len(observed) == 2
    registry_storage.write_registry(path, replacement)
    return loader(path)
