"""Translate parsed WorkloadRun sources into safe, explainable local drafts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass

from .import_report import (
    ImportDisposition,
    ImportReportBuilder,
    ImportReportItem,
)
from .runtime_compilers import RuntimeCompileError, RuntimeProjection, compile_runtime
from .source_bundles import GeneratedSourceBundle, generate_source_bundle
from .workload_run_source import WorkloadRunSource

_SENSITIVE = re.compile(
    r"(?:^|_)(?:authorization|credential|password|secret|token|private_key|certificate)(?:$|_)",
    re.IGNORECASE,
)
_DIGEST_IMAGE = re.compile(r"^[^\s]+@sha256:[0-9a-f]{64}$")
_MUTABLE_REVISIONS = frozenset({"main", "master", "latest", "head"})


@dataclass(frozen=True, slots=True)
class WorkloadRunImportResult:
    draft_document: dict[str, object]
    bundle: GeneratedSourceBundle
    report: tuple[ImportReportItem, ...]
    source_sha256: str
    report_digest: str
    redacted_source: dict[str, object]
    runnable: bool


def import_workload_run(source: WorkloadRunSource) -> WorkloadRunImportResult:
    builder = ImportReportBuilder(source.leaf_paths())
    projection: RuntimeProjection | None = None
    compiler_error: str | None = None
    try:
        projection = compile_runtime(source, builder)
    except RuntimeCompileError as error:
        compiler_error = str(error)[:240]
    for path in source.leaf_paths():
        _classify(source, builder, path, compiler_error=compiler_error)
    builder.record(
        "/@missing/resources",
        ImportDisposition.OVERLAY_REQUIRED,
        None,
        "resources.overlay_required",
        "WorkloadRun does not declare a complete download, install, staging, resident-memory, and activation-memory envelope. Enter measured or verified byte values.",
        True,
    )
    builder.record(
        "/@missing/security",
        ImportDisposition.OVERLAY_REQUIRED,
        None,
        "security.overlay_required",
        "Confirm the unprivileged GPU device and read-only model mount policy before this recipe can run.",
        True,
    )
    if (source.min_nodes or 1) > 1:
        builder.record(
            "/@missing/topology-fabric",
            ImportDisposition.OVERLAY_REQUIRED,
            None,
            "topology.fabric_required",
            "Multi-node imports require explicit ranks, transport, and minimum fabric bandwidth.",
            True,
        )
    report = builder.finalize()
    report_document = [
        {**asdict(item), "disposition": item.disposition.value} for item in report
    ]
    report_digest = hashlib.sha256(
        json.dumps(report_document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    runnable = not any(
        item.disposition
        in {
            ImportDisposition.RESOLUTION_REQUIRED,
            ImportDisposition.OVERLAY_REQUIRED,
            ImportDisposition.UNSUPPORTED_BLOCKING,
        }
        or item.blocking
        for item in report
    )
    bundle = _bundle(source)
    return WorkloadRunImportResult(
        draft_document=_draft(source, projection, bundle),
        bundle=bundle,
        report=report,
        source_sha256=source.source_sha256,
        report_digest=report_digest,
        redacted_source=_redact(source.document),
        runnable=runnable,
    )


def _classify(
    source: WorkloadRunSource,
    builder: ImportReportBuilder,
    path: str,
    *,
    compiler_error: str | None,
) -> None:
    top = path.split("/", 2)[1] if path.startswith("/") else ""
    destination: str | None = None
    if top == "recipe_version":
        disposition, reason, detail, blocking = (
            ImportDisposition.DROPPED_REDUNDANT,
            "schema.normalized",
            "The source schema marker is replaced by Vonk recipe schema version 1.",
            False,
        )
    elif top == "model":
        disposition, destination, reason, detail, blocking = (
            ImportDisposition.IMPORTED,
            "/artifacts/0/repository",
            "artifact.repository",
            "The model repository is imported as an external artifact identity.",
            False,
        )
    elif top == "model_revision":
        mutable = (
            source.model_revision is None
            or source.model_revision.lower() in _MUTABLE_REVISIONS
        )
        disposition = (
            ImportDisposition.RESOLUTION_REQUIRED
            if mutable
            else ImportDisposition.IMPORTED
        )
        destination, reason, detail, blocking = (
            "/artifacts/0/revision",
            "artifact.revision",
            "The model revision must resolve to an immutable provider revision."
            if mutable
            else "The immutable model revision is imported.",
            mutable,
        )
    elif top == "runtime":
        disposition, destination, reason, detail, blocking = (
            ImportDisposition.TRANSFORMED,
            "/runtime/adapter",
            "runtime.adapter",
            "The WorkloadRun runtime name is normalized to a typed Vonk runtime adapter.",
            False,
        )
    elif top == "container":
        immutable = (
            source.container is not None
            and _DIGEST_IMAGE.fullmatch(source.container) is not None
        )
        disposition = (
            ImportDisposition.INCORPORATED
            if immutable
            else ImportDisposition.RESOLUTION_REQUIRED
        )
        destination, reason, detail, blocking = (
            "/build/context/Dockerfile/FROM",
            "build.base_image",
            "The immutable ARM64 base is incorporated into the generated Dockerfile."
            if immutable
            else "The Dockerfile base tag must resolve to a linux/arm64 manifest digest before building.",
            not immutable,
        )
    elif top in {"min_nodes", "max_nodes"}:
        disposition, destination, reason, detail, blocking = (
            ImportDisposition.TRANSFORMED,
            f"/topology/@from-{top}",
            "topology.node_bound",
            "The declared bound is converted into one exact topology.",
            False,
        )
    elif top == "metadata":
        suffix = path.removeprefix("/metadata")
        disposition, destination, reason, detail, blocking = (
            ImportDisposition.IMPORTED,
            f"/metadata{suffix}",
            "metadata.imported",
            "Recipe metadata is imported.",
            False,
        )
    elif top == "defaults":
        suffix = path.removeprefix("/defaults")
        disposition, destination, reason, detail, blocking = (
            ImportDisposition.TRANSFORMED,
            f"/runtime/arguments{suffix}",
            "runtime.default",
            "The default is available only to the typed runtime compiler.",
            False,
        )
    elif top == "command":
        if compiler_error is None:
            disposition, destination, reason, detail, blocking = (
                ImportDisposition.TRANSFORMED,
                "/runtime/arguments",
                "runtime.command",
                "The command was parsed as an allowlisted runtime grammar; it was never executed as shell text.",
                False,
            )
        else:
            disposition, reason, detail, blocking = (
                ImportDisposition.UNSUPPORTED_BLOCKING,
                "runtime.command_unsupported",
                f"The command cannot be represented safely: {compiler_error}",
                True,
            )
    elif top == "env":
        suffix = path.removeprefix("/env")
        if compiler_error is None:
            disposition, destination, reason, detail, blocking = (
                ImportDisposition.TRANSFORMED,
                f"/runtime/environment{suffix}",
                "runtime.environment",
                "The allowlisted environment value is imported as typed container configuration.",
                False,
            )
        else:
            disposition, destination, reason, detail, blocking = (
                ImportDisposition.RESOLUTION_REQUIRED,
                f"/runtime/environment{suffix}",
                "runtime.environment_review",
                "This literal environment setting requires runtime-specific review; secret values are never accepted.",
                True,
            )
    elif top in {"mods", "tuning"}:
        suffix = path.removeprefix(f"/{top}")
        disposition, destination, reason, detail, blocking = (
            ImportDisposition.INCORPORATED,
            f"/build/context/{top}/imported.json{suffix}",
            f"build.{top}_incorporated",
            f"WorkloadRun {top} is preserved in the generated, reviewable build context.",
            False,
        )
    elif top == "benchmark":
        disposition, reason, detail, blocking = (
            ImportDisposition.DROPPED_REDUNDANT,
            "benchmark.not_authority",
            "Benchmark claims are not treated as installation or runtime authority and are not imported.",
            False,
        )
    else:
        disposition, reason, detail, blocking = (
            ImportDisposition.UNSUPPORTED_BLOCKING,
            "workload_run.unknown_field",
            f"Unknown WorkloadRun field {top!r} is preserved in the report but cannot authorize execution.",
            True,
        )
    builder.record(path, disposition, destination, reason, detail, blocking)


def _draft(
    source: WorkloadRunSource,
    projection: RuntimeProjection | None,
    bundle: GeneratedSourceBundle,
) -> dict[str, object]:
    slug = re.sub(r"[^a-z0-9-]+", "-", source.model.rsplit("/", 1)[-1].lower()).strip(
        "-"
    )
    if len(slug) < 2:
        slug = f"model-{slug or 'import'}"
    slug = slug[:63].rstrip("-")
    minimum = source.min_nodes or 1
    maximum = source.max_nodes or minimum
    node_count = min(maximum, minimum + 63)
    family = projection.family if projection is not None else source.runtime
    adapter = re.sub(r"[^a-z0-9_]+", "_", family.lower()).strip("_") or "custom"
    entrypoints = {
        "vllm": ["vllm", "serve", "/models"],
        "sglang": ["python", "-m", "sglang.launch_server", "--model-path", "/models"],
        "llama_cpp": ["llama-server"],
    }
    return {
        "schema_version": 1,
        "identity": {"publisher": "workload-run", "slug": slug},
        "metadata": {
            "title": source.metadata.title or source.model.rsplit("/", 1)[-1],
            "description": source.metadata.description
            or f"Imported WorkloadRun recipe for {source.model}.",
            "tags": list(source.metadata.tags),
        },
        "model": _catalog_reference("model-version", f"{slug}-version"),
        "execution": {
            "harness": _catalog_reference("execution-harness", f"{adapter}-openai"),
            "patch_bundle": None,
        },
        "build": {
            "context": {
                "sha256": bundle.sha256,
                "expected_bytes": len(bundle.archive),
                "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
            },
            "dockerfile": "Dockerfile",
            "platform": "linux/arm64",
            "arguments": [],
            "network": {"mode": "none", "hosts": []},
            "resources": {
                "download_bytes": 0,
                "temporary_bytes": 1,
                "memory_bytes": 1,
                "timeout_seconds": 3600,
            },
        },
        "parameters": [],
        "artifacts": [
            {
                "id": "weights",
                "kind": "huggingface.snapshot",
                "repository": source.model,
                "revision": source.model_revision or "0" * 40,
                "download_bytes": 1,
                "installed_bytes": 1,
                "mount": {"target": "/models", "read_only": True},
                "roles": ["entrypoint", *(["worker"] if node_count > 1 else [])],
            }
        ],
        "runtime": {
            "distribution": _catalog_reference(
                "runtime-distribution", f"{adapter}-linux-arm64"
            ),
            "entrypoint": entrypoints.get(adapter, [adapter]),
            "arguments": projection.recipe_arguments()
            if projection is not None
            else [],
            "environment": [
                {"name": name, "value": value}
                for name, value in sorted(
                    (projection.environment if projection is not None else {}).items()
                )
            ],
            "security": {
                "devices": ["nvidia.com/gpu=all"],
                "capabilities": [],
                "host_network": False,
                "privileged": False,
                "user": "10001:10001",
                "mounts": [
                    {"source": "model", "target": "/models", "read_only": True},
                    {"source": "state", "target": "/state", "read_only": False},
                ],
            },
            "lifecycle": {"pre_start": [], "post_stop": [], "stop_timeout_seconds": 30},
        },
        "topology": _topology(node_count),
        "interfaces": [
            {
                "adapter": "openai",
                "port": int(projection.endpoint["port"])
                if projection is not None
                else 8000,
                "model_aliases": [slug],
                "health_path": str(projection.endpoint["health_path"])
                if projection is not None
                else "/v1/models",
            }
        ],
        "validation": {
            "validators": [
                {
                    "interface": "openai",
                    "checks": [
                        "container.started",
                        "endpoint.healthy",
                        "inference.completed",
                    ],
                }
            ],
            "benchmarks": [],
        },
        "provenance": {
            "source_kind": "workload_run",
            "source_reference": None,
            "attribution": [f"Imported from WorkloadRun sha256:{source.source_sha256}"],
        },
    }


def _catalog_reference(kind: str, slug: str) -> dict[str, object]:
    normalized_slug = re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-")
    normalized_slug = normalized_slug[:63].rstrip("-")
    if len(normalized_slug) < 2:
        normalized_slug = f"id-{normalized_slug or 'unknown'}"
    digest = hashlib.sha256(
        f"workload-run:{kind}:{normalized_slug}".encode()
    ).hexdigest()
    return {
        "kind": kind,
        "publisher": "workload-run",
        "slug": normalized_slug,
        "content_sha256": digest,
    }


def _topology(node_count: int) -> dict[str, object]:
    roles = [_role("entrypoint", 1, True)]
    if node_count > 1:
        roles.append(_role("worker", node_count - 1, False))
    role_names = [str(role["name"]) for role in roles]
    return {
        "name": "solo" if node_count == 1 else f"nodes_{node_count}",
        "mode": "single" if node_count == 1 else "tensor_parallel",
        "node_count": node_count,
        "parallelism": {
            "tensor": node_count,
            "pipeline": 1,
            "data": 1,
            "backend": "local" if node_count == 1 else "tcp",
        },
        "roles": roles,
        "fabric": {
            "connectivity": "none" if node_count == 1 else "connected",
            "minimum_bandwidth_mbps": 0 if node_count == 1 else 1,
        },
        "start_order": list(reversed(role_names)),
        "stop_order": role_names,
    }


def _role(name: str, count: int, endpoint_owner: bool) -> dict[str, object]:
    return {
        "name": name,
        "count": count,
        "endpoint_owner": endpoint_owner,
        "artifacts": ["weights"],
        "resources": {
            "disk": {
                "image_bytes": 1,
                "artifact_bytes": 1,
                "staging_bytes": 1,
                "cache_bytes": 0,
                "rollback_bytes": 0,
                "safety_margin_bytes": 1,
            },
            "memory": {
                "kind": "unified",
                "startup_peak_bytes": 1,
                "steady_state_bytes": 1,
                "runtime_growth_bytes": 0,
                "system_reserve_bytes": 1,
            },
        },
    }


def _bundle(source: WorkloadRunSource) -> GeneratedSourceBundle:
    files: dict[str, bytes] = {
        "Dockerfile": _dockerfile(source).encode(),
        "workload_run/source.json": json.dumps(
            _redact(source.document), sort_keys=True, separators=(",", ":")
        ).encode(),
    }
    if source.mods:
        files["mods/imported.json"] = json.dumps(
            source.mods, sort_keys=True, separators=(",", ":")
        ).encode()
    if source.tuning:
        files["tuning/imported.json"] = json.dumps(
            source.tuning, sort_keys=True, separators=(",", ":")
        ).encode()
    return generate_source_bundle(files)


def _dockerfile(source: WorkloadRunSource) -> str:
    lines = [
        f"FROM {source.container or 'scratch'}",
        'LABEL ai.vonkforge.runtime-interface="v1"',
        "COPY workload_run/ /opt/vonk/workload_run/",
    ]
    if source.mods:
        lines.append("COPY mods/ /opt/vonk/mods/")
    if source.tuning:
        lines.append("COPY tuning/ /opt/vonk/tuning/")
    lines.extend(("USER 10001:10001", ""))
    return "\n".join(lines)


def _redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if _SENSITIVE.search(key) else _redact(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact(child) for child in value]
    return copy.deepcopy(value)
