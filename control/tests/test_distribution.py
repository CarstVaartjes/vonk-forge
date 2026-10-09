from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from vonk_agent_protocol import (
    DistributionAssignment,
    DistributionCode,
    DistributionObject,
)
from vonk_control.distribution import (
    CompositeObjectSource,
    DistributionError,
    DistributionService,
    DistributionUnknown,
    FilesystemObjectSource,
    MemoryObjectSource,
    ModelCacheObjectSource,
)
from vonk_control.distribution_assignment import NodeDistributionAssignment

from .test_agent_api import NODE_A, NODE_B, agent_headers
from .test_agent_api import agent_system as _agent_system

agent_system = _agent_system


def _assignment(
    node_id: str, model_digest: str, config_digest: str, archive_digest: str
) -> NodeDistributionAssignment:
    return NodeDistributionAssignment.parse(
        {
            "assignment_id": str(uuid4()),
            "plan_digest": "a" * 64,
            "generation": 1,
            "node_id": node_id,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            # The fixture models the NAS cache's opaque manifest identity.
            "model_artifact_set_sha256": "b" * 64,
            "objects": [
                {
                    "name": "weights/model.bin",
                    "sha256": model_digest,
                    "bytes": 13,
                    "kind": "model",
                },
                {
                    "name": "config/tokenizer.json",
                    "sha256": config_digest,
                    "bytes": 7,
                    "kind": "model",
                },
            ],
            "oci_image_digest": "sha256:" + "d" * 64,
            "oci_image_config_digest": "sha256:" + "9" * 64,
            "oci_archive_sha256": archive_digest,
        }
    )


