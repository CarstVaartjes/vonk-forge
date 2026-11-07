"""License notices must never become technical admission authority."""

import pytest
from vonk_control.legal_admission import territorial_admission
from vonk_forge_contracts import ModelDisplay, read_model_display

from .display_fixtures import model_display_denying


def _document() -> dict:
    return {
        "kind": "model",
        "identity": {"publisher": "vonk-forge", "slug": "synthetic-tiny-fp16"},
    }


@pytest.mark.parametrize("operation", ["install", "run"])
def test_a_missing_display_cannot_refuse_admission(operation):
    assert territorial_admission(None, operation=operation).warning is None


@pytest.mark.parametrize("operation", ["install", "run"])
def test_a_display_without_restrictions_has_no_warning(operation):
    display = read_model_display(model_display_denying(_document(), ("EU",), "x"))
    unrestricted = ModelDisplay.model_validate(
        {
            **display.model_dump(mode="json"),
            "license": {
                **display.license.model_dump(mode="json", exclude_none=True),
                "territorial_restrictions": None,
            },
        }
    )
    assert territorial_admission(unrestricted, operation=operation).warning is None


@pytest.mark.parametrize("operation", ["install", "run"])
def test_declared_restrictions_are_a_warning_not_a_refusal(operation):
    display = read_model_display(
        model_display_denying(_document(), ("EU", "GB"), "Synthetic notice.")
    )
    warning = territorial_admission(display, operation=operation).warning
    assert warning is not None
    code, detail = warning
    assert code == f"{operation}.license_territorial_restrictions_informational"
    assert "EU, GB" in detail and "informational" in detail
