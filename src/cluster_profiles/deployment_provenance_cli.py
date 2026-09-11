"""Human-readable output for the Controller provenance projection."""

from collections.abc import Mapping


class DeploymentProvenanceError(ValueError):
    """The deployment provenance payload is not the projected shape."""


def _object(value: object, label: str) -> Mapping[str, object]:
    """Return decoded JSON as an object or fail loudly."""

    if not isinstance(value, Mapping):
        raise DeploymentProvenanceError(
            f"deployment provenance {label} must be an object"
        )
    return value


def _object_list(value: object, label: str) -> list[Mapping[str, object]]:
    """Return decoded JSON as an array of objects or fail loudly."""

    if not isinstance(value, list):
        raise DeploymentProvenanceError(
            f"deployment provenance {label} must be an array"
        )
    return [_object(item, label) for item in value]


def render_deployment_provenance(payload: Mapping[str, object]) -> str:
    """Keep the exact JSON command and compact human view on one API result."""
    lines = ["Deployment provenance"]
    for boundary in _object_list(payload.get("platform"), "platform"):
        lines.append(
            f"{boundary['boundary']}: {boundary['state']} · {boundary.get('source_commit') or 'commit unknown'} · {boundary.get('image_digest') or 'image unknown'}"
        )
    for agent in _object_list(payload.get("agents"), "agents"):
        evidence = _object(agent.get("evidence"), "agent evidence")
        age = (
            f"{evidence['age_seconds']}s old"
            if evidence["age_seconds"] is not None
            else "age unknown"
        )
        lines.append(
            f"Spark {agent['display_name']}: {agent['connectivity']} · agent {agent.get('semantic_version') or 'unknown'} · binary {agent.get('binary_sha256') or 'unknown'} · {age}"
        )
    for workload in _object_list(payload.get("workloads"), "workloads"):
        acceptance = _object(
            workload.get("physical_acceptance"), "workload physical acceptance"
        )
        lines.append(
            f"{workload['recipe_publisher']}/{workload['recipe_slug']} · installation {workload['installation_id']} ({workload['installation_state']}) · run {workload.get('run_id') or 'none'} ({workload.get('run_state') or 'not loaded'})"
        )
        lines.append(
            f"  recipe {workload['recipe_content_sha256']} · image {workload['image_digest']} · physical acceptance: {acceptance['state']}"
        )
        for rank in _object_list(workload.get("ranks"), "workload ranks"):
            runtime_evidence = _object(
                rank.get("runtime_evidence"), "rank runtime evidence"
            )
            lines.append(
                f"  rank {rank['rank']} {rank['node_id']}: {rank['runtime_state']} · identity {rank['identity_agreement']} · generation {rank.get('observed_run_generation') or 'unknown'} · {runtime_evidence['freshness']}"
            )
    return "\n".join(lines)
