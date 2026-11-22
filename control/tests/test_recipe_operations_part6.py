from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentInstallResult,
    InstallAdmissionCode,
    LifecycleState,
    RuntimePreflightCode,
)
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_control.install_admission import (
    AdmissionReason,
    InstallAdmissionBusy,
    InstallNodePlan,
    InstallPlan,
)
from vonk_control.install_admission import (
    require_admissible as require_install_admissible,
)
from vonk_control.models import (
    AgentOperation,
    CatalogDocumentRevision,
    Job,
    RecipeBuild,
    RecipeInstallation,
    ResourceReservation,
)
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
)
from vonk_forge_contracts import (
    read_recipe,
)

from .recipe_operations_support import NOW, _CanonicalImageTransport, setup_services


def test_install_requires_reviewed_digest_and_request_keys_remain_scoped(
    tmp_path: Path,
) -> None:
    _sessions, service, _queue, mapping_id, build_id, _nodes = setup_services(tmp_path)
    plan = service.preview_install(mapping_id, build_id)
    with pytest.raises(Exception) as _ending:
        service.install(plan, plan_digest="0" * 64, actor="admin", request_id="9" * 36)
    service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="9" * 36
    )
    with pytest.raises(Exception) as _ending:
        service.stop(
            "f" * 36,
            plan_digest="0" * 64,
            actor="admin",
            request_id="9" * 36,
        )


def _blocked_install_plan(
    codes: tuple[str, ...], details: tuple[str, ...] | None = None
) -> InstallPlan:
    return InstallPlan(
        mapping_id="10000000-0000-4000-8000-000000000001",
        mapping_generation=1,
        recipe_build_id=None,
        image_digest="a" * 64,
        recipe_revision_id="20000000-0000-4000-8000-000000000002",
        recipe_content_sha256="b" * 64,
        allowed=False,
        nodes=(
            InstallNodePlan(
                node_id="spk_00000000000000000000000000000001",
                rank=0,
                role="entrypoint",
                allowed=False,
                inventory_observed_at=None,
                free_bytes=None,
                active_reserved_bytes=0,
                reused_bytes=0,
                required_download_bytes=0,
                required_bytes=0,
                required_payload_bytes=0,
                disk_floor_bytes=0,
                free_after_bytes=None,
                blockers=tuple(
                    AdmissionReason(
                        code=code,
                        detail=(
                            details[index]
                            if details is not None and index < len(details)
                            else code
                        ),
                    )
                    for index, code in enumerate(codes)
                ),
                warnings=(),
            ),
        ),
        plan_digest="c" * 64,
    )


@pytest.mark.parametrize(
    "codes",
    [
        (RuntimePreflightCode.HOST_CHANGED,),
        (RuntimePreflightCode.HOST_CHANGED, InstallAdmissionCode.INSUFFICIENT_DISK),
    ],
)
def test_prepare_installation_wait_releases_fresh_admission(
    tmp_path, monkeypatch, codes
) -> None:
    sessions, service, _queue, mapping, build, _nodes = setup_services(
        tmp_path, nodes=1
    )
    plan = service.preview_install(mapping, build)
    original = service._install_admission.plan_install
    blocked = replace(
        plan,
        allowed=False,
        nodes=tuple(
            replace(
                node,
                allowed=False,
                blockers=tuple(
                    AdmissionReason(code=code, detail="evidence unavailable")
                    for code in codes
                ),
            )
            for node in plan.nodes
        ),
    )
    monkeypatch.setattr(
        service._install_admission, "plan_install", lambda *a, **k: blocked
    )
    with pytest.raises(Exception) as _ending:
        service.prepare_installation(plan, actor="admin")
    with sessions() as session:
        assert session.scalar(select(RecipeInstallation)) is None
        assert session.scalar(select(ResourceReservation)) is None
    monkeypatch.setattr(service._install_admission, "plan_install", original)
    assert service.prepare_installation(plan, actor="admin")


