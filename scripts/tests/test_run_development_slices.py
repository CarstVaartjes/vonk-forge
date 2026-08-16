from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "run-development-slices"
STATES = [
    "inventory-ready",
    "recipe-resolved",
    "source-verified",
    "image-built",
    "image-distributed",
    "installed",
    "running",
    "route-published",
    "inference-ok",
    "stopped",
    "route-withdrawn",
    "uninstalled",
]
MODEL_MULTINODE_STATES = [
    *STATES[:9],
    "rank-failure-observed",
    "route-withdrawn-after-failure",
    "rank-recovered",
    "route-republished",
    "inference-recovered",
    "restart-persistence-observed",
    *STATES[9:],
]
NODE = "spk_0123456789abcdef0123456789abcdef"
NODE_2 = "spk_fedcba9876543210fedcba9876543210"
ADMIN_TOKEN = "admin-secret-marker"
INFERENCE_TOKEN = "inference-secret-marker"
RECIPE_DIGEST = "90396dc5d736ad8083ddfa23f90b2ecef5c05ea1c3129da5375455ddd684413a"


class SliceServer(ThreadingHTTPServer):
    def __init__(self, address, *, fail_path: str | None = None):
        super().__init__(address, SliceHandler)
        self.fail_path = fail_path
        self.fail_once_path: str | None = None
        self.requests: list[tuple[str, str, str]] = []
        self.request_bodies: list[tuple[str, str, bytes]] = []
        self.recipe_created = False
        self.recipe_digest = RECIPE_DIGEST
        self.recipe_revision = 1
        self.source_digest = (
            "61086ce766236b70045c7c45dbc7615a24e4cef96e0cad424de808d5f0861f94"
        )
        self.slug = "dev-http-smoke"
        self.route_published = False
        self.withdrawn_endpoint_status = 404
        self.operation = 0
        self.operation_nodes: dict[str, list[str]] = {}
        self.operation_kinds: dict[str, str] = {}
        self.operation_owners: dict[str, str] = {}
        self.operation_states: dict[str, str] = {}
        self.operation_plan_digests: dict[str, str] = {}
        self.build_operation_state = "succeeded"
        self.distribution_operation_state = "succeeded"
        self.install_operation_state = "succeeded"
        self.install_preview_blockers: list[str] = []
        self.stop_preview_blockers: list[str] = []
        self.stop_plan_digest = "a" * 64
        self.stop_preview_issued = False
        self.uninstall_preview_blockers: list[str] = []
        self.uninstall_plan_digest = "b" * 64
        self.uninstall_preview_issued = False
        self.retry_operation_state = "succeeded"
        self.start_operation_states: list[str] = []
        self.run_plan_digest = "f" * 64
        self.run = 0
        self.run_creation_attempts = 0
        self.interrupt_run_creation_number: int | None = None
        self.commit_interrupted_run_creation = False
        self.run_operations_by_request_key: dict[str, dict[str, object]] = {}
        self.run_authority_by_request_key: dict[str, tuple[str, str, str]] = {}
        self.run_preview_authority_by_digest: dict[str, tuple[str, str]] = {}
        self.run_states: dict[str, tuple[str, str]] = {}
        self.add_empty_provider_metadata = False
        self.nodes = [NODE]
        self.online = {NODE: True, NODE_2: True}
        self.inventory_stale = {NODE: False, NODE_2: False}
        self.inventory_capabilities = {
            NODE: [
                "recipe.operations.v1",
                "build.rootless-podman.v1",
                "runtime.spark-docker-nvidia.v1",
            ],
            NODE_2: [
                "recipe.operations.v1",
                "build.rootless-podman.v1",
                "runtime.spark-docker-nvidia.v1",
            ],
        }
        self.last_seen = {
            NODE: "2026-08-11T10:00:00+00:00",
            NODE_2: "2026-08-11T10:00:00+00:00",
        }
        self.fleet_digest = "b" * 64
        self.artifact_set_digests = {NODE: "7" * 64, NODE_2: "7" * 64}
        self.rank_states = {NODE: "running", NODE_2: "running"}
        self.entity_mismatch = False
        self.entity = 0
        self.entities: dict[tuple[str, str, str], dict[str, object]] = {}


