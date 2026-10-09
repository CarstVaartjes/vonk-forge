"""Install admission: identity."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from vonk_agent_protocol import (
    InstallAdmissionCode,
    WaitReason,
)
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledExecutionPlan as WireCompiledExecutionPlan,
)

from ..content_identity import same_image
from ..models import (
    RecipeBuild,
)
from .contracts import (
    InstallEvidenceChanged,
    InstallNodePlan,
    StoredInstallIdentityDamaged,
)


def _primary_model_sha256(document: Mapping[str, object]) -> str:
    selections = document.get("models")
    model_selection = (
        selections[0]
        if isinstance(selections, Sequence)
        and not isinstance(selections, (str, bytes))
        and selections
        else None
    )
    model = (
        model_selection.get("model") if isinstance(model_selection, Mapping) else None
    )
    digest = model.get("content_sha256") if isinstance(model, Mapping) else None
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise InstallEvidenceChanged(
            InstallAdmissionCode.MODEL_IDENTITY_UNAVAILABLE,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    return digest


def _compiled_build_matches(
    payload: WireCompiledExecutionPlan, build: RecipeBuild, recipe_digest: str
) -> bool:
    return (
        payload.identity.recipe_revision_sha256 == recipe_digest
        and build.image_bytes is not None
        and same_image(payload.runtime_image, build)
    )


_INSTALL_NODE_DIGEST_FIELDS = (
    "node_id",
    "rank",
    "role",
    "reused_bytes",
    "required_download_bytes",
    "required_bytes",
    "disk_floor_bytes",
)


def _node_digest_document(
    node: InstallNodePlan | Mapping[str, object],
) -> dict[str, object]:
    """Bind install work and safety envelopes, not transient observations."""

    if isinstance(node, Mapping):
        if any(key not in node for key in _INSTALL_NODE_DIGEST_FIELDS):
            raise StoredInstallIdentityDamaged(
                "stored installation node identity is incomplete",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        return {key: node[key] for key in _INSTALL_NODE_DIGEST_FIELDS}
    return {key: getattr(node, key) for key in _INSTALL_NODE_DIGEST_FIELDS}


def _installation_plan_identity(
    *,
    mapping_id: str,
    mapping_generation: int,
    recipe_build_id: str | None,
    image_digest: str,
    recipe_revision_id: str,
    recipe_content_sha256: str,
    compiled_execution_plans: Mapping[str, object],
    nodes: Sequence[InstallNodePlan | Mapping[str, object]],
) -> dict[str, object]:
    """Return the sole canonical identity document used for install hashing."""

    return {
        "schema_version": 1,
        "mapping_id": mapping_id,
        "mapping_generation": mapping_generation,
        "recipe_build_id": recipe_build_id,
        "image_digest": image_digest,
        "recipe_revision_id": recipe_revision_id,
        "recipe_content_sha256": recipe_content_sha256,
        "compiled_execution_plans": dict(compiled_execution_plans),
        "nodes": [_node_digest_document(item) for item in nodes],
    }


def _installation_plan_digest(identity: Mapping[str, object]) -> str:
    """Preserve the exact install-admission JSON digest spelling."""

    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def installation_plan_digest_from_stored_document(value: object) -> str:
    """Recompute an installation's immutable identity without parsing launch data.

    Reconciliation needs to prove that the persisted opaque document still
    matches the original accepted digest.  The compiled plans are generic JSON
    values here; this path deliberately does not construct or validate a
    ``CompiledExecutionPlan``.
    """

    if not isinstance(value, Mapping):
        raise StoredInstallIdentityDamaged(
            "stored installation identity is invalid",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    nodes = value.get("nodes")
    compiled = value.get("compiled_execution_plans")
    if (
        value.get("schema_version") != 1
        or type(value.get("mapping_generation")) is not int
        or not isinstance(nodes, list)
        or not nodes
        or not isinstance(compiled, Mapping)
        or any(not isinstance(item, Mapping) for item in nodes)
        or any(not isinstance(item, Mapping) for item in compiled.values())
    ):
        raise StoredInstallIdentityDamaged(
            "stored installation identity is invalid",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    node_documents: list[dict[str, object]] = []
    for item in nodes:
        assert isinstance(item, Mapping)
        if (
            not isinstance(item.get("node_id"), str)
            or type(item.get("rank")) is not int
            or not isinstance(item.get("role"), str)
            or any(
                not _is_nonnegative_json_int(item.get(key))
                for key in (
                    "reused_bytes",
                    "required_download_bytes",
                    "required_bytes",
                    "disk_floor_bytes",
                )
            )
        ):
            raise StoredInstallIdentityDamaged(
                "stored installation node identity is invalid",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        node_documents.append(_node_digest_document(item))
    if len({item["node_id"] for item in node_documents}) != len(node_documents):
        raise StoredInstallIdentityDamaged(
            "stored installation node identities are duplicated",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    if set(compiled) != {item["node_id"] for item in node_documents}:
        raise StoredInstallIdentityDamaged(
            "stored compiled plans do not match installation nodes",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    identity = _installation_plan_identity(
        mapping_id=value["mapping_id"],
        mapping_generation=value["mapping_generation"],
        recipe_build_id=value.get("recipe_build_id"),
        image_digest=value["image_digest"],
        recipe_revision_id=value["recipe_revision_id"],
        recipe_content_sha256=value["recipe_content_sha256"],
        compiled_execution_plans=compiled,
        nodes=nodes,
    )
    return _installation_plan_digest(identity)


def _is_nonnegative_json_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
