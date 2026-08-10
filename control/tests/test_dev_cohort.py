from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from vonk_control.dev_cohort import (
    DEVELOPMENT_IMAGE_IDENTITY_PATH,
    DEVELOPMENT_SOURCE_REPOSITORY,
    DevelopmentCohort,
    DevelopmentCohortError,
    DevelopmentImageIdentity,
    SelectedDevelopmentCohort,
    build_identity,
    canonical_json,
    read_identity,
    verify_cohort,
)

COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER_COMMIT = "89abcdef0123456789abcdef0123456789abcdef"
SCHEMA_VERSION = 1


def _canonical_for_expected(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("ascii")


def _expected_build_digest(source_commit: str = COMMIT) -> str:
    common = {
        "channel": "development",
        "database_revision": "0020_recipe_catalog_bridge",
        "platform_version": "0.1.0",
        "protocol_maximum": 2,
        "protocol_minimum": 1,
        "schema_version": SCHEMA_VERSION,
        "source_commit": source_commit,
        "source_repository": DEVELOPMENT_SOURCE_REPOSITORY,
    }
    return "sha256:" + hashlib.sha256(_canonical_for_expected(common)).hexdigest()


def _identity_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "source_repository": DEVELOPMENT_SOURCE_REPOSITORY,
        "source_commit": COMMIT,
        "channel": "development",
        "platform_version": "0.1.0",
        "build_digest": _expected_build_digest(),
        "database_revision": "0020_recipe_catalog_bridge",
        "protocol_minimum": 1,
        "protocol_maximum": 2,
        "image_role": "api",
    }
    document.update(overrides)
    return document


def _identity_bytes(**overrides: object) -> bytes:
    return canonical_json(_identity_document(**overrides))


def test_identity_parser_accepts_only_the_canonical_development_identity() -> None:
    raw = _identity_bytes()

    identity = DevelopmentImageIdentity.from_bytes(raw, expected_role="api")

    assert identity == DevelopmentImageIdentity(
        schema_version=SCHEMA_VERSION,
        source_repository=DEVELOPMENT_SOURCE_REPOSITORY,
        source_commit=COMMIT,
        channel="development",
        platform_version="0.1.0",
        build_digest=_expected_build_digest(),
        database_revision="0020_recipe_catalog_bridge",
        protocol_minimum=1,
        protocol_maximum=2,
        image_role="api",
    )
    assert identity.to_bytes() == raw


@pytest.mark.parametrize(
    ("label", "raw"),
    (
        ("malformed", b"{"),
        ("not an object", b"[]\n"),
        ("duplicate field", b'{"build_digest":"sha256:'
        + b"a" * 64
        + b'","build_digest":"sha256:'
        + b"b" * 64
        + b'","channel":"development","database_revision":"0020_recipe_catalog_bridge",'
        + b'"image_role":"api","platform_version":"0.1.0","protocol_maximum":2,'
        + b'"protocol_minimum":1,"schema_version":1,"source_commit":"'
        + COMMIT.encode("ascii")
        + b'","source_repository":"'
        + DEVELOPMENT_SOURCE_REPOSITORY.encode("ascii")
        + b'"}\n'),
        ("non canonical whitespace", json.dumps(_identity_document(), sort_keys=True).encode("ascii")),
        ("non canonical key order", b'{"schema_version":1,"source_repository":"'
        + DEVELOPMENT_SOURCE_REPOSITORY.encode("ascii")
        + b'","source_commit":"'
        + COMMIT.encode("ascii")
        + b'","channel":"development","platform_version":"0.1.0","build_digest":"sha256:'
        + b"a" * 64
        + b'","database_revision":"0020_recipe_catalog_bridge","protocol_minimum":1,'
        + b'"protocol_maximum":2,"image_role":"api"}\n'),
        ("oversized", b" " * 17000),
    ),
)
def test_identity_parser_rejects_untrusted_json_encodings(
    label: str, raw: bytes
) -> None:
    with pytest.raises(DevelopmentCohortError, match="identity"):
        DevelopmentImageIdentity.from_bytes(raw, expected_role="api")


