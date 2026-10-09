"""The Controller's release trust anchor against a real published signature.

The fixture is the SHA256SUMS and Sigstore bundle that the recipe repository's
publish.yml attached to release v1.1.0, so these cases exercise the actual
producer-to-consumer signature handoff offline, with the packaged trust root.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from vonk_control import recipe_release
from vonk_control.recipe_release import (
    RecipeReleaseError,
    parse_release_checksums,
    verify_release_checksums,
)

FIXTURE = Path(__file__).parent / "fixtures" / "recipe-release-v1.1.0"
CHECKSUMS = (FIXTURE / "SHA256SUMS").read_bytes()
BUNDLE = (FIXTURE / "SHA256SUMS.sigstore.json").read_bytes()
RELEASE_COMMIT = "0929ef6b467c4ffd64ca5ce685ed5a20e468923e"


def test_published_release_signature_verifies_offline_to_its_commit() -> None:
    assert verify_release_checksums(CHECKSUMS, BUNDLE) == RELEASE_COMMIT
    digests = parse_release_checksums(CHECKSUMS)
    assert "catalog-index.json" in digests
    assert "qualification-index.json" in digests


def test_signature_does_not_cover_modified_checksums() -> None:
    changed = CHECKSUMS.replace(b"catalog-index.json", b"catalog-indey.json", 1)
    with pytest.raises(Exception):  # noqa: B017 -- no verified identity for tampered ingress
        verify_release_checksums(changed, BUNDLE)
    assert verify_release_checksums(CHECKSUMS, BUNDLE) == RELEASE_COMMIT


@pytest.mark.parametrize(
    ("constant", "value"),
    [
        ("RELEASE_REPOSITORY_ID", "1"),
        (
            "RELEASE_SIGNER_IDENTITY",
            (
                "https://github.com/CarstVaartjes/vonk-forge-recipes/.github/"
                "workflows/validate.yml@refs/heads/main"
            ),
        ),
        ("RELEASE_WORKFLOW_REF", "refs/heads/release"),
    ],
)
def test_a_valid_signature_from_another_publisher_is_refused(
    monkeypatch: pytest.MonkeyPatch, constant: str, value: str
) -> None:
    # A genuine Sigstore signature is not enough: it must come from the pinned
    # repository's publish workflow on main.
    with monkeypatch.context() as wrong_authority:
        wrong_authority.setattr(recipe_release, constant, value)
        with pytest.raises(Exception):  # noqa: B017 -- wrong publisher produces no verified identity
            verify_release_checksums(CHECKSUMS, BUNDLE)
    assert verify_release_checksums(CHECKSUMS, BUNDLE) == RELEASE_COMMIT


def test_malformed_bundle_is_refused() -> None:
    with pytest.raises(Exception):  # noqa: B017 -- malformed ingress produces no verified identity
        verify_release_checksums(CHECKSUMS, b'{"mediaType": "not-a-bundle"}')
    assert verify_release_checksums(CHECKSUMS, BUNDLE) == RELEASE_COMMIT


@pytest.mark.parametrize(
    "checksums",
    [
        b"",
        b"0" * 64 + b"  catalog-index.json",
        b"0" * 64 + b" catalog-index.json\n",
        b"0" * 64 + b"  z.tar.gz\n" + b"0" * 64 + b"  catalog-index.json\n",
        b"0" * 64 + b"  catalog-index.json\n" + b"0" * 64 + b"  catalog-index.json\n",
        b"0" * 64 + b"  ../catalog-index.json\n",
        b"0" * 64 + b"  a.tar.gz\n",
    ],
)
def test_only_the_canonical_checksum_manifest_parses(checksums: bytes) -> None:
    with pytest.raises(RecipeReleaseError):
        parse_release_checksums(checksums)