class SliceHandler(BaseHTTPRequestHandler):
    server: SliceServer

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def _body(self) -> bytes:
        length = int(self.headers.get("content-length", "0"))
        return self.rfile.read(length)

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self) -> bytes:
        body = self._body()
        authorization = self.headers.get("authorization", "")
        self.server.requests.append((self.command, self.path, authorization))
        self.server.request_bodies.append((self.command, self.path, body))
        if self.server.fail_path == self.path:
            self._json(
                409,
                {
                    "detail": (
                        f"Authorization: Bearer {ADMIN_TOKEN}; "
                        f"inference={INFERENCE_TOKEN}"
                    )
                },
            )
            raise RuntimeError("handled failure")
        if self.server.fail_once_path == self.path:
            self.server.fail_once_path = None
            self._json(503, {"detail": "temporary test interruption"})
            raise RuntimeError("handled failure")
        return body

    @staticmethod
    def _entity_response(record: dict[str, object]) -> dict[str, object]:
        document = record["document"]
        assert isinstance(document, dict)
        identity = document["identity"]
        metadata = document["metadata"]
        assert isinstance(identity, dict)
        assert isinstance(metadata, dict)
        return {
            "entity_id": record["entity_id"],
            "kind": document["kind"],
            "publisher": identity["publisher"],
            "slug": identity["slug"],
            "title": metadata["title"],
            "revision_id": record["revision_id"],
            "revision_number": record["revision_number"],
            "lifecycle": record["lifecycle"],
            "schema_version": document["schema_version"],
            "document": document,
            "content_sha256": record["content_sha256"],
            "created_by": "development-runner",
            "created_at": "2026-08-15T12:00:00+00:00",
        }

    def do_GET(self) -> None:
        try:
            self._record()
        except RuntimeError:
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/v1/catalog/entities":
            query = urllib.parse.parse_qs(parsed.query)
            kind = query.get("kind", [None])[0]
            publisher = query.get("publisher", [None])[0]
            if self.server.entity_mismatch and not self.server.entities:
                document = json.loads(
                    (
                        ROOT
                        / "control/tests/fixtures/recipes/dev-http-smoke/entities/01-model-group.json"
                    ).read_text()
                )
                document["metadata"]["title"] = "Mismatched development fixture"
                record = {
                    "entity_id": "50000000-0000-4000-8000-000000000001",
                    "revision_id": "50000000-0000-4000-8001-000000000001",
                    "revision_number": 2,
                    "lifecycle": "resolved",
                    "document": document,
                    "content_sha256": "f" * 64,
                }
                identity = document["identity"]
                self.server.entities[
                    (document["kind"], identity["publisher"], identity["slug"])
                ] = record
            entities = [
                self._entity_response(record)
                for (
                    entity_kind,
                    entity_publisher,
                    _slug,
                ), record in self.server.entities.items()
                if (kind is None or entity_kind == kind)
                and (publisher is None or entity_publisher == publisher)
            ]
            self._json(200, {"entities": entities, "next_cursor": None})
        elif parsed.path.startswith("/api/v1/catalog/entities/"):
            entity_id = parsed.path.rsplit("/", 1)[-1]
            record = next(
                (
                    value
                    for value in self.server.entities.values()
                    if value["entity_id"] == entity_id
                ),
                None,
            )
            if record is None:
                self._json(404, {"detail": "not found"})
            else:
                self._json(200, self._entity_response(record))
        elif self.path == "/api/v1/fleet":
            self._json(
                200,
                {
                    "commit": "a" * 40,
                    "evidence_digest": self.server.fleet_digest,
                    "nodes": [
                        {
                            "id": node,
                            "healthy": True,
                            "stale": False,
                            "agent_online": self.server.online[node],
                            "agent_state": "active",
                            "compatibility": "supported",
                            "inventory_stale": self.server.inventory_stale[node],
                            "inventory_capabilities": (
                                self.server.inventory_capabilities[node]
                            ),
                            "agent_last_seen_at": self.server.last_seen[node],
                        }
                        for node in self.server.nodes
                    ],
                },
            )
        elif self.path == (
            "/api/v1/catalog/recipes/10000000-0000-4000-8000-000000000001"
        ):
            self._json(
                200,
                {
                    "recipe_id": "10000000-0000-4000-8000-000000000001",
                    "id": "10000000-0000-4000-8000-000000000002",
                    "revision_number": self.server.recipe_revision,
                    "lifecycle": "resolved",
                    "content_sha256": self.server.recipe_digest,
                    "source_bundle_sha256": self.server.source_digest,
                },
            )
        elif self.path.startswith("/api/v1/catalog/recipes"):
            recipes = []
            if self.server.recipe_created:
                recipes.append(
                    {
                        "recipe_id": "10000000-0000-4000-8000-000000000001",
                        "slug": self.server.slug,
                        "revision_number": self.server.recipe_revision,
                        "lifecycle": "resolved",
                        "content_sha256": self.server.recipe_digest,
                    }
                )
            self._json(200, {"recipes": recipes, "next_cursor": None})
        elif self.path.startswith("/api/v1/recipes/operations/"):
            operation_id = self.path.rsplit("/", 1)[-1]
            nodes = self.server.operation_nodes.get(operation_id, self.server.nodes)
            kind = self.server.operation_kinds.get(operation_id, "unknown")
            node_evidence: dict[str, dict[str, object]] = {}
            for rank, node in enumerate(nodes):
                if kind == "build":
                    node_evidence[node] = {
                        "build_input_sha256": "e" * 64,
                        "image_bytes": 123456789,
                        "image_digest": "sha256:" + "9" * 64,
                        "oci_layout_sha256": "8" * 64,
                        "policy": {
                            "passed": True,
                            "findings": [],
                            "dockerfile": "Dockerfile",
                        },
                    }
                elif kind == "distribution":
                    node_evidence[node] = {
                        "build_id": "20000000-0000-4000-8000-000000000001",
                        "image_bytes": 123456789,
                        "image_digest": "sha256:" + "9" * 64,
                        "oci_layout_sha256": "8" * 64,
                    }
                elif kind == "start":
                    node_evidence[node] = {
                        "image_digest": "9" * 64,
                        "artifact_set_digest": self.server.artifact_set_digests[node],
                        "rank": rank,
                        "ready": True,
                        "evidence_digest": f"{rank + 1:064x}",
                    }
                elif kind == "install":
                    node_evidence[node] = {"installed_bytes": 123456789}
                else:
                    node_evidence[node] = {"status": "ok"}
            self._json(
                200,
                {
                    "id": operation_id,
                    "kind": "recipe.test",
                    "owner_id": self.server.operation_owners.get(
                        operation_id, "20000000-0000-4000-8000-000000000001"
                    ),
                    "state": self.server.operation_states.get(
                        operation_id, "succeeded"
                    ),
                    "plan_digest": self.server.operation_plan_digests.get(
                        operation_id, "f" * 64
                    ),
                    "nodes": [NODE],
                    "result": {
                        "successful_nodes": sorted(nodes),
                        "failed_nodes": [],
                        "node_evidence": node_evidence,
                    },
                },
            )
        elif self.path == f"/api/v1/endpoints/{self.server.slug}":
            if self.server.route_published:
                self._json(
                    200,
                    {
                        "alias": self.server.slug,
                        "state": "published",
                        "nodes": [NODE],
                    },
                )
            else:
                detail = (
                    "endpoint publication unavailable"
                    if self.server.withdrawn_endpoint_status == 503
                    else "not found"
                )
                self._json(self.server.withdrawn_endpoint_status, {"detail": detail})
        elif self.path.startswith("/api/v1/recipes/runs/"):
            run_id = self.path.rsplit("/", 1)[-1]
            run_state, route_state = self.server.run_states.get(
                run_id,
                (
                    "running",
                    "published" if self.server.route_published else "withdrawn",
                ),
            )
            ranks = [
                {
                    "node_id": node,
                    "rank": rank,
                    "role": "entrypoint" if rank == 0 else "worker",
                    "state": self.server.rank_states[node],
                    "observed_at": "2026-08-11T10:00:00Z",
                    "age_seconds": 1.0,
                    "fresh": True,
                }
                for rank, node in enumerate(self.server.nodes)
            ]
            self._json(
                200,
                {
                    "id": run_id,
                    "alias": self.server.slug,
                    "state": run_state,
                    "route_state": route_state,
                    "healthy": all(rank["state"] == "running" for rank in ranks),
                    "ranks": ranks,
                },
            )
        else:
            self._json(404, {"detail": "not found"})

    def do_PUT(self) -> None:
        try:
            body = self._record()
        except RuntimeError:
            return
        if self.path.endswith("/draft"):
            payload = json.loads(body)
            if payload.get("expected_revision") != self.server.recipe_revision:
                self._json(409, {"detail": "stale revision"})
                return
            document = payload["document"]
            self.server.recipe_revision += 1
            self.server.recipe_digest = hashlib.sha256(
                json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            self.server.source_digest = document["build"]["context"]["sha256"]
            self._json(
                200,
                {
                    "recipe_id": "10000000-0000-4000-8000-000000000001",
                    "id": "10000000-0000-4000-8000-000000000003",
                    "revision_number": self.server.recipe_revision,
                    "lifecycle": "draft",
                    "content_sha256": None,
                    "source_bundle_sha256": self.server.source_digest,
                },
            )
            return
        digest = self.path.rsplit("/", 1)[-1]
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as archive:
            files = [member for member in archive.getmembers() if member.isfile()]
        self._json(
            200,
            {
                "sha256": digest,
                "archive_bytes": len(body),
                "total_bytes": sum(member.size for member in files),
                "file_count": len(files),
                "files": [member.name for member in files],
            },
        )

    def do_POST(self) -> None:
        try:
            body = self._record()
        except RuntimeError:
            return
        payload = json.loads(body) if body else {}
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/v1/catalog/entities":
            document = payload["document"]
            identity = document["identity"]
            key = (document["kind"], identity["publisher"], identity["slug"])
            if key in self.server.entities:
                self._json(409, {"detail": "catalog entity identity already exists"})
                return
            self.server.entity += 1
            record = {
                "entity_id": f"50000000-0000-4000-8000-{self.server.entity:012d}",
                "revision_id": f"50000000-0000-4000-8001-{self.server.entity:012d}",
                "revision_number": 1,
                "lifecycle": "draft",
                "document": document,
                "content_sha256": None,
            }
            self.server.entities[key] = record
            self._json(201, self._entity_response(record))
        elif path.startswith("/api/v1/catalog/entities/") and path.endswith("/resolve"):
            entity_id = path.rsplit("/", 2)[-2]
            record = next(
                (
                    value
                    for value in self.server.entities.values()
                    if value["entity_id"] == entity_id
                ),
                None,
            )
            if record is None:
                self._json(404, {"detail": "not found"})
                return
            if payload.get("expected_revision") != record["revision_number"]:
                self._json(409, {"detail": "stale revision"})
                return
            record["revision_number"] = int(record["revision_number"]) + 1
            record["lifecycle"] = "resolved"
            record["content_sha256"] = hashlib.sha256(
                json.dumps(
                    record["document"], sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            self._json(200, self._entity_response(record))
        elif path == "/api/v1/catalog/recipes":
            self.server.recipe_created = True
            document = payload["document"]
            self.server.slug = payload["slug"]
            self.server.recipe_digest = hashlib.sha256(
                json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            self.server.source_digest = document["build"]["context"]["sha256"]
            self._json(
                201,
                {
                    "recipe_id": "10000000-0000-4000-8000-000000000001",
                    "id": "10000000-0000-4000-8000-000000000002",
                    "revision_number": self.server.recipe_revision,
                    "lifecycle": "draft",
                    "content_sha256": None,
                    "source_bundle_sha256": payload["document"]["build"]["context"][
                        "sha256"
                    ],
                },
            )
        elif path.endswith("/resolve"):
            self._json(
                200,
                {
                    "recipe_id": "10000000-0000-4000-8000-000000000001",
                    "id": "10000000-0000-4000-8000-000000000002",
                    "revision_number": self.server.recipe_revision,
                    "lifecycle": "resolved",
                    "content_sha256": self.server.recipe_digest,
                    "source_bundle_sha256": self.server.source_digest,
                },
            )
        elif path == "/api/v1/recipes/source-checks":
            self._json(
                200,
                {
                    "passed": True,
                    "source_bundle_sha256": self.server.source_digest,
                    "dockerfile": "Dockerfile",
                    "findings": [],
                },
            )
        elif path.endswith("mapping-plans/preview"):
            self._json(
                200,
                {
                    "generation": 1,
                    "placement_digest": "d" * 64,
                    "nodes": [{"node_id": NODE, "rank": 0, "endpoint_owner": True}],
                },
            )
        elif path.endswith("/mappings"):
            self._json(
                201,
                {
                    "mapping_id": "30000000-0000-4000-8000-000000000001",
                    "generation": 1,
                    "placement_digest": "d" * 64,
                },
            )
        elif path.endswith("build-plans/preview"):
            self._json(
                200,
                {"build_input_sha256": "e" * 64, "source_bundle_sha256": "f" * 64},
            )
        elif path == "/api/v1/recipes/install-plans/preview":
            blocker = (
                self.server.install_preview_blockers.pop(0)
                if self.server.install_preview_blockers
                else None
            )
            self._json(
                200,
                {
                    "allowed": blocker is None,
                    "plan_digest": "f" * 64,
                    "nodes": [
                        {
                            "node_id": node,
                            "allowed": blocker is None,
                            "blockers": (
                                []
                                if blocker is None
                                else [{"code": blocker, "detail": "test blocker"}]
                            ),
                        }
                        for node in self.server.nodes
                    ],
                },
            )
        elif path.endswith("run-plans/preview"):
            plan_digest = self.server.run_plan_digest
            installation_id = payload.get("installation_id")
            alias = payload.get("alias")
            if not isinstance(installation_id, str) or not isinstance(alias, str):
                self._json(422, {"detail": "run preview alias is required"})
                return
            self.server.run_preview_authority_by_digest[plan_digest] = (
                installation_id,
                alias,
            )
            self._json(
                200,
                {
                    "alias": alias,
                    "allowed": True,
                    "plan_digest": plan_digest,
                    "nodes": [],
                },
            )
        elif path == "/api/v1/recipes/stop-plans/preview":
            blocker = (
                self.server.stop_preview_blockers.pop(0)
                if self.server.stop_preview_blockers
                else None
            )
            self.server.stop_preview_issued = blocker is None
            self._json(
                200,
                {
                    "allowed": blocker is None,
                    "plan_digest": self.server.stop_plan_digest,
                    "nodes": [],
                    "blockers": (
                        []
                        if blocker is None
                        else [{"code": blocker, "detail": "test blocker"}]
                    ),
                    "warnings": [],
                },
            )
        elif path == "/api/v1/recipes/uninstall-plans/preview":
            blocker = (
                self.server.uninstall_preview_blockers.pop(0)
                if self.server.uninstall_preview_blockers
                else None
            )
            self.server.uninstall_preview_issued = blocker is None
            self._json(
                200,
                {
                    "allowed": blocker is None,
                    "plan_digest": self.server.uninstall_plan_digest,
                    "nodes": [],
                    "blockers": (
                        []
                        if blocker is None
                        else [{"code": blocker, "detail": "test blocker"}]
                    ),
                    "warnings": [],
                },
            )
        elif path == "/v1/chat/completions":
            if payload.get("model") == "dev-http-smoke":
                response = json.loads(
                    (
                        ROOT
                        / "control/tests/fixtures/recipes/dev-http-smoke/expected.json"
                    ).read_text()
                )["response"]
                if self.server.add_empty_provider_metadata:
                    response["choices"][0]["provider_specific_fields"] = {}
                    response["choices"][0]["message"]["provider_specific_fields"] = {
                        "refusal": None
                    }
            else:
                response = {
                    "id": "chatcmpl-model-smoke",
                    "object": "chat.completion",
                    "model": payload["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "VONK MODEL OK",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }
            self._json(200, response)
        elif path.endswith("/stop"):
            if (
                not self.server.stop_preview_issued
                or payload.get("plan_digest") != self.server.stop_plan_digest
            ):
                self._json(
                    409,
                    {"detail": "submitted stop plan digest does not match preview"},
                )
                return
            self.server.route_published = False
            run_id = path.rsplit("/", 2)[-2]
            self.server.run_states[run_id] = ("stopped", "withdrawn")
            self._operation("20000000-0000-4000-8000-000000000004", self.server.nodes)
        elif path.endswith("/uninstall"):
            if (
                not self.server.uninstall_preview_issued
                or payload.get("plan_digest") != self.server.uninstall_plan_digest
            ):
                self._json(
                    409,
                    {"detail": "submitted uninstall plan digest does not match preview"},
                )
                return
            self._operation("20000000-0000-4000-8000-000000000005", self.server.nodes)
        elif path.startswith("/api/v1/recipes/operations/") and path.endswith("/retry"):
            original_operation_id = self.path.rsplit("/", 2)[-2]
            original_kind = self.server.operation_kinds.get(
                original_operation_id, "build"
            )
            self._operation(
                {
                    "build": "20000000-0000-4000-8000-000000000001",
                    "distribution": "20000000-0000-4000-8000-000000000002",
                    "install": "20000000-0000-4000-8000-000000000003",
                }[original_kind],
                [NODE],
                kind=original_kind,
                state=self.server.retry_operation_state,
            )
        else:
            owner = {
                "/api/v1/recipes/builds": "20000000-0000-4000-8000-000000000001",
                "/api/v1/recipes/image-distributions": "20000000-0000-4000-8000-000000000002",
                "/api/v1/recipes/installations": "20000000-0000-4000-8000-000000000003",
            }.get(path)
            if path == "/api/v1/recipes/runs":
                request_key = payload["request_key"]
                existing = self.server.run_operations_by_request_key.get(request_key)
                submitted_authority = (
                    payload.get("installation_id"),
                    payload.get("alias"),
                    payload.get("plan_digest"),
                )
                if (
                    existing is not None
                    and self.server.run_authority_by_request_key.get(request_key)
                    == submitted_authority
                ):
                    self._json(202, existing)
                    return
                preview_authority = self.server.run_preview_authority_by_digest.get(
                    payload.get("plan_digest")
                )
                if (
                    payload.get("plan_digest") != self.server.run_plan_digest
                    or preview_authority
                    != (payload.get("installation_id"), payload.get("alias"))
                ):
                    self._json(
                        409,
                        {"detail": "submitted plan digest does not match preview"},
                    )
                    return
                if existing is not None:
                    self._json(
                        409,
                        {"detail": "request key was already used differently"},
                    )
                    return
                self.server.run_creation_attempts += 1
                interrupt = (
                    self.server.interrupt_run_creation_number
                    == self.server.run_creation_attempts
                )
                if interrupt and not self.server.commit_interrupted_run_creation:
                    self.server.interrupt_run_creation_number = None
                    self._json(503, {"detail": "temporary test interruption"})
                    return
                self.server.run += 1
                owner = f"20000000-0000-4000-8001-{self.server.run:012d}"
            if owner is None:
                self._json(404, {"detail": "not found"})
                return
            if path == "/api/v1/recipes/runs":
                start_state = (
                    self.server.start_operation_states.pop(0)
                    if self.server.start_operation_states
                    else "succeeded"
                )
                if start_state == "succeeded":
                    self.server.route_published = True
                    self.server.run_states[owner] = ("running", "published")
                else:
                    self.server.route_published = False
                    self.server.run_states[owner] = ("stopped", "withdrawn")
            else:
                start_state = None
            kind = {
                "/api/v1/recipes/builds": "build",
                "/api/v1/recipes/image-distributions": "distribution",
                "/api/v1/recipes/installations": "install",
                "/api/v1/recipes/runs": "start",
            }[path]
            nodes = [NODE] if kind == "build" else self.server.nodes
            operation = self._register_operation(
                owner,
                nodes,
                kind=kind,
                state=start_state,
                plan_digest=payload.get("plan_digest", "f" * 64),
            )
            if path == "/api/v1/recipes/runs":
                self.server.run_operations_by_request_key[payload["request_key"]] = (
                    operation
                )
                self.server.run_authority_by_request_key[payload["request_key"]] = (
                    payload["installation_id"],
                    payload["alias"],
                    payload["plan_digest"],
                )
                if interrupt:
                    self.server.interrupt_run_creation_number = None
                    self._json(503, {"detail": "temporary test interruption"})
                    return
            self._json(202, operation)

    def _operation(
        self,
        owner: str,
        nodes: list[str] | None = None,
        *,
        kind: str = "lifecycle",
        state: str | None = None,
        plan_digest: str = "f" * 64,
    ) -> None:
        self._json(
            202,
            self._register_operation(
                owner,
                nodes,
                kind=kind,
                state=state,
                plan_digest=plan_digest,
            ),
        )

    def _register_operation(
        self,
        owner: str,
        nodes: list[str] | None = None,
        *,
        kind: str = "lifecycle",
        state: str | None = None,
        plan_digest: str = "f" * 64,
    ) -> dict[str, object]:
        self.server.operation += 1
        operation_id = f"40000000-0000-4000-8000-{self.server.operation:012d}"
        self.server.operation_nodes[operation_id] = list(nodes or self.server.nodes)
        self.server.operation_kinds[operation_id] = kind
        self.server.operation_owners[operation_id] = owner
        self.server.operation_plan_digests[operation_id] = plan_digest
        if state is None and kind == "build":
            state = self.server.build_operation_state
        if state is None and kind == "distribution":
            state = self.server.distribution_operation_state
        if state is None and kind == "install":
            state = self.server.install_operation_state
        if state is not None:
            self.server.operation_states[operation_id] = state
        return {
            "id": operation_id,
            "kind": "recipe.test",
            "owner_id": owner,
            "state": "queued",
            "plan_digest": plan_digest,
            "nodes": list(nodes or self.server.nodes),
            "result": None,
        }


@pytest.fixture
def server():
    active = SliceServer(("127.0.0.1", 0))
    thread = threading.Thread(target=active.serve_forever, daemon=True)
    thread.start()
    try:
        yield active
    finally:
        active.shutdown()
        thread.join(timeout=5)
        active.server_close()


def _token(path: Path, value: str) -> Path:
    path.write_text(value + "\n", encoding="ascii")
    path.chmod(0o600)
    return path


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _qualification(path: Path, recipe_name: str) -> Path:
    document = {
        "passed": True,
        "recipe": recipe_name,
        "status": "passed",
    }
    path.write_bytes(_canonical(document))
    path.chmod(0o600)
    return path


def _synthetic_source_context(tmp_path: Path) -> Path:
    context = tmp_path / "source-context"
    if not context.exists():
        shutil.copytree(
            ROOT / "control/tests/fixtures/recipes/dev-http-smoke/context",
            context,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    return context


def _run(tmp_path: Path, server: SliceServer, *extra: str):
    admin = _token(tmp_path / "admin-token", ADMIN_TOKEN)
    inference = _token(tmp_path / "inference-token", INFERENCE_TOKEN)
    evidence = tmp_path / "evidence.json"
    result = subprocess.run(
        (
            sys.executable,
            str(RUNNER),
            "--api-base",
            f"http://127.0.0.1:{server.server_port}",
            "--admin-token-file",
            str(admin),
            "--inference-token-file",
            str(inference),
            "--phase",
            "synthetic",
            "--builder-node",
            NODE,
            "--target-node",
            NODE,
            "--source-context",
            str(_synthetic_source_context(tmp_path)),
            "--evidence-file",
            str(evidence),
            "--timeout-seconds",
            "2",
            "--poll-seconds",
            "0.01",
            *extra,
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, evidence


def _run_model(
    tmp_path: Path,
    server: SliceServer,
    *extra: str,
    qualification: Path | None = None,
):
    admin = _token(tmp_path / "admin-token", ADMIN_TOKEN)
    inference = _token(tmp_path / "inference-token", INFERENCE_TOKEN)
    qualification = qualification or _qualification(
        tmp_path / "qualification.json",
        "deepseek-v4-flash-0731-mia-dual.json",
    )
    evidence = tmp_path / "model-evidence.json"
    result = subprocess.run(
        (
            sys.executable,
            str(RUNNER),
            "--api-base",
            f"http://127.0.0.1:{server.server_port}",
            "--admin-token-file",
            str(admin),
            "--inference-token-file",
            str(inference),
            "--phase",
            "model-multinode",
            "--qualification-file",
            str(qualification),
            "--builder-node",
            NODE,
            "--target-node",
            NODE,
            "--target-node",
            NODE_2,
            "--failure-node",
            NODE_2,
            "--evidence-file",
            str(evidence),
            "--timeout-seconds",
            "2",
            "--poll-seconds",
            "0.01",
            *extra,
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, evidence


def _run_model_single(
    tmp_path: Path,
    server: SliceServer,
    *extra: str,
    qualification: Path | None = None,
):
    admin = _token(tmp_path / "admin-token", ADMIN_TOKEN)
    inference = _token(tmp_path / "inference-token", INFERENCE_TOKEN)
    qualification = qualification or _qualification(
        tmp_path / "qualification.json",
        "deepseek-v4-flash-0731-ds4-single.json",
    )
    evidence = tmp_path / "model-single-evidence.json"
    result = subprocess.run(
        (
            sys.executable,
            str(RUNNER),
            "--api-base",
            f"http://127.0.0.1:{server.server_port}",
            "--admin-token-file",
            str(admin),
            "--inference-token-file",
            str(inference),
            "--phase",
            "model-single",
            "--qualification-file",
            str(qualification),
            "--builder-node",
            NODE,
            "--target-node",
            NODE,
            "--evidence-file",
            str(evidence),
            "--timeout-seconds",
            "2",
            "--poll-seconds",
            "0.01",
            *extra,
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, evidence


def _contains_sensitive_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            key.lower()
            in {"authorization", "credential", "password", "secret", "token"}
            or _contains_sensitive_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_sensitive_key(child) for child in value)
    return False


def test_runner_help_exposes_restart_and_failure_checkpoints() -> None:
    result = subprocess.run(
        (sys.executable, str(RUNNER), "--help"),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--stop-after" in result.stdout
    assert "Pause after an accepted state" in result.stdout


def test_runner_authors_exact_entities_in_dependency_order_before_recipe(
    tmp_path: Path, server: SliceServer
) -> None:
    result, _evidence = _run(tmp_path, server, "--stop-after", "recipe-resolved")

    assert result.returncode == 0, result.stderr
    entity_creates = [
        (index, json.loads(body))
        for index, (method, path, body) in enumerate(server.request_bodies)
        if method == "POST" and path == "/api/v1/catalog/entities"
    ]
    assert [payload["document"]["kind"] for _index, payload in entity_creates] == [
        "model-group",
        "model",
        "model-version",
        "execution-harness",
        "runtime-distribution",
    ]
    recipe_index = next(
        index
        for index, (method, path, _body) in enumerate(server.request_bodies)
        if method == "POST" and path == "/api/v1/catalog/recipes"
    )
    assert all(index < recipe_index for index, _payload in entity_creates)
    entity_resolves = [
        index
        for index, (method, path, _body) in enumerate(server.request_bodies)
        if method == "POST"
        and path.startswith("/api/v1/catalog/entities/")
        and path.endswith("/resolve")
    ]
    assert len(entity_resolves) == 5
    assert all(index < recipe_index for index in entity_resolves)
    assert all(
        not _contains_sensitive_key(payload) for _index, payload in entity_creates
    )
    encoded_entities = json.dumps([payload for _index, payload in entity_creates])
    assert ADMIN_TOKEN not in encoded_entities
    assert INFERENCE_TOKEN not in encoded_entities


def test_runner_entity_authoring_is_idempotent(
    tmp_path: Path, server: SliceServer
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()

    first, _ = _run(first_root, server, "--stop-after", "recipe-resolved")
    second, _ = _run(second_root, server, "--stop-after", "recipe-resolved")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    entity_creates = [
        body
        for method, path, body in server.request_bodies
        if method == "POST" and path == "/api/v1/catalog/entities"
    ]
    assert len(entity_creates) == 5


def test_runner_refuses_mismatched_existing_entity_before_recipe(
    tmp_path: Path, server: SliceServer
) -> None:
    server.entity_mismatch = True

    result, _evidence = _run(tmp_path, server, "--stop-after", "recipe-resolved")

    assert result.returncode == 1
    assert result.stderr.strip() == (
        "development slice failed: existing catalog entity does not match checked-in input"
    )
    assert not any(
        path.startswith("/api/v1/catalog/recipes")
        for _method, path, _authorization in server.requests
    )


def test_model_phases_select_native_single_and_pair_recipes(
    tmp_path: Path, server: SliceServer
) -> None:
    server.nodes = [NODE]
    single_root = tmp_path / "single"
    pair_root = tmp_path / "pair"
    single_root.mkdir()
    pair_root.mkdir()

    single, _ = _run_model_single(
        single_root, server, "--stop-after", "recipe-resolved"
    )
    assert single.returncode == 0, single.stderr
    single_document = next(
        json.loads(body)["document"]
        for method, path, body in server.request_bodies
        if method == "POST" and path == "/api/v1/catalog/recipes"
    )
    assert single_document["topology"]["node_count"] == 1

    server.requests.clear()
    server.request_bodies.clear()
    server.recipe_created = False
    server.nodes = [NODE, NODE_2]
    pair, _ = _run_model(pair_root, server, "--stop-after", "recipe-resolved")
    assert pair.returncode == 0, pair.stderr
    pair_document = next(
        json.loads(body)["document"]
        for method, path, body in server.request_bodies
        if method == "POST" and path == "/api/v1/catalog/recipes"
    )
    assert pair_document["topology"]["node_count"] == 2


def test_runner_completes_exact_public_lifecycle_without_secret_leaks(
    tmp_path: Path, server: SliceServer
) -> None:
    result, evidence_path = _run(tmp_path, server)

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == STATES
    assert stat.S_IMODE(evidence_path.stat().st_mode) == 0o600
    encoded = result.stdout + result.stderr + evidence_path.read_text()
    assert ADMIN_TOKEN not in encoded
    assert INFERENCE_TOKEN not in encoded
    assert all(
        authorization in {f"Bearer {ADMIN_TOKEN}", f"Bearer {INFERENCE_TOKEN}"}
        for _method, _path, authorization in server.requests
    )
    assert all(
        path.startswith(("/api/v1/catalog/", "/api/v1/recipes/"))
        or path
        in {
            "/api/v1/fleet",
            "/api/v1/endpoints/dev-http-smoke",
            "/v1/chat/completions",
        }
        for _method, path, _authorization in server.requests
    )
    assert evidence["outputs"]["image_digest"] == "sha256:" + "9" * 64
    assert evidence["outputs"]["oci_layout_sha256"] == "8" * 64
    assert evidence["outputs"]["artifact_set_digest"] == "7" * 64
    assert evidence["outputs"]["distribution_nodes"] == [NODE]
    assert any(
        path == "/api/v1/recipes/image-distributions"
        for _method, path, _authorization in server.requests
    )


@pytest.mark.parametrize(
    ("phase", "preview_path", "apply_path", "identifier", "digest"),
    (
        (
            "stopped",
            "/api/v1/recipes/stop-plans/preview",
            "/api/v1/recipes/runs/{identifier}/stop",
            "run_id",
            "a" * 64,
        ),
        (
            "uninstalled",
            "/api/v1/recipes/uninstall-plans/preview",
            "/api/v1/recipes/installations/{identifier}/uninstall",
            "installation_id",
            "b" * 64,
        ),
    ),
)
def test_runner_previews_lifecycle_action_before_applying_exact_digest(
    tmp_path: Path,
    server: SliceServer,
    phase: str,
    preview_path: str,
    apply_path: str,
    identifier: str,
    digest: str,
) -> None:
    result, evidence_path = _run(tmp_path, server, "--stop-after", phase)

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text())
    selected_id = evidence["outputs"][identifier]
    requests = [
        (path, json.loads(body))
        for method, path, body in server.request_bodies
        if method == "POST" and path in {preview_path, apply_path.format(identifier=selected_id)}
    ]
    assert requests == [
        (preview_path, {identifier: selected_id}),
        (
            apply_path.format(identifier=selected_id),
            {
                "plan_digest": digest,
                "request_key": str(
                    uuid.uuid5(
                        uuid.UUID(evidence["acceptance_id"]),
                        f"{phase}:{'stop' if phase == 'stopped' else 'uninstall'}",
                    )
                ),
            },
        ),
    ]


@pytest.mark.parametrize(
    ("phase", "blockers", "preview_path", "apply_suffix", "message"),
    (
        (
            "stopped",
            "stop_preview_blockers",
            "/api/v1/recipes/stop-plans/preview",
            "/stop",
            "recipe stop is not admitted",
        ),
        (
            "uninstalled",
            "uninstall_preview_blockers",
            "/api/v1/recipes/uninstall-plans/preview",
            "/uninstall",
            "recipe uninstall is not admitted",
        ),
    ),
)
def test_runner_rejects_blocked_lifecycle_action_preview_before_apply(
    tmp_path: Path,
    server: SliceServer,
    phase: str,
    blockers: str,
    preview_path: str,
    apply_suffix: str,
    message: str,
) -> None:
    setattr(server, blockers, ["recipe.action_blocked"])

    result, _evidence_path = _run(tmp_path, server, "--stop-after", phase)

    assert result.returncode == 1
    assert message in result.stderr
    assert any(
        method == "POST" and path == preview_path
        for method, path, _body in server.request_bodies
    )
    assert not any(
        method == "POST" and path.endswith(apply_suffix)
        for method, path, _body in server.request_bodies
    )


@pytest.mark.parametrize(
    ("preview_path", "preview_body", "apply_path", "expected_digest"),
    (
        (
            "/api/v1/recipes/stop-plans/preview",
            {"run_id": "20000000-0000-4000-8001-000000000001"},
            "/api/v1/recipes/runs/20000000-0000-4000-8001-000000000001/stop",
            "a" * 64,
        ),
        (
            "/api/v1/recipes/uninstall-plans/preview",
            {"installation_id": "20000000-0000-4000-8000-000000000003"},
            "/api/v1/recipes/installations/20000000-0000-4000-8000-000000000003/uninstall",
            "b" * 64,
        ),
    ),
)
def test_fake_authority_rejects_wrong_lifecycle_action_digest(
    server: SliceServer,
    preview_path: str,
    preview_body: dict[str, str],
    apply_path: str,
    expected_digest: str,
) -> None:
    base = f"http://127.0.0.1:{server.server_port}"
    preview = Request(
        f"{base}{preview_path}",
        data=json.dumps(preview_body).encode(),
        method="POST",
    )
    with urlopen(preview) as response:
        assert response.status == 200
        assert json.loads(response.read())["plan_digest"] == expected_digest

    apply = Request(
        f"{base}{apply_path}",
        data=json.dumps(
            {
                "plan_digest": "c" * 64,
                "request_key": "30000000-0000-4000-8000-000000000001",
            }
        ).encode(),
        method="POST",
    )
    with pytest.raises(HTTPError) as error:
        urlopen(apply)

    assert error.value.code == 409
    assert expected_digest != "c" * 64
    assert "does not match preview" in json.loads(error.value.read())["detail"]


def test_fake_run_authority_replays_only_exact_previewed_alias_and_digest(
    server: SliceServer,
) -> None:
    base = f"http://127.0.0.1:{server.server_port}"
    installation_id = "20000000-0000-4000-8000-000000000003"
    request_key = "30000000-0000-4000-8000-000000000009"

    def post(path: str, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        request = Request(
            f"{base}{path}",
            data=json.dumps(body).encode(),
            method="POST",
        )
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    status, preview_a = post(
        "/api/v1/recipes/run-plans/preview",
        {"installation_id": installation_id, "alias": server.slug},
    )
    assert status == 200
    body_a = {
        "installation_id": installation_id,
        "alias": server.slug,
        "plan_digest": preview_a["plan_digest"],
        "request_key": request_key,
    }
    first_status, first = post("/api/v1/recipes/runs", body_a)
    replay_status, replayed = post("/api/v1/recipes/runs", body_a)

    assert first_status == replay_status == 202
    assert replayed == first
    assert server.run_creation_attempts == 1

    mismatched_status, mismatched = post(
        "/api/v1/recipes/runs",
        {**body_a, "alias": "different-alias"},
    )
    assert mismatched_status == 409
    assert mismatched["detail"] == "submitted plan digest does not match preview"
    assert server.run_creation_attempts == 1

    server.run_plan_digest = "e" * 64
    status, preview_b = post(
        "/api/v1/recipes/run-plans/preview",
        {"installation_id": installation_id, "alias": "different-alias"},
    )
    assert status == 200
    conflict_status, conflict = post(
        "/api/v1/recipes/runs",
        {
            "installation_id": installation_id,
            "alias": "different-alias",
            "plan_digest": preview_b["plan_digest"],
            "request_key": request_key,
        },
    )
    assert conflict_status == 409
    assert conflict["detail"] == "request key was already used differently"
    assert server.run_creation_attempts == 1


def test_runner_accepts_litellm_empty_provider_metadata(
    tmp_path: Path, server: SliceServer
) -> None:
    server.add_empty_provider_metadata = True

    result, evidence_path = _run(tmp_path, server, "--stop-after", "inference-ok")

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == STATES[:9]


def test_runner_retries_transient_stale_install_inventory(
    tmp_path: Path, server: SliceServer
) -> None:
    server.install_preview_blockers = ["install.stale_inventory"]

    result, evidence_path = _run(tmp_path, server, "--stop-after", "installed")

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == STATES[:6]
    assert (
        sum(
            path == "/api/v1/recipes/install-plans/preview"
            for method, path, _body in server.request_bodies
            if method == "POST"
        )
        == 2
    )
    assert (
        sum(
            path == "/api/v1/recipes/installations"
            for method, path, _body in server.request_bodies
            if method == "POST"
        )
        == 1
    )


def test_runner_does_not_retry_permanent_install_blocker(
    tmp_path: Path, server: SliceServer
) -> None:
    server.install_preview_blockers = ["install.insufficient_disk"]

    result, _evidence_path = _run(tmp_path, server, "--stop-after", "installed")

    assert result.returncode == 1
    assert "recipe installation is not admitted" in result.stderr
    assert (
        sum(
            path == "/api/v1/recipes/install-plans/preview"
            for method, path, _body in server.request_bodies
            if method == "POST"
        )
        == 1
    )
    assert not any(
        path == "/api/v1/recipes/installations"
        for method, path, _body in server.request_bodies
        if method == "POST"
    )


def test_runner_recovers_once_after_cleaned_failed_start(
    tmp_path: Path, server: SliceServer
) -> None:
    server.start_operation_states = ["failed", "succeeded"]

    result, evidence_path = _run(tmp_path, server, "--stop-after", "inference-ok")

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == STATES[:9]
    assert evidence["outputs"]["failed_run_id"] == (
        "20000000-0000-4000-8001-000000000001"
    )
    assert evidence["outputs"]["failed_run_operation_id"] == (
        "40000000-0000-4000-8000-000000000004"
    )
    assert evidence["outputs"]["run_id"] == ("20000000-0000-4000-8001-000000000002")
    run_creations = [
        json.loads(body)
        for method, path, body in server.request_bodies
        if method == "POST" and path == "/api/v1/recipes/runs"
    ]
    run_previews = [
        json.loads(body)
        for method, path, body in server.request_bodies
        if method == "POST" and path == "/api/v1/recipes/run-plans/preview"
    ]
    assert len(run_creations) == 2
    assert [
        (request["installation_id"], request["alias"])
        for request in run_previews
    ] == [
        (request["installation_id"], request["alias"])
        for request in run_creations
    ]
    assert {request["alias"] for request in run_creations} == {server.slug}
    expected_retry_key = str(
        uuid.uuid5(uuid.UUID(evidence["acceptance_id"]), "running:start-retry")
    )
    assert run_creations[1]["request_key"] == expected_retry_key
    assert (
        sum(
            path == "/api/v1/recipes/run-plans/preview"
            for method, path, _body in server.request_bodies
            if method == "POST"
        )
        == 2
    )
    assert not any(
        path.endswith("/retry") for _method, path, _body in server.request_bodies
    )


def test_runner_does_not_retry_a_failed_replacement_start(
    tmp_path: Path, server: SliceServer
) -> None:
    server.start_operation_states = ["failed", "failed", "succeeded"]

    result, evidence_path = _run(tmp_path, server, "--stop-after", "inference-ok")

    assert result.returncode == 1
    assert "recipe start operation ended in failed" in result.stderr
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == STATES[:6]
    assert "run_id" not in evidence["outputs"]
    run_creations = [
        json.loads(body)
        for method, path, body in server.request_bodies
        if method == "POST" and path == "/api/v1/recipes/runs"
    ]
    assert len(run_creations) == 2
    assert not any(
        path.endswith("/retry") for _method, path, _body in server.request_bodies
    )


def test_runner_resumes_checkpointed_starts_without_repreviewing(
    tmp_path: Path, server: SliceServer
) -> None:
    initial_operation = (
        "/api/v1/recipes/operations/40000000-0000-4000-8000-000000000004"
    )
    replacement_operation = (
        "/api/v1/recipes/operations/40000000-0000-4000-8000-000000000005"
    )
    server.start_operation_states = ["failed", "succeeded"]
    server.fail_once_path = initial_operation

    first, evidence_path = _run(tmp_path, server, "--stop-after", "inference-ok")

    assert first.returncode == 1
    after_first = json.loads(evidence_path.read_text())
    assert after_first["completed_states"] == STATES[:6]
    assert after_first["outputs"]["start_plan_digest"] == "f" * 64
    assert (
        after_first["outputs"]["start_operation_id"]
        == initial_operation.rsplit("/", 1)[-1]
    )
    assert after_first["outputs"]["start_run_id"] == (
        "20000000-0000-4000-8001-000000000001"
    )

    server.run_plan_digest = "e" * 64
    server.fail_once_path = replacement_operation
    second, _ = _run(tmp_path, server, "--stop-after", "inference-ok")

    assert second.returncode == 1
    after_second = json.loads(evidence_path.read_text())
    assert after_second["completed_states"] == STATES[:6]
    assert "start_retry_plan_digest" in after_second["outputs"], second.stderr
    assert after_second["outputs"]["start_retry_plan_digest"] == "e" * 64
    assert (
        after_second["outputs"]["start_retry_operation_id"]
        == (replacement_operation.rsplit("/", 1)[-1])
    )
    assert after_second["outputs"]["start_retry_run_id"] == (
        "20000000-0000-4000-8001-000000000002"
    )

    server.run_plan_digest = "d" * 64
    third, _ = _run(tmp_path, server, "--stop-after", "inference-ok")

    assert third.returncode == 0, third.stderr
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == STATES[:9]
    run_previews = [
        json.loads(body)
        for method, path, body in server.request_bodies
        if method == "POST" and path == "/api/v1/recipes/run-plans/preview"
    ]
    run_creations = [
        json.loads(body)
        for method, path, body in server.request_bodies
        if method == "POST" and path == "/api/v1/recipes/runs"
    ]
    assert len(run_previews) == 2
    assert [request["alias"] for request in run_previews] == [server.slug] * 2
    assert [request["alias"] for request in run_creations] == [server.slug] * 2
    assert [creation["plan_digest"] for creation in run_creations] == [
        "f" * 64,
        "e" * 64,
    ]
    assert len(run_creations) == 2


@pytest.mark.parametrize("purpose", ("start", "start-retry"))
@pytest.mark.parametrize("committed", (False, True))
def test_runner_recovers_interrupted_start_creation(
    tmp_path: Path,
    server: SliceServer,
    purpose: str,
    committed: bool,
) -> None:
    creation_number = 1 if purpose == "start" else 2
    server.start_operation_states = (
        ["succeeded"] if purpose == "start" else ["failed", "succeeded"]
    )
    server.interrupt_run_creation_number = creation_number
    server.commit_interrupted_run_creation = committed

    first, evidence_path = _run(tmp_path, server, "--stop-after", "inference-ok")

    assert first.returncode == 1
    after_first = json.loads(evidence_path.read_text())
    prefix = "start" if purpose == "start" else "start_retry"
    assert after_first["outputs"][f"{prefix}_plan_digest"] == "f" * 64
    assert f"{prefix}_operation_id" not in after_first["outputs"]
    assert f"{prefix}_run_id" not in after_first["outputs"]

    server.run_plan_digest = "e" * 64
    second, _ = _run(tmp_path, server, "--stop-after", "inference-ok")

    assert second.returncode == 0, second.stderr
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == STATES[:9]
    expected_digest = "f" * 64 if committed else "e" * 64
    assert evidence["outputs"][f"{prefix}_plan_digest"] == expected_digest
    run_previews = [
        json.loads(body)
        for method, path, body in server.request_bodies
        if method == "POST" and path == "/api/v1/recipes/run-plans/preview"
    ]
    baseline_previews = 1 if purpose == "start" else 2
    assert len(run_previews) == baseline_previews + (0 if committed else 1)
    assert {request["alias"] for request in run_previews} == {server.slug}
    request_key = str(
        uuid.uuid5(uuid.UUID(evidence["acceptance_id"]), f"running:{purpose}")
    )
    matching_creations = [
        json.loads(body)
        for method, path, body in server.request_bodies
        if method == "POST"
        and path == "/api/v1/recipes/runs"
        and json.loads(body)["request_key"] == request_key
    ]
    assert {creation["alias"] for creation in matching_creations} == {server.slug}
    assert [creation["plan_digest"] for creation in matching_creations] == (
        ["f" * 64, "f" * 64] if committed else ["f" * 64, "f" * 64, "e" * 64]
    )


def test_runner_accepts_documented_maintenance_route_withdrawal(
    tmp_path: Path, server: SliceServer
) -> None:
    server.withdrawn_endpoint_status = 503

    result, evidence_path = _run(tmp_path, server, "--stop-after", "route-withdrawn")

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == STATES[:11]


def test_model_multinode_runner_proves_failure_recovery_restart_and_cleanup(
    tmp_path: Path, server: SliceServer
) -> None:
    server.nodes = [NODE, NODE_2]

    initial, evidence_path = _run_model(
        tmp_path, server, "--stop-after", "inference-ok"
    )
    assert initial.returncode == 0, initial.stderr

    server.rank_states[NODE_2] = "failed"
    server.route_published = False
    failed, _ = _run_model(
        tmp_path, server, "--stop-after", "route-withdrawn-after-failure"
    )
    assert failed.returncode == 0, failed.stderr

    server.rank_states[NODE_2] = "running"
    server.last_seen = {
        NODE: "2026-08-11T10:01:00+00:00",
        NODE_2: "2026-08-11T10:01:00+00:00",
    }
    server.fleet_digest = "c" * 64
    server.route_published = True
    recovered, _ = _run_model(tmp_path, server, "--stop-after", "inference-recovered")
    assert recovered.returncode == 0, recovered.stderr

    server.last_seen = {
        NODE: "2026-08-11T10:02:00+00:00",
        NODE_2: "2026-08-11T10:02:00+00:00",
    }
    server.fleet_digest = "d" * 64
    completed, _ = _run_model(tmp_path, server)

    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == list(MODEL_MULTINODE_STATES)
    assert evidence["failure_node"] == NODE_2
    assert evidence["outputs"]["restart_fleet_evidence_digest"] == "d" * 64
    assert (
        evidence["qualification_sha256"]
        == hashlib.sha256((tmp_path / "qualification.json").read_bytes()).hexdigest()
    )
    assert server.route_published is False
    assert any(
        path == "/api/v1/endpoints/deepseek-v4-flash-0731-mia-dual"
        for _method, path, _authorization in server.requests
    )


def test_model_inference_allows_a_bounded_reasoning_budget(
    tmp_path: Path, server: SliceServer
) -> None:
    server.nodes = [NODE, NODE_2]

    result, _evidence_path = _run_model(
        tmp_path, server, "--stop-after", "inference-ok"
    )

    assert result.returncode == 0, result.stderr
    inference_requests = [
        json.loads(body)
        for method, path, body in server.request_bodies
        if method == "POST" and path == "/v1/chat/completions"
    ]
    assert len(inference_requests) == 1
    assert inference_requests[0]["max_tokens"] == 128


def test_model_runner_requires_exact_private_qualification(
    tmp_path: Path, server: SliceServer
) -> None:
    server.nodes = [NODE, NODE_2]
    qualification = _qualification(
        tmp_path / "qualification.json",
        "deepseek-v4-flash-0731-mia-dual.json",
    )
    document = json.loads(qualification.read_text())
    document["recipe"] = "wrong-recipe.json"
    qualification.write_bytes(_canonical(document))

    result, evidence = _run_model(tmp_path, server, qualification=qualification)

    assert result.returncode == 1
    assert result.stderr.strip() == (
        "development slice failed: model qualification does not match this phase"
    )
    assert not evidence.exists()


def test_model_runner_rejects_cross_node_artifact_identity_mismatch(
    tmp_path: Path, server: SliceServer
) -> None:
    server.nodes = [NODE, NODE_2]
    server.artifact_set_digests[NODE_2] = "6" * 64

    result, evidence_path = _run_model(tmp_path, server, "--stop-after", "running")

    assert result.returncode == 1
    assert result.stderr.strip() == (
        "development slice failed: runtime artifacts differ between target nodes"
    )
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == MODEL_MULTINODE_STATES[:6]


@pytest.mark.parametrize("completed_count", range(len(STATES)))
def test_runner_resumes_after_every_completed_gate(
    tmp_path: Path, server: SliceServer, completed_count: int
) -> None:
    first, evidence_path = _run(
        tmp_path,
        server,
        "--stop-after",
        STATES[completed_count],
    )
    assert first.returncode == 0, first.stderr
    before = json.loads(evidence_path.read_text())
    assert before["completed_states"] == STATES[: completed_count + 1]

    second, _ = _run(tmp_path, server)

    assert second.returncode == 0, second.stderr
    assert json.loads(evidence_path.read_text())["completed_states"] == STATES


def test_runner_refuses_to_advance_past_failed_gate_and_redacts_errors(
    tmp_path: Path, server: SliceServer
) -> None:
    server.fail_path = "/api/v1/recipes/source-checks"

    result, evidence_path = _run(tmp_path, server)

    assert result.returncode == 1
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == STATES[:3]
    assert "image-built" not in evidence["completed_states"]
    assert ADMIN_TOKEN not in result.stderr
    assert INFERENCE_TOKEN not in result.stderr
    assert "Authorization" not in result.stderr


@pytest.mark.parametrize(
    "terminal_state", ("failed", "waiting-for-operator", "expired")
)
def test_runner_retries_every_terminal_build_operation_once(
    tmp_path: Path, server: SliceServer, terminal_state: str
) -> None:
    server.build_operation_state = terminal_state

    result, evidence_path = _run(tmp_path, server)

    assert result.returncode == 0, result.stderr
    assert json.loads(evidence_path.read_text())["completed_states"] == STATES
    assert (
        sum(
            path.endswith("/retry") for _method, path, _authorization in server.requests
        )
        == 1
    )


def test_runner_stops_after_one_terminal_build_retry(
    tmp_path: Path, server: SliceServer
) -> None:
    server.build_operation_state = "waiting-for-operator"
    server.retry_operation_state = "failed"

    result, evidence_path = _run(tmp_path, server)

    assert result.returncode == 1
    assert result.stderr.strip() == (
        "development slice failed: recipe build operation ended in failed"
    )
    assert json.loads(evidence_path.read_text())["completed_states"] == STATES[:3]


def test_runner_retries_one_terminal_image_distribution(
    tmp_path: Path, server: SliceServer
) -> None:
    server.distribution_operation_state = "failed"

    result, evidence_path = _run(tmp_path, server)

    assert result.returncode == 0, result.stderr
    assert sum(path.endswith("/retry") for _, path, _ in server.requests) == 1
    evidence = json.loads(evidence_path.read_text())
    expected_retry_key = str(
        uuid.uuid5(
            uuid.UUID(evidence["acceptance_id"]),
            "image-distributed:distribution-retry",
        )
    )
    retry_bodies = [
        json.loads(body)
        for method, path, body in server.request_bodies
        if method == "POST" and path.endswith("/retry")
    ]
    assert retry_bodies == [{"request_key": expected_retry_key}]
    assert evidence["completed_states"] == STATES


def test_runner_stops_after_one_terminal_distribution_retry(
    tmp_path: Path, server: SliceServer
) -> None:
    server.distribution_operation_state = "failed"
    server.retry_operation_state = "failed"

    result, evidence_path = _run(tmp_path, server)

    assert result.returncode == 1
    assert "image distribution operation ended in failed" in result.stderr
    assert json.loads(evidence_path.read_text())["completed_states"] == STATES[:4]


def test_runner_retries_one_failed_installation(
    tmp_path: Path, server: SliceServer
) -> None:
    server.install_operation_state = "failed"

    result, evidence_path = _run(tmp_path, server)

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text())
    expected_retry_key = str(
        uuid.uuid5(uuid.UUID(evidence["acceptance_id"]), "installed:install-retry")
    )
    retry_bodies = [
        json.loads(body)
        for method, path, body in server.request_bodies
        if method == "POST" and path.endswith("/retry")
    ]
    assert retry_bodies == [{"request_key": expected_retry_key}]
    assert evidence["completed_states"] == STATES


def test_runner_stops_after_one_failed_installation_retry(
    tmp_path: Path, server: SliceServer
) -> None:
    server.install_operation_state = "failed"
    server.retry_operation_state = "failed"

    result, evidence_path = _run(tmp_path, server)

    assert result.returncode == 1
    assert "recipe installation operation ended in failed" in result.stderr
    assert sum(path.endswith("/retry") for _, path, _ in server.requests) == 1
    assert json.loads(evidence_path.read_text())["completed_states"] == STATES[:5]


@pytest.mark.parametrize("terminal_state", ("waiting-for-operator", "expired"))
def test_runner_does_not_retry_nonfailed_installation(
    tmp_path: Path, server: SliceServer, terminal_state: str
) -> None:
    server.install_operation_state = terminal_state

    result, evidence_path = _run(tmp_path, server)

    assert result.returncode == 1
    assert f"recipe installation operation ended in {terminal_state}" in result.stderr
    assert not any(path.endswith("/retry") for _, path, _ in server.requests)
    assert json.loads(evidence_path.read_text())["completed_states"] == STATES[:5]


@pytest.mark.parametrize("failure", ("stale", "missing-build", "missing-runtime"))
def test_runner_requires_fresh_spark_runtime_inventory(
    tmp_path: Path, server: SliceServer, failure: str
) -> None:
    if failure == "stale":
        server.inventory_stale[NODE] = True
    elif failure == "missing-build":
        server.inventory_capabilities[NODE] = [
            "recipe.operations.v1",
            "runtime.spark-docker-nvidia.v1",
        ]
    else:
        server.inventory_capabilities[NODE] = ["recipe.operations.v1"]

    result, evidence_path = _run(tmp_path, server)

    assert result.returncode == 1
    assert "required development inventory is not ready" in result.stderr
    assert json.loads(evidence_path.read_text())["completed_states"] == []


def test_runner_revises_an_existing_same_slug_recipe_with_different_content(
    tmp_path: Path, server: SliceServer
) -> None:
    server.recipe_created = True
    server.recipe_digest = "9" * 64

    result, evidence_path = _run(tmp_path, server, "--stop-after", "recipe-resolved")

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text())
    assert evidence["completed_states"] == STATES[:2]
    assert evidence["outputs"]["recipe_revision"] == 2
    assert evidence["outputs"]["recipe_content_sha256"] == RECIPE_DIGEST
    assert (
        "PUT",
        "/api/v1/catalog/recipes/10000000-0000-4000-8000-000000000001/draft",
        f"Bearer {ADMIN_TOKEN}",
    ) in server.requests


def test_runner_rejects_plain_http_to_a_non_loopback_host(tmp_path: Path) -> None:
    admin = _token(tmp_path / "admin", ADMIN_TOKEN)
    inference = _token(tmp_path / "inference", INFERENCE_TOKEN)

    result = subprocess.run(
        (
            sys.executable,
            str(RUNNER),
            "--api-base",
            "http://192.0.2.10:8080",
            "--admin-token-file",
            str(admin),
            "--inference-token-file",
            str(inference),
            "--phase",
            "synthetic",
            "--builder-node",
            NODE,
            "--target-node",
            NODE,
            "--evidence-file",
            str(tmp_path / "evidence.json"),
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "plain HTTP requires an explicit loopback address" in result.stderr
    assert not (tmp_path / "evidence.json").exists()


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "permissive"])
def test_runner_rejects_unsafe_token_files(
    tmp_path: Path, server: SliceServer, unsafe: str
) -> None:
    admin = _token(tmp_path / "admin", ADMIN_TOKEN)
    inference = _token(tmp_path / "inference", INFERENCE_TOKEN)
    if unsafe == "symlink":
        admin_link = tmp_path / "admin-link"
        admin_link.symlink_to(admin)
        admin = admin_link
    elif unsafe == "hardlink":
        admin_link = tmp_path / "admin-link"
        os.link(admin, admin_link)
    else:
        admin.chmod(0o644)

    result = subprocess.run(
        (
            sys.executable,
            str(RUNNER),
            "--api-base",
            f"http://127.0.0.1:{server.server_port}",
            "--admin-token-file",
            str(admin),
            "--inference-token-file",
            str(inference),
            "--phase",
            "synthetic",
            "--builder-node",
            NODE,
            "--target-node",
            NODE,
            "--evidence-file",
            str(tmp_path / "evidence.json"),
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert ADMIN_TOKEN not in result.stderr
    assert INFERENCE_TOKEN not in result.stderr
    assert not (tmp_path / "evidence.json").exists()