@pytest.mark.parametrize(
    "overrides",
    (
        {"unknown": "value"},
        {"source_repository": "https://github.com/CarstVaartjes/vonk-forge.git"},
        {"source_repository": "https://github.com/evil/vonk-forge"},
        {"source_commit": COMMIT.upper()},
        {"source_commit": "1234"},
        {"channel": "latest"},
        {"platform_version": "v0.1.0"},
        {"platform_version": "0.1.1"},
        {"build_digest": "sha256:" + "A" * 64},
        {"build_digest": "not-a-digest"},
        {"build_digest": "sha256:" + "a" * 64},
        {"database_revision": ""},
        {"database_revision": "0021_next_revision"},
        {"protocol_minimum": 0},
        {"protocol_minimum": 3, "protocol_maximum": 2},
        {"protocol_minimum": 1, "protocol_maximum": 3},
        {"protocol_maximum": 1000},
        {"image_role": "scheduler"},
    ),
)
def test_identity_parser_rejects_values_that_break_the_development_trust_boundary(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(DevelopmentCohortError):
        DevelopmentImageIdentity.from_bytes(_identity_bytes(**overrides), expected_role="api")


def test_identity_parser_rejects_missing_required_fields() -> None:
    document = _identity_document()
    del document["source_commit"]

    with pytest.raises(DevelopmentCohortError, match="missing"):
        DevelopmentImageIdentity.from_bytes(canonical_json(document), expected_role="api")


def test_identity_parser_binds_the_expected_role_to_the_reader() -> None:
    with pytest.raises(DevelopmentCohortError, match="role"):
        DevelopmentImageIdentity.from_bytes(_identity_bytes(image_role="worker"), expected_role="api")


def test_build_identity_uses_fixed_development_values_and_one_cohort_digest() -> None:
    api = build_identity(role="api", source_commit=COMMIT)
    worker = build_identity(role="worker", source_commit=COMMIT)

    assert api.source_repository == DEVELOPMENT_SOURCE_REPOSITORY
    assert api.source_commit == COMMIT
    assert api.channel == "development"
    assert api.platform_version == "0.1.0"
    assert api.database_revision == "0020_recipe_catalog_bridge"
    assert api.protocol_minimum == 1
    assert api.protocol_maximum == 2
    assert api.image_role == "api"
    assert worker.image_role == "worker"
    assert api.build_digest == worker.build_digest
    assert api.to_bytes() == canonical_json(json.loads(api.to_bytes()))
    assert worker.to_bytes() == canonical_json(json.loads(worker.to_bytes()))


def test_read_identity_uses_the_fixed_embedded_path_and_rejects_symlinked_input(
    tmp_path: Path,
) -> None:
    assert DEVELOPMENT_IMAGE_IDENTITY_PATH == Path(
        "/usr/local/share/vonk-forge/development-image-identity.json"
    )
    target = tmp_path / "identity.json"
    target.write_bytes(_identity_bytes())
    symlink = tmp_path / "identity-link.json"
    symlink.symlink_to(target)

    assert read_identity(target, expected_role="api").source_commit == COMMIT
    with pytest.raises(DevelopmentCohortError, match="unsafe"):
        read_identity(symlink, expected_role="api")


def test_identity_model_cannot_be_mutated_into_an_invalid_public_identity() -> None:
    identity = DevelopmentImageIdentity.from_bytes(_identity_bytes(), expected_role="api")

    with pytest.raises(DevelopmentCohortError):
        replace(identity, source_commit=OTHER_COMMIT.upper())


def _unsafe_identity(identity: DevelopmentImageIdentity, **overrides: object) -> DevelopmentImageIdentity:
    clone = object.__new__(DevelopmentImageIdentity)
    values = identity.to_document()
    values.update(overrides)
    for key, value in values.items():
        object.__setattr__(clone, key, value)
    return clone


def test_verify_cohort_rejects_duplicate_or_missing_development_roles() -> None:
    api = build_identity(role="api", source_commit=COMMIT)
    worker = build_identity(role="worker", source_commit=COMMIT)

    with pytest.raises(DevelopmentCohortError, match="roles"):
        verify_cohort([api])
    with pytest.raises(DevelopmentCohortError, match="roles"):
        verify_cohort([api, api])
    with pytest.raises(DevelopmentCohortError, match="roles"):
        verify_cohort([api, worker, worker])


@pytest.mark.parametrize(
    "overrides",
    (
        {"source_commit": OTHER_COMMIT},
        {"build_digest": "sha256:" + "f" * 64},
        {"platform_version": "0.1.1"},
        {"database_revision": "0021_next_revision"},
        {"protocol_minimum": 2},
        {"protocol_maximum": 3},
        {"source_repository": "https://github.com/CarstVaartjes/vonk-forge-local"},
        {"channel": "development-local"},
    ),
)
def test_verify_cohort_rejects_any_mismatch_in_common_identity_metadata(
    overrides: dict[str, object],
) -> None:
    api = build_identity(role="api", source_commit=COMMIT)
    worker = _unsafe_identity(build_identity(role="worker", source_commit=COMMIT), **overrides)

    with pytest.raises(DevelopmentCohortError, match="cohort"):
        verify_cohort([api, worker])


def test_matching_identities_produce_one_canonical_selected_cohort_document() -> None:
    api = build_identity(role="api", source_commit=COMMIT)
    worker = build_identity(role="worker", source_commit=COMMIT)
    expected_api_identity_digest = "sha256:" + hashlib.sha256(api.to_bytes()).hexdigest()
    expected_worker_identity_digest = "sha256:" + hashlib.sha256(worker.to_bytes()).hexdigest()
    expected_common = {
        "build_digest": api.build_digest,
        "channel": "development",
        "database_revision": "0020_recipe_catalog_bridge",
        "platform_version": "0.1.0",
        "protocol_maximum": 2,
        "protocol_minimum": 1,
        "schema_version": SCHEMA_VERSION,
        "source_commit": COMMIT,
        "source_repository": DEVELOPMENT_SOURCE_REPOSITORY,
    }
    expected_release_digest = "sha256:" + hashlib.sha256(
        (
            json.dumps(expected_common, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("ascii")
    ).hexdigest()
    expected_generation_seed = {
        "api_identity_digest": expected_api_identity_digest,
        "common": expected_common,
        "worker_identity_digest": expected_worker_identity_digest,
    }
    expected_generation_hash = hashlib.sha256(
        (
            json.dumps(expected_generation_seed, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("ascii")
    ).hexdigest()

    selected = verify_cohort([worker, api])

    assert selected == SelectedDevelopmentCohort(
        schema_version=SCHEMA_VERSION,
        source_repository=DEVELOPMENT_SOURCE_REPOSITORY,
        source_commit=COMMIT,
        channel="development",
        platform_version="0.1.0",
        database_revision="0020_recipe_catalog_bridge",
        protocol_minimum=1,
        protocol_maximum=2,
        build_digest=api.build_digest,
        release_digest=expected_release_digest,
        api_identity_digest=expected_api_identity_digest,
        worker_identity_digest=expected_worker_identity_digest,
        api_image=f"development.invalid/vonk-forge-api@{expected_api_identity_digest}",
        worker_image=f"development.invalid/vonk-forge-worker@{expected_worker_identity_digest}",
        generation_id="dev-" + expected_generation_hash[:24],
        start_nonce=hashlib.sha256(
            f"vonk-forge:development-start:{expected_generation_hash}".encode("ascii")
        ).hexdigest(),
    )
    assert selected.to_document() == {
        **expected_common,
        "release_digest": expected_release_digest,
        "api_identity_digest": expected_api_identity_digest,
        "worker_identity_digest": expected_worker_identity_digest,
        "api_image": f"development.invalid/vonk-forge-api@{expected_api_identity_digest}",
        "worker_image": f"development.invalid/vonk-forge-worker@{expected_worker_identity_digest}",
        "generation_id": "dev-" + expected_generation_hash[:24],
        "start_nonce": hashlib.sha256(
            f"vonk-forge:development-start:{expected_generation_hash}".encode("ascii")
        ).hexdigest(),
    }
    assert selected.to_bytes() == canonical_json(selected.to_document())
    assert DevelopmentCohort(api=api, worker=worker).common_document() == expected_common


def test_selected_cohort_round_trips_and_rejects_tampered_canonical_documents() -> None:
    selected = verify_cohort(
        [
            build_identity(role="api", source_commit=COMMIT),
            build_identity(role="worker", source_commit=COMMIT),
        ]
    )
    document = selected.to_document()
    tampered = dict(document, source_commit=OTHER_COMMIT)

    assert SelectedDevelopmentCohort.from_bytes(selected.to_bytes()) == selected
    with pytest.raises(DevelopmentCohortError, match="selected"):
        SelectedDevelopmentCohort.from_bytes(canonical_json(tampered))
    with pytest.raises(DevelopmentCohortError, match="canonical"):
        SelectedDevelopmentCohort.from_bytes(
            json.dumps(document, sort_keys=True).encode("ascii")
        )
