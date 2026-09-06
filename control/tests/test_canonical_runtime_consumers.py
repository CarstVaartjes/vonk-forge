"""Focused fail-closed checks for canonical runtime consumers."""

from __future__ import annotations

import json
from importlib.resources import files
from types import SimpleNamespace

import pytest

contracts = pytest.importorskip("vonk_forge_contracts")
from vonk_control.agent_api import _runtime_image_receipt_matches
from vonk_control.artifact_jobs import _active_recipe_revision
from vonk_control.fleet_projection import _canonical_recipe
from vonk_forge_contracts import content_sha256


def _document() -> dict[str, object]:
    return json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-image.json")
        .read_text(encoding="utf-8")
    )


class _Session:
    def __init__(self, revision: object | None) -> None:
        self.revision = revision

    def get(self, _model: object, _revision_id: str) -> object | None:
        return self.revision


def _revision(*, state: str = "active", digest: str | None = None) -> object:
    document = _document()
    recipe = contracts.RecipeDefinition.model_validate(document)
    return SimpleNamespace(
        id="revision",
        kind="recipe",
        schema_version=2,
        state=state,
        document=document,
        content_digest=digest or content_sha256(recipe),
    )


def test_active_canonical_revision_is_consumed() -> None:
    revision = _revision()
    resolved = _active_recipe_revision(_Session(revision), "revision")
    assert resolved is not None
    assert resolved[0] is revision
    assert resolved[1].identity.slug == contracts.RecipeDefinition.model_validate(
        _document()
    ).identity.slug
    assert _canonical_recipe(revision) is not None


@pytest.mark.parametrize(
    "revision",
    [
        None,
        _revision(state="candidate"),
        _revision(digest="0" * 64),
    ],
)
def test_missing_or_stale_revision_fails_closed(revision: object | None) -> None:
    assert _active_recipe_revision(_Session(revision), "revision") is None
    assert revision is None or _canonical_recipe(revision) is None


def _runtime_receipt_fixture(
    *, source: str = "published", build_id: str | None = None
) -> tuple[dict[str, object], dict[str, object], object]:
    revision_id = "revision"
    revision_digest = "a" * 64
    platform_digest = "sha256:" + "b" * 64
    receipt = SimpleNamespace(
        state="verified",
        recipe_revision_id=revision_id,
        original_content_digest=revision_digest,
        effective_execution_key="c" * 64,
        source=source,
        registry_manifest_digest=("sha256:" + "d" * 64 if source == "published" else None),
        platform_manifest_digest=platform_digest,
        local_image_config_id="sha256:" + "e" * 64,
        oci_archive_sha256="f" * 64,
        image_bytes=4096,
        architecture="linux-arm64",
        runtime_interface="vonk.runtime.v1",
        runtime_interface_label="v1",
        build_id=build_id,
    )
    identity = {
        "recipe_revision_sha256": revision_digest,
        "execution_sha256": receipt.effective_execution_key,
    }
    runtime_image = {
        "source": source,
        "build_id": build_id,
        "registry_manifest_digest": receipt.registry_manifest_digest,
        "platform_manifest_digest": platform_digest,
        "local_image_config_id": receipt.local_image_config_id,
        "image_digest": platform_digest,
        "oci_layout_sha256": receipt.oci_archive_sha256,
        "image_bytes": receipt.image_bytes,
        "architecture": receipt.architecture,
        "runtime_interface": receipt.runtime_interface,
        "runtime_interface_label": receipt.runtime_interface_label,
    }
    return runtime_image, identity, receipt


def test_persisted_published_image_binds_without_a_recipe_build() -> None:
    runtime_image, identity, receipt = _runtime_receipt_fixture()

    assert _runtime_image_receipt_matches(
        runtime_image,
        identity,
        receipt,
        revision_id="revision",
        revision_digest="a" * 64,
        installation_image_digest=runtime_image["image_digest"],
    )


def test_persisted_controller_build_image_requires_matching_build_receipt() -> None:
    runtime_image, identity, receipt = _runtime_receipt_fixture(
        source="controller-build", build_id="build"
    )

    assert _runtime_image_receipt_matches(
        runtime_image,
        identity,
        receipt,
        revision_id="revision",
        revision_digest="a" * 64,
        installation_image_digest=runtime_image["image_digest"],
    )

    receipt.local_image_config_id = "sha256:" + "0" * 64
    assert not _runtime_image_receipt_matches(
        runtime_image,
        identity,
        receipt,
        revision_id="revision",
        revision_digest="a" * 64,
        installation_image_digest=runtime_image["image_digest"],
    )

    receipt.local_image_config_id = runtime_image["local_image_config_id"]
    identity["recipe_revision_sha256"] = "0" * 64
    assert not _runtime_image_receipt_matches(
        runtime_image,
        identity,
        receipt,
        revision_id="revision",
        revision_digest="a" * 64,
        installation_image_digest=runtime_image["image_digest"],
    )
