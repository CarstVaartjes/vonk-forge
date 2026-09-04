from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from vonk_control.distribution import DistributionService, MemoryVerifiedObjectSource
from vonk_agent_protocol import DistributionAssignment

from test_agent_api import NODE_A, NODE_B, agent_headers


def _assignment(
    node_id: str, model_digest: str, config_digest: str, archive_digest: str
) -> DistributionAssignment:
    return DistributionAssignment.parse(
        {
            "schema_version": 2,
            "assignment_id": str(uuid4()),
            "plan_digest": "a" * 64,
            "generation": 1,
            "node_id": node_id,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "model_artifact_set_sha256": "b" * 64,
            "objects": [
                {"name": "weights/model.bin", "sha256": model_digest, "bytes": 13, "kind": "model"},
                {"name": "config/tokenizer.json", "sha256": config_digest, "bytes": 7, "kind": "model"},
                {"name": "image.oci.tar", "sha256": archive_digest, "bytes": 11, "kind": "oci-archive"},
            ],
            "oci_image_digest": "sha256:" + "d" * 64,
            "oci_archive_sha256": archive_digest,
        }
    )


def test_controller_serves_one_verified_assignment_to_two_nodes(agent_system) -> None:
    client, services, _, clock = agent_system
    source = MemoryVerifiedObjectSource()
    model_digest = source.put(b"model payload")
    config_digest = source.put(b"config!")
    archive_digest = source.put(b"oci archive")
    service = DistributionService(source, clock=clock)
    assignment = _assignment(NODE_A, model_digest, config_digest, archive_digest)
    service.register(assignment)
    service.register(
        DistributionAssignment.parse(
            assignment.to_mapping() | {"assignment_id": str(uuid4()), "node_id": NODE_B}
        )
    )
    object.__setattr__(services, "distribution", service)

    manifest = client.get(
        "/agent/v1/distribution/manifests/" + "a" * 64,
        headers=agent_headers(NODE_A, "serial-a"),
    )
    assert manifest.status_code == 200
    assert {item["sha256"] for item in manifest.json()["objects"]} == {
        model_digest,
        config_digest,
        archive_digest,
    }
    response = client.get(
        "/agent/v1/distribution/objects/" + model_digest + "?plan_digest=" + "a" * 64,
        headers={**agent_headers(NODE_A, "serial-a"), "Range": "bytes=2-7"},
    )
    assert response.status_code == 206
    assert response.content == b"del pa"
    assert response.headers["etag"] == f'"sha256:{model_digest}"'
    assert response.headers["content-range"] == "bytes 2-7/13"

    second = client.get(
        "/agent/v1/distribution/objects/" + model_digest + "?plan_digest=" + "a" * 64,
        headers=agent_headers(NODE_B, "serial-b"),
    )
    assert second.status_code == 200 and second.content == b"model payload"


def test_distribution_rejects_unassigned_wrong_node_and_corrupt_object(agent_system) -> None:
    client, services, _, clock = agent_system
    source = MemoryVerifiedObjectSource()
    model_digest = source.put(b"model payload")
    archive_digest = source.put(b"oci archive")
    service = DistributionService(source, clock=clock)
    service.register(_assignment(NODE_A, model_digest, source.put(b"config!"), archive_digest))
    object.__setattr__(services, "distribution", service)
    path = "/agent/v1/distribution/objects/" + model_digest
    assert client.get(path + "?plan_digest=" + "a" * 64, headers=agent_headers(NODE_B, "serial-b")).status_code == 403
    assert client.get(path + "?plan_digest=" + "e" * 64, headers=agent_headers(NODE_A, "serial-a")).status_code == 403
    source.objects[model_digest] = b"tampered payload"
    assert client.get(path + "?plan_digest=" + "a" * 64, headers=agent_headers(NODE_A, "serial-a")).status_code == 503
