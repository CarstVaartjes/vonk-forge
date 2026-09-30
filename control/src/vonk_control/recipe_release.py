"""Verify the signed checksum manifest of a recipe-library GitHub release.

The recipe repository's ``publish.yml`` builds every release asset from the
release commit, lists them in ``SHA256SUMS`` and attests that one file with a
keyless GitHub artifact attestation (a Sigstore bundle in the public-good
instance). The Controller trusts a release only after that bundle verifies
offline against the pinned Sigstore trust root and the pinned workflow
identity below; every other asset is then trusted solely through the digest
``SHA256SUMS`` lists for it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from functools import lru_cache
from importlib.resources import files

from sigstore.errors import Error as SigstoreError
from sigstore.models import Bundle, TrustedRoot
from sigstore.verify import Verifier, policy

from .recipe_library_types import RecipeLibraryError

RELEASE_CHECKSUMS = "SHA256SUMS"
RELEASE_BUNDLE = "SHA256SUMS.sigstore.json"
RELEASE_INDEX = "catalog-index.json"
# The one release asset the Controller downloads: an uncompressed tar holding
# SHA256SUMS, its Sigstore bundle and every file SHA256SUMS lists, under the
# same flat names. One asset makes an in-place release update atomic.
RELEASE_LIBRARY = "recipe-library.tar"
RELEASE_REPOSITORY_URI = "https://github.com/CarstVaartjes/vonk-forge-recipes"
# GitHub's numeric repository ID survives renames and is never reused, so a
# deleted-and-recreated repository with the same name cannot sign releases.
RELEASE_REPOSITORY_ID = "1336002555"
RELEASE_WORKFLOW_REF = "refs/heads/main"
RELEASE_SIGNER_IDENTITY = (
    f"{RELEASE_REPOSITORY_URI}/.github/workflows/publish.yml@{RELEASE_WORKFLOW_REF}"
)
RELEASE_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
MAX_CHECKSUMS_BYTES = 256 * 1024
MAX_BUNDLE_BYTES = 256 * 1024
_IN_TOTO_PAYLOAD_TYPE = "application/vnd.in-toto+json"
_IN_TOTO_STATEMENT = "https://in-toto.io/Statement/v1"
_SLSA_PROVENANCE = "https://slsa.dev/provenance/v1"
_SOURCE_DEPENDENCY = f"git+{RELEASE_REPOSITORY_URI}@{RELEASE_WORKFLOW_REF}"
_CHECKSUM_LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]{0,127})")
_SHA1 = re.compile(r"[0-9a-f]{40}")


class RecipeReleaseError(RecipeLibraryError):
    """A recipe release is unsigned, signed by the wrong identity, or malformed."""


@lru_cache(maxsize=1)
def _verifier() -> Verifier:
    # The trust root is a reviewed, packaged file: verification never
    # refreshes it over the network and never writes a local TUF cache.
    root = files("vonk_control.resources").joinpath("sigstore-trusted-root.json")
    return Verifier(trusted_root=TrustedRoot.from_file(str(root)))


def _invalid(detail: str) -> RecipeReleaseError:
    return RecipeReleaseError("recipe_release.signature_invalid", detail)


def _source_commit(statement: Mapping[str, object]) -> str:
    predicate = statement.get("predicate")
    build = predicate.get("buildDefinition") if isinstance(predicate, Mapping) else None
    dependencies = (
        build.get("resolvedDependencies") if isinstance(build, Mapping) else None
    )
    if not isinstance(dependencies, list):
        raise _invalid("release provenance does not name its source")
    commits = [
        dependency.get("digest", {}).get("gitCommit")
        for dependency in dependencies
        if isinstance(dependency, Mapping)
        and dependency.get("uri") == _SOURCE_DEPENDENCY
        and isinstance(dependency.get("digest"), Mapping)
    ]
    if len(commits) != 1 or not isinstance(commits[0], str):
        raise _invalid("release provenance does not name exactly one source commit")
    commit = commits[0]
    if _SHA1.fullmatch(commit) is None:
        raise _invalid("release provenance source commit is invalid")
    return commit


def verify_release_checksums(checksums: bytes, bundle: bytes) -> str:
    """Verify ``bundle`` signs ``checksums`` and return the attested commit.

    The certificate must come from ``publish.yml`` on ``refs/heads/main`` of
    the pinned repository, run on a GitHub-hosted runner, and its source
    repository digest must equal the commit the provenance names.
    """

    if len(checksums) > MAX_CHECKSUMS_BYTES or len(bundle) > MAX_BUNDLE_BYTES:
        raise _invalid("release signature material exceeds its size bound")
    try:
        parsed = Bundle.from_json(bundle)
    except (SigstoreError, ValueError, TypeError) as error:
        raise _invalid("release signature bundle is malformed") from error
    identity = policy.AllOf(
        [
            policy.Identity(
                identity=RELEASE_SIGNER_IDENTITY, issuer=RELEASE_OIDC_ISSUER
            ),
            policy.OIDCSourceRepositoryURI(RELEASE_REPOSITORY_URI),
            policy.OIDCSourceRepositoryIdentifier(RELEASE_REPOSITORY_ID),
            policy.OIDCSourceRepositoryRef(RELEASE_WORKFLOW_REF),
            policy.OIDCRunnerEnvironment("github-hosted"),
        ]
    )
    try:
        payload_type, payload = _verifier().verify_dsse(parsed, identity)
    except (SigstoreError, ValueError, TypeError) as error:
        raise _invalid(
            "release signature does not verify for the pinned publisher"
        ) from error
    if payload_type != _IN_TOTO_PAYLOAD_TYPE:
        raise _invalid("release signature does not carry an in-toto statement")
    try:
        statement = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _invalid("release attestation statement is malformed") from error
    if (
        not isinstance(statement, Mapping)
        or statement.get("_type") != _IN_TOTO_STATEMENT
        or statement.get("predicateType") != _SLSA_PROVENANCE
    ):
        raise _invalid("release attestation is not SLSA provenance")
    subjects = statement.get("subject")
    if subjects != [
        {
            "name": RELEASE_CHECKSUMS,
            "digest": {"sha256": hashlib.sha256(checksums).hexdigest()},
        }
    ]:
        raise _invalid("release attestation does not sign this SHA256SUMS")
    commit = _source_commit(statement)
    # The provenance is signed content, but the certificate extension is what
    # GitHub's OIDC token asserted; require both to name the same commit.
    try:
        _verifier().verify_dsse(parsed, policy.OIDCSourceRepositoryDigest(commit))
    except (SigstoreError, ValueError, TypeError) as error:
        raise _invalid(
            "release certificate and provenance name different commits"
        ) from error
    return commit


def parse_release_checksums(checksums: bytes) -> dict[str, str]:
    """Parse ``sha256sum`` output: one ``<digest>  <name>`` line per asset."""

    try:
        text = checksums.decode("ascii")
    except UnicodeDecodeError as error:
        raise _invalid("release SHA256SUMS is not ASCII") from error
    if not text.endswith("\n"):
        raise _invalid("release SHA256SUMS is not newline terminated")
    digests: dict[str, str] = {}
    for line in text[:-1].split("\n"):
        match = _CHECKSUM_LINE.fullmatch(line)
        if match is None:
            raise _invalid("release SHA256SUMS line is malformed")
        digest, name = match.groups()
        if name in digests or name in {RELEASE_CHECKSUMS, RELEASE_BUNDLE}:
            raise _invalid("release SHA256SUMS names an asset twice")
        digests[name] = digest
    if list(digests) != sorted(digests) or RELEASE_INDEX not in digests:
        raise _invalid("release SHA256SUMS is not the canonical asset manifest")
    return digests


__all__ = [
    "RELEASE_BUNDLE",
    "RELEASE_CHECKSUMS",
    "RELEASE_INDEX",
    "RELEASE_LIBRARY",
    "RELEASE_OIDC_ISSUER",
    "RELEASE_REPOSITORY_ID",
    "RELEASE_SIGNER_IDENTITY",
    "RecipeReleaseError",
    "parse_release_checksums",
    "verify_release_checksums",
]
