"""CLI splits retain effective patches and bounded source ownership."""

from pathlib import Path

import pytest

from tests.test_run_switch_patch_targets import facade_patch_lines

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("controller_cli", "cli_render", "control_client", "qualification_fixtures")


def test_cli_test_doubles_patch_the_lookup_owner() -> None:
    """Replacing a facade export must not silently leave the consumer unchanged."""
    failures = {
        str(path.relative_to(ROOT)): lines
        for directory in (ROOT / "tests", ROOT / "control/tests")
        for path in directory.rglob("*.py")
        if (lines := facade_patch_lines(path.read_text(encoding="utf-8")))
    }
    assert not failures, f"Patch the defining lookup module: {failures}"


@pytest.mark.parametrize("package", PACKAGES)
def test_cli_concerns_stay_below_one_thousand_lines(package: str) -> None:
    """A moved concern cannot grow back into an allowlisted monolith."""
    directory = ROOT / "src/cluster_profiles" / package
    assert directory.is_dir()
    assert not directory.with_suffix(".py").exists()
    for path in directory.glob("*.py"):
        assert len(path.read_text().splitlines()) < 1000, path


@pytest.mark.parametrize("package", PACKAGES)
def test_guard_rejects_cli_facade_replacement(package: str) -> None:
    assert facade_patch_lines(
        f"from cluster_profiles import {package} as owner\n"
        'monkeypatch.setattr(owner, "helper", fake)'
    )
    assert not facade_patch_lines(
        f'monkeypatch.setattr("cluster_profiles.{package}.concern.helper", fake)'
    )


def test_exported_renderer_uses_the_dispatch_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A test double on the real dispatch binding changes the public renderer."""
    from cluster_profiles.cli_render import dispatch, render_payload

    calls = []

    def present(payload, *, wide):
        calls.append((payload, wide))

    monkeypatch.setattr(dispatch, "_fleet_overview", present)
    render_payload({}, "fleet", wide=True)
    assert calls == [({}, True)]
