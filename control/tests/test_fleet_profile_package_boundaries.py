"""Profile concerns cannot grow back into an exempted monolith."""

from pathlib import Path


def test_fleet_profile_concerns_remain_bounded_without_size_exemptions() -> None:
    import vonk_control.fleet_profiles as profiles

    package = Path(profiles.__file__).parent
    assert not package.with_suffix(".py").exists()
    oversized = {
        path.name: len(path.read_text().splitlines())
        for path in package.glob("*.py")
        if len(path.read_text().splitlines()) >= 1000
    }
    assert not oversized, f"split the growing profile concern: {oversized}"


def test_profile_words_are_consumed_from_the_generated_contract_vocabulary() -> None:
    import ast
    from enum import Enum

    import vonk_control.fleet_profiles as profiles
    from vonk_agent_protocol import agent_words

    words = {
        member.value
        for name, definition in vars(agent_words).items()
        if name.startswith("Profile")
        and isinstance(definition, type)
        and issubclass(definition, Enum)
        for member in definition
    }
    violations = []
    for path in Path(profiles.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text())
        keys = {
            id(key)
            for node in ast.walk(tree)
            if isinstance(node, ast.Dict)
            for key in node.keys
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in words
                and id(node) not in keys
            ):
                violations.append((path.name, node.lineno, node.value))
    assert not violations, f"use the canonical profile enum: {violations}"


def test_profile_typing_cannot_borrow_an_untyped_mapping_allowance() -> None:
    import vonk_control.fleet_profiles as profiles

    from .untyped_mapping_boundaries import scan_source

    sites = [
        site
        for path in Path(profiles.__file__).parent.glob("*.py")
        for site in scan_source(path.read_text(), path=path.name)
    ]
    assert not sites, f"profile documents require typed models: {sites}"


def test_profile_bookkeeping_cannot_borrow_lifecycle_debt_from_another_owner() -> None:
    from .blocker_boundaries import load_allowlist
    from .blocker_classifier import DEBT

    families = load_allowlist()["fail_closed"]
    assert isinstance(families, list)
    debt = []
    for family in families:
        assert isinstance(family, dict)
        if family["category"] != DEBT:
            continue
        sites = family["sites"]
        assert isinstance(sites, list)
        debt.extend(site for site in sites if "/fleet_profiles/" in site[0])
    assert not debt, f"register the bounded retry or end without holds: {debt}"
