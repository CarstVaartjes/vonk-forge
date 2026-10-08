"""Terminal presentation for review responses."""

from __future__ import annotations

from collections.abc import Mapping

from .common import (
    _actions,
    _bytes,
    _field,
    _headroom,
    _object,
    _optional,
    _reasons,
    _records,
    _text,
    _time,
    _words,
)
from .library import _option_choices


def _preview(payload: Mapping[str, object]) -> None:
    if payload.get("allowed") is True:
        print("Ready for review")
    elif payload.get("waits_for_preparation") is True:
        print("Ready after preparation")
        print("The Controller prepares what is listed below, then continues.")
    else:
        print("Blocked")
    _field("Profile", payload.get("profile_name"))
    _field("Revision", payload.get("profile_revision"))
    _field("Plan digest", payload.get("plan_digest"))
    scope = _object(payload.get("scope"), "scope")
    _field("All Sparks", _words(scope.get("node_ids")))
    _field("Idle Sparks", _words(scope.get("idle_node_ids")))
    for key, value in _object(payload.get("summary"), "summary").items():
        _field(key.replace("_", " ").title(), value)
    for assignment in _records(payload, "assignments"):
        _field("Recipe", assignment.get("recipe_title"))
        _field("Sparks", _words(assignment.get("node_ids")))
        _field("Current", assignment.get("current_state"))
        _field("Desired", assignment.get("desired_state"))
        _option_choices(assignment.get("option_choices"))
        _reasons(assignment.get("reasons"), subject=assignment.get("recipe_title"))
    for step in (
        _records(payload, "preparation_steps") if "preparation_steps" in payload else []
    ):
        print(f"Prepare {_text(step.get('index'))}. {_text(step.get('label'))}")
        _field("Affected Sparks", _words(step.get("node_ids")))
    for step in _records(payload, "steps"):
        print(f"{_text(step.get('index'))}. {_text(step.get('label'))}")
        _field("Affected Sparks", _words(step.get("node_ids")))
    effects = _object(payload.get("effects"), "effects")
    for run in _records(effects, "runs"):
        _field(f"{_text(run.get('action')).title()} endpoint", run.get("alias"))
        _field("Run", run.get("run_id"))
        _field("Complete group", _words(run.get("node_ids")))
    for installation in _records(effects, "installations"):
        _field(
            f"{_text(installation.get('action')).title()} installation",
            installation.get("installation_id"),
        )
        _field("Complete group", _words(installation.get("node_ids")))
    for continuing in _records(effects, "adopted") if "adopted" in effects else []:
        _field("Continue application", continuing.get("application_id"))
        _field("Complete group", _words(continuing.get("node_ids")))
        _field("Bound plan", continuing.get("plan_digest"))
    for pending in _records(effects, "superseded"):
        _field(f"Supersede {_text(pending.get('kind'))}", pending.get("id"))
        _field("Complete group", _words(pending.get("node_ids")))
    for item in _records(payload, "preparation_decisions"):
        _field("Assignment", item.get("assignment_id"))
        model = _object(item.get("model"), "model identity")
        runtime = _object(item.get("runtime_image"), "runtime identity")
        _field("Exact model set", model.get("artifact_set_sha256"))
        _field("Exact image", runtime.get("image_digest"))
        _field("Image manifest", runtime.get("oci_layout_sha256"))
        _field("Image size", _bytes(runtime.get("image_bytes")))
        _field("Architecture", runtime.get("architecture"))
        _field("Runtime interface", runtime.get("runtime_interface"))
        build_id = runtime.get("build_id")
        _field(
            "Build",
            "published image" if build_id is None else build_id,
        )
        _field("Reuse model on", _words(item.get("model_reuse_node_ids")))
        _field("Reuse image on", _words(item.get("image_reuse_node_ids")))
    for item in _records(payload, "assessments"):
        assessment = _object(item.get("assessment"), "workload assessment")
        _field("Admission for assignment", item.get("assignment_id"))
        _field("Intended endpoint", assessment.get("alias"))
        for label, key in (
            ("Current capacity", "fit_current"),
            ("Capacity after stops", "fit_after_stop"),
        ):
            fit = _optional(assessment.get(key), key)
            if not fit:
                continue
            _field(label, "fits" if fit.get("allowed") is True else "blocked")
            for node in _records(fit, "nodes"):
                node_id = node.get("node_id")
                _field("Spark", node_id)
                _field("Ports required", _words(node.get("ports_required")))
                _field("Memory demand kind", node.get("memory_kind"))
                _field("Physical memory pool", node.get("memory_pool"))
                _field("Memory required", _bytes(node.get("memory_required_bytes")))
                _field("Memory reserve", _bytes(node.get("memory_floor_bytes")))
                _field(
                    "Physical memory capacity",
                    _bytes(node.get("memory_capacity_bytes")),
                )
                _field(
                    "Available in limiting pool",
                    _bytes(node.get("memory_available_bytes")),
                )
                _field(
                    "Memory after placement",
                    _headroom(node.get("memory_free_after_bytes")),
                )
                uncertainty = _optional(
                    node.get("memory_usage_uncertainty"), "memory usage uncertainty"
                )
                if uncertainty:
                    _field(
                        "Resident usage evidence",
                        "Per-run usage is unavailable; admission uses each full residual upper bound.",
                    )
                    _field("Capacity source", uncertainty.get("source"))
                    _field(
                        "Inventory sample",
                        uncertainty.get("inventory_observed_at"),
                    )
                    _field(
                        "Inventory evidence digest",
                        uncertainty.get("inventory_evidence_digest"),
                    )
                    for residual in _records(uncertainty, "residual_ranges"):
                        _field(
                            "Possible run residual",
                            f"{residual.get('run_id')} generation "
                            f"{residual.get('run_generation')}: 0–"
                            f"{_bytes(residual.get('maximum_bytes'))} (not measured)",
                        )
                _field("Disk required", _bytes(node.get("disk_required_bytes")))
                _field(
                    "Disk after placement", _headroom(node.get("disk_free_after_bytes"))
                )
                for reason_kind, label in (
                    ("blockers", "blocker"),
                    ("warnings", "warning"),
                ):
                    _reasons(
                        node.get(reason_kind),
                        subject=f"{_text(node_id)} capacity {label}",
                    )
        condition = _optional(
            assessment.get("post_stop_memory_check"), "post-stop memory check"
        )
        if condition:
            _field(
                "Conditional memory fit",
                "stop the reviewed workloads, then recheck fresh capacity before preparing or starting",
            )
            _field("Required stops", _words(condition.get("stop_run_ids")))
        if assessment.get("stop_before_prepare") is True:
            print("Stop the reviewed workloads before preparing the replacement.")
        if assessment.get("stop_before_transfer") is True:
            print("Stop the reviewed workloads before transferring the replacement.")
        _reasons(assessment.get("blockers"))
        _reasons(assessment.get("warnings"))
    for item in _records(payload, "preparations"):
        preparation = _object(item.get("preparation"), "preparation")
        _field("Preparation", item.get("assignment_id"))
        _field("Controller ready", preparation.get("controller_ready"))
        _field("Targets ready", preparation.get("targets_ready"))
    _reasons(payload.get("reasons"))


