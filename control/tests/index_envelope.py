"""The signed index's envelope, built from the contract's own models."""

from __future__ import annotations

from vonk_forge_contracts import (
    INDEX_KIND,
    INDEX_SCHEMA_VERSION,
    PACKAGE_MEDIA_TYPE,
    PACKAGE_METADATA_FIELDS,
    PACKAGE_PATH_PREFIX,
    PACKAGE_SCHEMA_VERSION,
    PackageContract,
    catalog_vocabulary,
)

REPOSITORY = "CarstVaartjes/vonk-forge-recipes"


def index_envelope(
    *,
    contract_version: str = "4.0.0",
    updated_at: str = "2026-09-28T12:00:00Z",
    source_commit: str = "a" * 40,
    catalog_entities: list[object] | None = None,
    recipes: list[object] | None = None,
) -> dict[str, object]:
    """A ``catalog-index.json`` mapping whose envelope the contract accepts."""

    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "kind": INDEX_KIND,
        "contract_version": contract_version,
        "updated_at": updated_at,
        "repository": REPOSITORY,
        "source_commit": source_commit,
        "package_contract": PackageContract(
            schema_version=PACKAGE_SCHEMA_VERSION,
            media_type=PACKAGE_MEDIA_TYPE,
            path_prefix=PACKAGE_PATH_PREFIX,
            metadata_fields=PACKAGE_METADATA_FIELDS,
        ).model_dump(mode="json"),
        "vocabulary": catalog_vocabulary().model_dump(mode="json"),
        "catalog_entities": catalog_entities or [],
        "recipes": recipes or [],
    }
