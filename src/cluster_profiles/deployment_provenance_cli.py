"""Human-readable output for the Controller provenance projection."""

from collections.abc import Mapping


def render_deployment_provenance(payload: Mapping[str, object]) -> str:
    """Keep the exact JSON command and compact human view on one API result."""
    lines = ["Deployment provenance"]
    for boundary in payload["platform"]:
        lines.append(
            f"{boundary['boundary']}: {boundary['state']} · {boundary.get('source_commit') or 'commit unknown'} · {boundary.get('image_digest') or 'image unknown'}"
        )
    for agent in payload["agents"]:
        evidence = agent["evidence"]
        age = (
            f"{evidence['age_seconds']}s old"
            if evidence["age_seconds"] is not None
            else "age unknown"
        )
        lines.append(
            f"Spark {agent['display_name']}: {agent['connectivity']} · agent {agent.get('semantic_version') or 'unknown'} · binary {agent.get('binary_sha256') or 'unknown'} · {age}"
        )
    for workload in payload["workloads"]:
        lines.append(
            f"{workload['recipe_publisher']}/{workload['recipe_slug']} · installation {workload['installation_id']} ({workload['installation_state']}) · run {workload.get('run_id') or 'none'} ({workload.get('run_state') or 'not loaded'})"
        )
        lines.append(
            f"  recipe {workload['recipe_content_sha256']} · image {workload['image_digest']} · physical acceptance: {workload['physical_acceptance']['state']}"
        )
        for rank in workload["ranks"]:
            lines.append(
                f"  rank {rank['rank']} {rank['node_id']}: {rank['runtime_state']} · identity {rank['identity_agreement']} · generation {rank.get('observed_run_generation') or 'unknown'} · {rank['runtime_evidence']['freshness']}"
            )
    return "\n".join(lines)