def test_malformed_install_request_has_no_effect_and_fresh_valid_request_is_admitted(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, _nodes = setup_services(tmp_path)
    reviewed = service.preview_install(mapping_id, build_id)
    ended = False
    try:
        service.install(
            reviewed, plan_digest="invalid", actor="admin", request_id="malformed"
        )
    except Exception:  # noqa: BLE001 - outcome assertions cover effects and fresh admission
        ended = True
    assert ended
    with sessions() as session:
        assert (
            session.scalar(
                select(AgentOperation).where(
                    AgentOperation.kind == WireAgentOperation.RECIPE_INSTALL.value
                )
            )
            is None
        )
        assert session.scalar(select(RecipeInstallation)) is None
    fresh = service.install(
        reviewed,
        plan_digest=reviewed.plan_digest,
        actor="admin",
        request_id="fresh-valid",
    )
    assert fresh.owner_id


@pytest.mark.parametrize(
    "fault", ["missing", "malformed", "malformed-reply", "persistent"]
)
def test_installation_requests_exact_receipt_repair_before_any_agent_effect(
    tmp_path: Path,
    fault: str,
) -> None:
    from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
    from vonk_control.recipe_image_availability_contract import AvailabilityBuildReceipt
    from vonk_control.runtime_image_preparation.preparation import (
        stored_runtime_image_resolver,
    )

    sessions, service, _queue, mapping_id, build_id, _nodes = setup_services(tmp_path)
    reviewed = service.preview_install(mapping_id, build_id)
    assert reviewed.allowed, reviewed.nodes
    provider = service._install_admission._compiled_plan_provider
    assert provider is not None
    compiler = provider.__self__
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime-images")
    resolver = stored_runtime_image_resolver(storage)
    reply_damaged = [fault == "malformed-reply"]
    compiler._runtime_image_preparer = None

    def observe(*args):
        if reply_damaged[0]:
            reply_damaged[0] = False
            return None
        return resolver(*args)

    compiler._runtime_image_resolver = observe
    with sessions() as session:
        build = session.get(RecipeBuild, build_id)
        revision = session.get(CatalogDocumentRevision, reviewed.recipe_revision_id)
        assert revision is not None
        assert build is not None
        assert build.image_digest is not None
        assert build.oci_layout_sha256 is not None
        assert build.image_bytes is not None
        recipe = read_recipe(revision.document)
        evidence = AvailabilityBuildReceipt(
            state=LifecycleState.SUCCEEDED,
            build_id=build.id,
            build_input_sha256=build.build_input_sha256,
            image_digest=build.image_digest,
            oci_layout_sha256=build.oci_layout_sha256,
            image_bytes=build.image_bytes,
        )
    path = storage.root / f"{evidence.oci_layout_sha256}.receipt.json"
    if fault in ("missing", "persistent"):
        path.unlink()
    elif fault == "malformed":
        path.write_text("{")
    with sessions() as session:
        previous_jobs = set(session.scalars(select(Job.id)))
    from vonk_agent_protocol import RecipeImageCode, WaitReason
    from vonk_control.recipe_image_availability import BuildUnsettled
    from vonk_control.recipe_image_availability.contracts import OPERATION_KIND

    blocked = [fault == "persistent"]
    now = [NOW]

    def build_exact(*_args, **_kwargs):
        with sessions() as session:
            assert (
                session.scalar(
                    select(AgentOperation).where(
                        AgentOperation.kind == WireAgentOperation.RECIPE_INSTALL.value
                    )
                )
                is None
            )
        if blocked[0]:
            return BuildUnsettled(
                RecipeImageCode.BUILD_WAIT,
                "verified build observation unavailable",
                retryable=True,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        return evidence.model_dump(mode="json")

    preparation = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        transport=_CanonicalImageTransport(),
        authority=lambda *_args, **_kwargs: (
            recipe,
            {
                "architecture": "linux/arm64",
                "interface": "vonk.runtime.v1",
                "build_input_sha256": evidence.build_input_sha256,
            },
        ),
        builder=build_exact,
        clock=lambda: now[0],
    )
    service.bind_install_preparation(preparation.prepare_install)
    request_id = "receipt-repair"
    installed = None
    ended = False
    try:
        installed = service.install(
            reviewed,
            plan_digest=reviewed.plan_digest,
            actor="admin",
            request_id=request_id,
        )
    except Exception:  # noqa: BLE001 -- assert ending, effects and fresh admission
        ended = True
    with sessions() as session:
        parents = tuple(
            session.scalars(
                select(Job).where(
                    Job.id.not_in(previous_jobs),
                    Job.kind == OPERATION_KIND,
                )
            )
        )
        assert len(parents) == 1
    if fault == "persistent":
        assert ended
        assert installed is None
        with sessions() as session:
            assert (
                session.scalar(
                    select(AgentOperation).where(
                        AgentOperation.kind == WireAgentOperation.RECIPE_INSTALL.value
                    )
                )
                is None
            )
        from vonk_control.lifecycle.image_availability import CANCEL_BUDGET

        now[0] += timedelta(minutes=16)
        preparation.run_pending()
        now[0] += CANCEL_BUDGET
        preparation.run_pending()
        exhausted = preparation.get(parents[0].id)
        assert exhausted.result is None
        assert exhausted.state not in (
            LifecycleState.QUEUED,
            LifecycleState.RUNNING,
            LifecycleState.BACKOFF,
            LifecycleState.OBSERVING,
        )
        with sessions() as session:
            row = session.get(Job, parents[0].id)
            assert row is not None
            assert row.payload.get("claim_owner") is None
            assert row.payload.get("claim_until") is None
        blocked[0] = False
        request_id = "fresh-after-exhaustion"
        installed = service.install(
            reviewed,
            plan_digest=reviewed.plan_digest,
            actor="admin",
            request_id=request_id,
        )
    else:
        assert not ended
        assert preparation.get(parents[0].id).state == LifecycleState.SUCCEEDED
    assert installed is not None
    receipt = storage.read_receipt(evidence.oci_layout_sha256)
    assert receipt.image_digest == reviewed.image_digest
    assert installed.owner_id

    for node_id in _nodes:
        service.record_node_result(
            installed.id,
            node_id,
            succeeded=True,
            evidence=AgentInstallResult(installed_bytes=120).model_dump(mode="json"),
        )
    assert service.get(installed.id).state == LifecycleState.SUCCEEDED
    with sessions() as session:
        assert (
            session.scalar(
                select(AgentOperation).where(
                    AgentOperation.kind == WireAgentOperation.RECIPE_INSTALL.value
                )
            )
            is not None
        )
    assert (
        service.install(
            reviewed,
            plan_digest=reviewed.plan_digest,
            actor="admin",
            request_id=request_id,
        ).id
        == installed.id
    )


def test_a_bounded_install_blocker_keeps_the_specific_cause() -> None:
    detail = (
        "Controller-issued compiled execution plan is unavailable. "
        "compiled execution plan for spk_2818d189042b4c77aefa7796f4befd23 "
        "is unavailable: runtime image receipt identity is unavailable or malformed"
    )
    with pytest.raises(InstallAdmissionBusy) as error:
        require_install_admissible(
            _blocked_install_plan(
                (RuntimePreflightCode.HOST_CHANGED,), details=(detail,)
            )
        )
    assert "malformed" in str(error.value)
    require_install_admissible(replace(_blocked_install_plan(()), allowed=True))