def _cache_removal_review(payload: Mapping[str, object]) -> None:
    """Present the owning cache policy's removal review without interpretation."""
    _field("Action", payload.get("action"))
    _field("Resource", payload.get("resource_kind"))
    _field("Selector", payload.get("selector"))
    _field("Target identity", payload.get("target_identity"))
    if payload.get("with_model") is not None:
        _field("Remove dependent model", payload.get("with_model"))
    _field("Review digest", payload.get("review_digest"))
    _field("Observed", _time(payload.get("observed_at")))

    assets = _records(payload, "assets")
    if not assets:
        print("Assets: none")
    for asset in assets:
        print("Asset:")
        _field("  Kind", asset.get("kind"))
        _field("  SHA-256", asset.get("sha256"))
        _field("  Disposition", asset.get("disposition"))
        _field("  Readiness", asset.get("availability"))
        _field("  Expected bytes", _bytes(asset.get("expected_bytes")))
        _field("  Available bytes", _bytes(asset.get("available_bytes")))

    for field, heading in (
        ("references", "Saved references"),
        ("active_work", "Active work"),
    ):
        records = _records(payload, field)
        if not records:
            print(f"{heading}: none")
        for record in records:
            print(f"{heading}:")
            _field("  Classification", record.get("classification"))
            _field(
                "  Asset",
                f"{_text(record.get('asset_kind'))} "
                f"{_text(record.get('asset_sha256'))}",
            )
            _field(
                "  Owner",
                f"{_text(record.get('owner_kind'))} {_text(record.get('owner_id'))}",
            )
            _field("  State", record.get("state"))
            if record.get("detail") is not None:
                _field("  Detail", record.get("detail"))
            if record.get("reason") is not None:
                _field("  Reason", record.get("reason"))

    blockers = _records(payload, "blockers")
    if not blockers:
        print("Blockers: none")
    for blocker in blockers:
        _field(
            "Blocker",
            f"{_text(blocker.get('code'))}: {_text(blocker.get('detail'))}",
        )
        _field("  Retryable", blocker.get("retryable"))
        _actions(blocker.get("recovery_actions"))
