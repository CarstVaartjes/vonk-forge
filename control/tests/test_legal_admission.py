"""License notices must never become technical admission authority."""

import pytest
from vonk_control.legal_admission import territorial_admission


@pytest.mark.parametrize(
    "document",
    [
        None,
        {},
        {"license": None},
        {"license": {"territorial_restrictions": "bad"}},
        {
            "license": {
                "territorial_restrictions": {
                    "denied_jurisdictions": [1],
                    "notice": "notice",
                }
            }
        },
    ],
)
@pytest.mark.parametrize("operation", ["install", "run"])
def test_missing_or_malformed_license_metadata_cannot_refuse_admission(
    document, operation
):
    assert territorial_admission(document, operation=operation).warning is None