@pytest.mark.usefixtures("damaged_json_rows")
def test_controller_serves_one_verified_assignment_to_two_nodes(agent_system) -> None:
    client, services, _, clock = agent_system
    source = MemoryObjectSource()
    model_digest = source.put(b"model payload")
    config_digest = source.put(b"config!")
    archive_digest = source.put(b"oci archive")
    service = DistributionService(source, clock=clock)
    assignment = _assignment(NODE_A, model_digest, config_digest, archive_digest)
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(assignment.oci_image_digest, archive_digest)
    service.register(assignment)
    service.register(
        NodeDistributionAssignment.parse(
            assignment.to_mapping() | {"assignment_id": str(uuid4()), "node_id": NODE_B}
        )
    )
    object.__setattr__(services, "distribution", service)

    manifest = client.get(
        "/agent/distribution/manifests/" + "a" * 64,
        headers=agent_headers(NODE_A, "serial-a"),
    )
    assert manifest.status_code == 200
    assert (
        DistributionAssignment.model_validate_json(manifest.content)
        == assignment.wire()
    )
    assert manifest.headers["cache-control"] == "no-store"
    assert manifest.headers["etag"] == f'"plan:{assignment.plan_digest}"'
    response_schema = client.app.openapi()["paths"][
        "/agent/distribution/manifests/{plan_digest}"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_schema == {"$ref": "#/components/schemas/DistributionAssignment"}
    # The runtime image is pulled by digest, never served as an object.
    assert {item["sha256"] for item in manifest.json()["objects"]} == {
        model_digest,
        config_digest,
    }
    assert manifest.json()["oci_image_digest"] == assignment.oci_image_digest
    assert manifest.json()["oci_image_config_digest"] == "sha256:" + "9" * 64
    assert (
        client.get(
            "/agent/distribution/objects/"
            + archive_digest
            + "?plan_digest="
            + "a" * 64,
            headers=agent_headers(NODE_A, "serial-a"),
        ).status_code
        >= 400
    )
    response = client.get(
        "/agent/distribution/objects/" + model_digest + "?plan_digest=" + "a" * 64,
        headers={**agent_headers(NODE_A, "serial-a"), "Range": "bytes=2-7"},
    )
    # An authorized request is answered with the file the edge serves; the
    # range itself is answered by the edge from that file.
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["x-vonk-file"] == (
        (source.root / model_digest).relative_to("/").as_posix()
    )
    assert response.headers["etag"] == f'"sha256:{model_digest}"'
    assert (
        client.get(
            "/agent/distribution/objects/" + model_digest + "?plan_digest=" + "a" * 64,
            headers={
                **agent_headers(NODE_A, "serial-a"),
                "Range": "bytes=2-7",
                "If-Range": '"sha256:' + "0" * 64 + '"',
            },
        ).status_code
        == 412
    )
    assert (
        client.get(
            "/agent/distribution/objects/" + model_digest + "?plan_digest=" + "a" * 64,
            headers={**agent_headers(NODE_A, "serial-a"), "Range": "bytes=0-1,3-4"},
        ).status_code
        == 416
    )

    second = client.get(
        "/agent/distribution/objects/" + model_digest + "?plan_digest=" + "a" * 64,
        headers=agent_headers(NODE_B, "serial-b"),
    )
    assert second.status_code == 200 and "x-vonk-file" in second.headers


def test_distribution_rejects_unassigned_wrong_node_and_corrupt_object(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    source = MemoryObjectSource()
    model_digest = source.put(b"model payload")
    archive_digest = source.put(b"oci archive")
    service = DistributionService(source, clock=clock)
    assignment = _assignment(
        NODE_A, model_digest, source.put(b"config!"), archive_digest
    )
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(assignment.oci_image_digest, archive_digest)
    service.register(assignment)
    object.__setattr__(services, "distribution", service)
    path = "/agent/distribution/objects/" + model_digest
    wrong_node = client.get(
        path + "?plan_digest=" + "a" * 64, headers=agent_headers(NODE_B, "serial-b")
    )
    assert wrong_node.status_code == 403
    # The denial names the refusing check so the agent can report which
    # authorization boundary rejected it, not just the generic 403 category.
    assert wrong_node.headers["x-vonk-error-code"] == "distribution.wrong_node"
    unassigned = client.get(
        path + "?plan_digest=" + "e" * 64, headers=agent_headers(NODE_A, "serial-a")
    )
    assert unassigned.status_code == 403
    assert unassigned.headers["x-vonk-error-code"] == "distribution.unassigned"
    source.objects[model_digest] = b"tampered payload"
    unavailable = client.get(
        path + "?plan_digest=" + "a" * 64, headers=agent_headers(NODE_A, "serial-a")
    )
    assert unavailable.status_code != 403
    assert "retry-after" in unavailable.headers
    assert source.put(b"model payload") == model_digest
    recovered = client.get(
        path + "?plan_digest=" + "a" * 64, headers=agent_headers(NODE_A, "serial-a")
    )
    assert recovered.status_code == 200


def test_distribution_assignment_survives_controller_service_restart(
    agent_system,
) -> None:
    _client, _services, _tokens, clock = agent_system
    sessions = _services.sessions
    source = MemoryObjectSource()
    assignment = _assignment(
        NODE_A,
        source.put(b"model payload"),
        source.put(b"config!"),
        source.put(b"oci archive"),
    )
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(
        assignment.oci_image_digest, assignment.oci_archive_sha256
    )
    DistributionService(source, clock=clock, sessions=sessions).register(assignment)

    restarted = DistributionService(source, clock=clock, sessions=sessions)
    assert (
        restarted.authorize(
            node_id=NODE_A, plan_digest=assignment.plan_digest
        ).assignment_id
        == assignment.assignment_id
    )
    restarted.revoke(plan_digest=assignment.plan_digest, node_id=NODE_A)
    with pytest.raises(DistributionError):
        restarted.authorize(node_id=NODE_A, plan_digest=assignment.plan_digest)


def test_separate_api_process_serves_worker_registered_model_files(
    agent_system, tmp_path
) -> None:
    client, services, _, clock = agent_system
    image_source = MemoryObjectSource()
    model_digest = image_source.put(b"model payload")
    config_digest = image_source.put(b"config!")
    archive_digest = image_source.put(b"oci archive")
    assignment = _assignment(NODE_A, model_digest, config_digest, archive_digest)
    model_objects = tuple(item for item in assignment.objects if item.kind == "model")
    for item in model_objects:
        (tmp_path / item.sha256).write_bytes(image_source.objects[item.sha256])
        del image_source.objects[item.sha256]
    image_source.register_runtime_image(assignment.oci_image_digest, archive_digest)

    class Cache:
        class Manifest:
            digest = assignment.model_artifact_set_sha256

        def manifest_for_artifact_set(self, digest):
            assert digest == assignment.model_artifact_set_sha256
            return self.Manifest()

        def resolve_verified_artifact_set(self, digest):
            assert digest == assignment.model_artifact_set_sha256
            return tuple(
                {
                    "path": item.name,
                    "sha256": item.sha256,
                    "bytes": item.bytes,
                    "file": tmp_path / item.sha256,
                }
                for item in model_objects
            )

        def cached_artifact_file(self, set_digest, digest, path):
            assert set_digest == assignment.model_artifact_set_sha256
            item = next(
                item
                for item in model_objects
                if item.sha256 == digest and item.name == path
            )
            return tmp_path / item.sha256, item.bytes, item.sha256

    def process_service():
        # Each process has a new adapter and no shared in-memory path index.
        source = CompositeObjectSource(
            ModelCacheObjectSource.from_service(Cache()), image_source
        )
        return DistributionService(source, clock=clock, sessions=services.sessions)

    process_service().register(assignment)
    # Serving must also work again after the API process restarts.
    for _ in range(2):
        object.__setattr__(services, "distribution", process_service())
        for item in model_objects:
            response = client.get(
                f"/agent/distribution/objects/{item.sha256}?plan_digest={assignment.plan_digest}",
                headers={
                    **agent_headers(NODE_A, "serial-a"),
                    "Range": f"bytes=0-{item.bytes - 1}",
                    "If-Range": f'"sha256:{item.sha256}"',
                },
            )
            assert response.status_code == 200
            assert response.headers["x-vonk-file"] == (
                (tmp_path / item.sha256).relative_to("/").as_posix()
            )


def test_distribution_binds_opaque_cache_and_image_identities(agent_system) -> None:
    _client, _services, _tokens, clock = agent_system
    source = MemoryObjectSource()
    assignment = _assignment(
        NODE_A,
        source.put(b"model payload"),
        source.put(b"config!"),
        source.put(b"oci archive"),
    )
    source.register_artifact_set("c" * 64, assignment.objects)
    source.register_runtime_image(
        assignment.oci_image_digest, assignment.oci_archive_sha256
    )
    service = DistributionService(source, clock=clock)
    with pytest.raises(DistributionError):
        service.register(assignment)

    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    service.register(assignment)
    assert (
        service.authorize(node_id=NODE_A, plan_digest=assignment.plan_digest)
        == assignment
    )


def test_model_cache_adapter_consumes_service_manifest_identity(tmp_path) -> None:
    payload = b"model payload"
    digest = __import__("hashlib").sha256(payload).hexdigest()
    path = tmp_path / "model.bin"
    path.write_bytes(payload)

    class Cache:
        class Manifest:
            digest = "b" * 64

        def manifest_for_artifact_set(self, set_digest):
            assert set_digest == "b" * 64
            return self.Manifest()

        def resolve_verified_artifact_set(self, set_digest):
            return (
                {
                    "path": "weights/model.bin",
                    "sha256": digest,
                    "bytes": len(payload),
                    "file": path,
                },
            )

        def cached_artifact_file(self, set_digest, object_digest, object_path):
            assert (
                set_digest == "b" * 64
                and object_digest == digest
                and object_path == "weights/model.bin"
            )
            return path, len(payload), digest

    source = ModelCacheObjectSource.from_service(Cache())
    obj = DistributionObject(
        name="weights/model.bin", sha256=digest, bytes=len(payload), kind="model"
    )
    assert source.verify_artifact_set("b" * 64, (obj,))
    opened = source.open_object(digest, len(payload))
    assert opened.stream.read() == payload


def test_stored_object_is_served_by_name_and_size_without_hashing(tmp_path) -> None:
    # The name is the digest; the bytes were verified where they entered. A
    # same-size file under that name is served as stored, which only holds if
    # nothing re-hashes it. A wrong size is a recoverable miss.
    digest = "c" * 64
    (tmp_path / digest).write_bytes(b"12345678")
    tmp_path.chmod(0o700)
    (tmp_path / digest).chmod(0o640)
    source = FilesystemObjectSource(tmp_path)

    with source.open_object(digest, 8).stream as stream:
        assert stream.read() == b"12345678"
    with pytest.raises(DistributionError):
        source.open_object(digest, 9)
    with source.open_object(digest, 8).stream as stream:
        assert stream.read() == b"12345678"


def test_model_cache_manifest_publication_is_atomic_across_readers(tmp_path):
    # Break caught: describing one file exposes its authorization before the
    # rest of the set has been checked, even if a later descriptor is corrupt.
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace

    descriptor_ready, finish = Event(), Event()
    path = tmp_path / "model.bin"
    path.write_bytes(b"model")
    digest = __import__("hashlib").sha256(b"model").hexdigest()

    class Cache:
        def manifest_for_artifact_set(self, _digest):
            return SimpleNamespace(digest="b" * 64)

        def resolve_verified_artifact_set(self, _digest):
            yield {"path": "model.bin", "sha256": digest, "bytes": 5, "file": path}
            descriptor_ready.set()
            assert finish.wait(5)
            yield {"path": "malformed"}

        def cached_artifact_file(self, *_args):
            return path, 5, digest

    source = ModelCacheObjectSource.from_service(Cache())
    with ThreadPoolExecutor(max_workers=1) as pool:
        loading = pool.submit(source.objects_for_set, "b" * 64)
        try:
            assert descriptor_ready.wait(5)
            with pytest.raises(DistributionError):
                source.open_object(digest, 5)
        finally:
            finish.set()
        with pytest.raises(DistributionError):
            loading.result(timeout=5)
    with pytest.raises(DistributionError):
        source.open_object(digest, 5)


def test_authorization_is_decided_once_per_assignment_not_per_range(
    agent_system,
) -> None:
    from sqlalchemy import event

    _client, services, _tokens, clock = agent_system
    source = MemoryObjectSource()
    assignment = _assignment(
        NODE_A,
        source.put(b"model payload"),
        source.put(b"config!"),
        source.put(b"oci archive"),
    )
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(
        assignment.oci_image_digest, assignment.oci_archive_sha256
    )
    service = DistributionService(source, clock=clock, sessions=services.sessions)
    service.register(assignment)
    statements: list[str] = []
    engine = services.sessions.kw["bind"]

    def count(_conn, _cursor, statement, *_rest):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        for _ in range(5):
            service.authorize(node_id=NODE_A, plan_digest=assignment.plan_digest)
    finally:
        event.remove(engine, "before_cursor_execute", count)

    assert len(statements) == 1
    service.revoke(plan_digest=assignment.plan_digest, node_id=NODE_A)
    with pytest.raises(DistributionError):
        service.authorize(node_id=NODE_A, plan_digest=assignment.plan_digest)


def test_object_location_is_resolved_once_per_window_and_follows_the_file(
    agent_system,
) -> None:
    _client, services, _tokens, clock = agent_system

    class CountingSource(MemoryObjectSource):
        opened = 0

        def open_object(self, digest, expected_bytes):
            self.opened += 1
            if not (self.root / digest).exists():
                raise DistributionUnknown(
                    DistributionCode.OBJECT_UNAVAILABLE, "stored object is unavailable"
                )
            return super().open_object(digest, expected_bytes)

    source = CountingSource()
    model = source.put(b"model payload")
    assignment = _assignment(NODE_A, model, source.put(b"config!"), source.put(b"oci"))
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(
        assignment.oci_image_digest, assignment.oci_archive_sha256
    )
    service = DistributionService(source, clock=clock, sessions=services.sessions)
    service.register(assignment)
    ask = {"node_id": NODE_A, "plan_digest": assignment.plan_digest, "digest": model}

    # Many range requests of one transfer resolve the stored object once.
    for _ in range(5):
        _assignment_value, spec, location = service.locate_object(**ask)
    assert source.opened == 1
    assert spec.sha256 == model and location.path == source.root / model

    # A file that disappeared is looked up again as a recoverable miss.
    (source.root / model).unlink()
    with pytest.raises(DistributionError):
        service.locate_object(**ask)

    # A revoked assignment is refused no matter what was remembered.
    (source.root / model).write_bytes(b"model payload")
    service.locate_object(**ask)
    service.revoke(plan_digest=assignment.plan_digest, node_id=NODE_A)
    with pytest.raises(DistributionError):
        service.locate_object(**ask)


def test_revoked_distribution_is_a_refusal_on_the_agent_wire(agent_system):
    """A revoked grant must never advertise a temporary source outage."""
    client, services, _, clock = agent_system
    source = MemoryObjectSource()
    model_digest = source.put(b"model payload")
    config_digest = source.put(b"config!")
    archive_digest = source.put(b"oci archive")
    assignment = _assignment(NODE_A, model_digest, config_digest, archive_digest)
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(assignment.oci_image_digest, archive_digest)
    service = DistributionService(source, clock=clock, sessions=services.sessions)
    service.register(assignment)
    service.revoke(plan_digest=assignment.plan_digest, node_id=NODE_A)
    object.__setattr__(services, "distribution", service)
    denied = client.get(
        "/agent/distribution/manifests/" + assignment.plan_digest,
        headers=agent_headers(NODE_A, "serial-a"),
    )
    assert denied.status_code == 403
    assert denied.headers["x-vonk-error-code"] == "distribution.revoked"


@pytest.mark.parametrize("denial", ["permission", "typed"])
def test_denied_nas_source_cannot_fall_back_then_recovers_after_access_restored(
    agent_system, tmp_path, denial
):
    """Catch the composite source hiding an explicit NAS refusal with OCI bytes."""
    from vonk_agent_protocol import SecurityRefusalError, SecurityRefusalReason

    client, services, _, clock = agent_system
    fallback = MemoryObjectSource()
    model_digest = fallback.put(b"model payload")
    config_digest = fallback.put(b"config!")
    archive_digest = fallback.put(b"oci archive")
    assignment = _assignment(NODE_A, model_digest, config_digest, archive_digest)
    fallback.register_runtime_image(assignment.oci_image_digest, archive_digest)
    files = {}
    for item in assignment.objects:
        path = tmp_path / item.sha256
        path.write_bytes(fallback.objects[item.sha256])
        files[item.sha256] = path
    denied = [True]

    class Cache:
        def cached_artifact_file(self, set_digest, digest, path):
            if denied[0]:
                if denial == "permission":
                    raise PermissionError(13, "NAS access denied")
                raise SecurityRefusalError(
                    "NAS authority denied",
                    reason=SecurityRefusalReason.PERMISSION_DENIED,
                )
            return files[digest], files[digest].stat().st_size, digest

    nas = ModelCacheObjectSource.from_service(Cache())
    nas._manifests[assignment.model_artifact_set_sha256] = assignment.objects
    nas._paths.update(
        {
            item.sha256: (
                assignment.model_artifact_set_sha256,
                item.name,
                files[item.sha256],
            )
            for item in assignment.objects
        }
    )
    service = DistributionService(CompositeObjectSource(nas, fallback), clock=clock)
    service.register(assignment)
    object.__setattr__(services, "distribution", service)
    url = f"/agent/distribution/objects/{model_digest}?plan_digest={assignment.plan_digest}"
    response = client.get(url, headers=agent_headers(NODE_A, "serial-a"))
    assert response.status_code == 403
    assert response.headers["x-vonk-error-code"] == "permission_denied"
    assert "x-vonk-file" not in response.headers
    denied[0] = False
    recovered = client.get(url, headers=agent_headers(NODE_A, "serial-a"))
    assert recovered.status_code == 200
    assert (
        recovered.headers["x-vonk-file"]
        == files[model_digest].relative_to("/").as_posix()
    )


def test_secondary_source_refusal_is_not_masked_by_primary_cache_miss():
    from vonk_agent_protocol import SecurityRefusalReason
    from vonk_control.distribution import DistributionRefused

    class DeniedSource(MemoryObjectSource):
        def open_object(self, digest, expected_bytes):
            raise DistributionRefused(
                "permission_denied",
                "OCI access denied",
                reason=SecurityRefusalReason.PERMISSION_DENIED,
            )

    source = CompositeObjectSource(MemoryObjectSource(), DeniedSource())
    with pytest.raises(DistributionRefused):
        source.open_object("a" * 64, 13)
