from __future__ import annotations

from .added_line_guards import check_source
from .contract_constant_copies import contract_constants, scan_literals, scan_source

NAMES = frozenset({"MAX_RUNTIME_ARGUMENTS", "MIN_SPEECH_SECONDS"})
PATH = "control/src/vonk_control/example.py"


def test_a_restated_contract_constant_is_found_by_name_not_value() -> None:
    source = "_MAX_RUNTIME_ARGUMENTS = 64\nMIN_SPEECH_SECONDS: float = 0.5\n"
    assert scan_source(PATH, source, NAMES) == [1, 2]
    assert scan_source("src/cluster_profiles/x.py", source, NAMES) == [1, 2]
    assert scan_source("agent_protocol/src/x.py", source, NAMES) == [1, 2]


def test_other_names_comparisons_and_other_trees_are_left_alone() -> None:
    source = "LIMIT = 128\nif MIN_SPEECH_SECONDS == 1:\n    pass\n    X_MIN_SPEECH_SECONDS = 1\n"
    assert scan_source(PATH, source, NAMES) == []
    assert scan_source("tests/test_x.py", "MIN_SPEECH_SECONDS = 1\n", NAMES) == []


def test_the_installed_contracts_export_the_constants_this_change_imports() -> None:
    assert {"MIN_SPEECH_SECONDS", "MAX_RUNTIME_ARGUMENTS"} <= contract_constants()


def test_only_an_added_line_fails_the_mode() -> None:
    source = "MIN_SPEECH_SECONDS = 0.5\n"
    assert check_source(PATH, source, {1}, modes=("contract-constants",))
    assert not check_source(PATH, source, {2}, modes=("contract-constants",))


def test_the_index_kind_and_media_type_literals_are_found_outside_the_contracts() -> (
    None
):
    source = 'KIND = "recipe-library-index"\nM = "application/vnd.vonk-forge.recipe-package.v2+tar+gzip"\nX = 1\n'
    assert scan_literals(PATH, source) == [1, 2]
    assert scan_literals("tests/test_x.py", source) == [1, 2]
    assert scan_literals("x/vonk_forge_contracts/catalog_index.py", source) == []
    assert check_source(PATH, source, {1}, modes=("contract-constants",))
