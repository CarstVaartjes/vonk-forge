from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from importlib import resources


def _recipe(
    slug: str,
    *,
    nodes: int = 1,
    recipe_id: str | None = None,
    revision_id: str | None = None,
) -> dict[str, object]:
    """Return a complete canonical recipe document for Library fixtures."""
    document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "recipe-image.json")
        .read_text(encoding="utf-8")
    )
    document["identity"]["publisher"] = "vonk"  # type: ignore[index]
    document["identity"]["slug"] = slug  # type: ignore[index]
    topology = document["topology"]
    topology["node_count"] = nodes  # type: ignore[index]
    topology["name"] = "solo" if nodes == 1 else "dual"  # type: ignore[index]
    topology["mode"] = "single" if nodes == 1 else "distributed"  # type: ignore[index]
    roles = topology["roles"]  # type: ignore[index]
    roles[0]["count"] = nodes  # type: ignore[index]
    if recipe_id is not None:
        document["__recipe_id"] = recipe_id
    if revision_id is not None:
        document["__revision_id"] = revision_id
    return document


def _library_detail(
    recipe: Mapping[str, object],
    *,
    recipe_id: str | None = None,
    revision_id: str | None = None,
    **overrides: object,
) -> dict[str, object]:
    """Build the current generated Library list/detail transport shape."""
    from cluster_profiles.generated_control.models.recipe_definition import (
        RecipeDefinition,
    )

    definition = RecipeDefinition.from_dict(recipe)
    canonical = definition.to_dict()
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    identity = definition.identity
    rid = recipe_id or str(recipe.get("__recipe_id") or uuid.uuid5(uuid.NAMESPACE_URL, f"vonk:{identity.publisher}/{identity.slug}"))
    rev = revision_id or str(recipe.get("__revision_id") or uuid.uuid5(uuid.NAMESPACE_URL, f"vonk:{rid}:revision"))
    model_documents = []
    for selection in canonical["models"]:
        model = json.loads(
            resources.files("vonk_forge_contracts")
            .joinpath("examples", "model-definition.json")
            .read_text(encoding="utf-8")
        )
        selected = selection["model"]
        model["identity"]["publisher"] = selected["publisher"]
        model["identity"]["slug"] = selected["slug"]
        model["identity"]["model"]["publisher"] = selected["publisher"]
        model["identity"]["model"]["slug"] = selected["slug"]
        model_documents.append({"selection": selection, "model_document": model})
    summary = {
        "capabilities": [],
        "content_sha256": digest,
        "description": identity.slug,
        "installation_returned_count": 0,
        "installation_total_count": 0,
        "installations": [],
        "installations_truncated": False,
        "publisher": identity.publisher,
        "reasons": [],
        "recipe_document": canonical,
        "recipe_id": rid,
        "recipe_revision_id": rev,
        "run_returned_count": 0,
        "run_total_count": 0,
        "runs": [],
        "runs_truncated": False,
        "slug": identity.slug,
        "title": identity.slug,
        "topology_name": definition.topology.name,
    }
    detail = {
        "schema_version": 2,
        "generated_at": "2026-09-07T00:00:00Z",
        "recipe": {
            "content_sha256": digest,
            "description": identity.slug,
            "publisher": identity.publisher,
            "recipe_id": rid,
            "recipe_revision_id": rev,
            "slug": identity.slug,
            "title": identity.slug,
        },
        "definition": canonical,
        "topology": canonical["topology"],
        "operational_state": {"builds": [], "mappings": [], "installations": [], "runs": []},
        "placement": [],
        "reasons": [],
        "model_documents": model_documents,
    }
    detail.update(overrides)
    return {"summary": summary, "detail": detail}


def _recipe_digest(slug: str = "tiny") -> str:
    return str(_library_detail(_recipe(slug))["detail"]["recipe"]["content_sha256"])
