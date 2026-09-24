"""A bound preparation build may borrow its future runtime's memory promise."""

import io
from copy import deepcopy
from datetime import timedelta
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import select
from vonk_agent_protocol import canonical_message
from vonk_control.memory_reservations import memory_reservations
from vonk_control.models import (
    AgentNode,
    ClusterMapping,
    Job,
    NodeInventorySnapshot,
    RecipeBuild,
    RecipeSourceBundle,
    ResourceReservation,
)
from vonk_control.recipe_builds import (
    RecipeBuildError,
    RecipeBuildPlan,
    RecipeBuildService,
)
from vonk_control.run_switch_contract import RunSwitchPlan
from vonk_control.run_switch_operations import RunSwitchOperationConflict
from vonk_control.source_bundles import SourceBundleStore, generate_source_bundle

from .preflight_fixtures import record_passing_preflight
from .test_profile_capacity_admission import _capacity_profile
from .test_profile_installed_execution import _profile_service
from .test_profile_port_claims import _load, _ready_profile


def _accepted_build_profile(
    tmp_path,
    engine,
    *,
    build_memory_bytes=200,
    accept_profile=True,
    initially_installed=True,
):
    if initially_installed:
        sessions, profiles, planner, profile, api, headers, nodes, _ = _ready_profile(
            tmp_path, engine
        )
    else:
        sessions, profiles, planner, profile, api, headers, _, nodes = (
            _capacity_profile(tmp_path, engine)
        )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    bundles = SourceBundleStore(tmp_path / "sources")
    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\nUSER 10001:10001\n"})
    stored = bundles.put(bundle.sha256, io.BytesIO(bundle.archive))
    with sessions.begin() as session:
        snapshot = session.scalar(select(NodeInventorySnapshot))
        node = session.get(AgentNode, nodes[0])
        build = session.scalar(select(RecipeBuild))
        assert snapshot is not None and node is not None and build is not None
        node.binary_digest = "1" * 64
        node.capabilities = [*node.capabilities, "recipe.build.v1"]
        snapshot.capabilities = [*snapshot.capabilities, "recipe.build.v1"]
        snapshot.host_memory_free_bytes = snapshot.gpu_memory_free_bytes = 275
        snapshot.disk_total_bytes = 100_000
        snapshot.disk_free_bytes = 80_000
        session.add(
            RecipeSourceBundle(
                sha256=bundle.sha256,
                media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
                archive_bytes=stored.archive_bytes,
                total_bytes=bundle.manifest.total_bytes,
                file_count=len(bundle.manifest.files),
                storage_key=f"{bundle.sha256[:2]}/{bundle.sha256}.tar",
                manifest=bundle.manifest.model_dump(mode="json"),
                verified_at=profiles._clock(),
            )
        )
        build.source_bundle_sha256 = bundle.sha256
        payload = deepcopy(build.plan)
        payload["build_id"] = build.id
        payload["source_bundle_sha256"] = bundle.sha256
        payload["source_bundle_bytes"] = stored.archive_bytes
        limits = payload["limits"]
        assert isinstance(limits, dict)
        limits["memory_bytes"] = build_memory_bytes
        build.plan = payload
        build.policy_report = {
            **build.policy_report,
            "builder_binary_digest": node.binary_digest,
            "source_bundle_sha256": bundle.sha256,
        }
        selected = RecipeBuildPlan(
            build_id=build.id,
            recipe_revision_id=build.recipe_revision_id,
            recipe_content_sha256=str(payload["recipe_content_sha256"]),
            builder_node_id=node.node_id,
            source_bundle_sha256=build.source_bundle_sha256,
            build_input_sha256=build.build_input_sha256,
            agent_payload=payload,
        )
    lifecycle._builds = RecipeBuildService(sessions, bundles=bundles)
    planner._memory_floor = 50
    record_passing_preflight(
        sessions,
        profiles._clock(),
        floor=lifecycle._install_admission._disk_floor,
        source_build=True,
    )
    if not initially_installed:
        # Resolve the verified fixture archive without accepting an install.
        # The process test can then remove real bytes after profile acceptance.
        with sessions() as session:
            mapping_id = session.scalar(select(ClusterMapping.id))
            assert mapping_id is not None
        preview = lifecycle.preview_install(mapping_id, selected.build_id)
        assert preview.allowed
    application_id = _load(profile, api, headers)[0] if accept_profile else None
    # Cache loss occurs after acceptance. The already selected, typed build
    # receipt is still the same identity; only its executable work is pending.
    with sessions.begin() as session:
        build = session.get(RecipeBuild, selected.build_id)
        assert build is not None
        build.state = "planned"
        build.image_digest = build.oci_layout_sha256 = None
        build.image_bytes = None
    return sessions, profiles, planner, nodes[0], application_id, selected


def _parent_build_request(sessions, profiles):
    assert profiles.tick()
    with sessions() as session:
        parent = session.scalar(select(Job).where(Job.kind == "recipe.run-switch.v2"))
        assert parent is not None
        return parent.id, str(uuid5(UUID(parent.request_id), "container-build"))


@pytest.mark.parametrize("state", ["building", "succeeded"])
def test_build_adoption_checks_input_identity_before_cached_or_active_result(
    tmp_path, postgres_engine, state
):
    sessions, profiles, planner, _, _, selected = _accepted_build_profile(
        tmp_path, postgres_engine
    )
    parent_id, request_id = _parent_build_request(sessions, profiles)
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    lifecycle.build(
        selected,
        build_input_sha256=selected.build_input_sha256,
        actor="admin",
        request_id=request_id,
    )
    with sessions.begin() as session:
        parent = session.get(Job, parent_id)
        build = session.get(RecipeBuild, selected.build_id)
        assert parent is not None and parent.result is not None and build is not None
        plan = RunSwitchPlan.model_validate_json(
            canonical_message(parent.payload["plan"]), strict=True
        )
        parent_key = parent.request_id
        parent_actor = parent.actor
        progress = deepcopy(parent.result)
        if state == "succeeded":
            build.state = state
            build.image_digest = "sha256:" + "1" * 64
            build.oci_layout_sha256 = "2" * 64
            build.image_bytes = 30
    changed = plan.model_copy(
        update={"build": plan.build.model_copy(update={"build_input_sha256": "d" * 64})}
    )
    assert changed.build.build_input_sha256 != selected.build_input_sha256
    executor = planner._phase_executor
    assert executor is not None
    with pytest.raises(
        RunSwitchOperationConflict, match="container-build-plan-invalid"
    ):
        executor.execute(
            changed,
            changed.phases[0],
            item_index=0,
            actor=parent_actor,
            request_key=parent_key,
            progress=progress,
        )


def test_an_unrelated_request_cannot_borrow_an_active_parent_build_claim(
    tmp_path, postgres_engine
):
    sessions, profiles, planner, _, _, selected = _accepted_build_profile(
        tmp_path, postgres_engine
    )
    _parent_build_request(sessions, profiles)
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    with pytest.raises(RecipeBuildError) as failure:
        lifecycle.build(
            selected,
            build_input_sha256=selected.build_input_sha256,
            actor="admin",
            request_id=str(uuid4()),
        )
    assert failure.value.code == "build.insufficient_memory"
    with sessions() as session:
        assert not tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.build.v1"))
        )


@pytest.mark.parametrize("demand", [225, 226])
def test_bound_build_preserves_the_profile_system_reserve(
    tmp_path, postgres_engine, demand
):
    sessions, profiles, planner, _, application_id, selected = _accepted_build_profile(
        tmp_path, postgres_engine, build_memory_bytes=demand
    )
    _, request_id = _parent_build_request(sessions, profiles)
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    if demand == 226:
        with pytest.raises(RecipeBuildError) as failure:
            lifecycle.build(
                selected,
                build_input_sha256=selected.build_input_sha256,
                actor="admin",
                request_id=request_id,
            )
        assert failure.value.code == "build.insufficient_memory"
    else:
        lifecycle.build(
            selected,
            build_input_sha256=selected.build_input_sha256,
            actor="admin",
            request_id=request_id,
        )
    with sessions() as session:
        promise = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == application_id,
                ResourceReservation.kind == "unified-memory",
            )
        )
        assert promise is not None and promise.state == "promised"
        builds = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.build.v1"))
        )
        assert len(builds) == (1 if demand == 225 else 0)


@pytest.mark.parametrize(
    "change", ["amount", "released", "digest", "pool", "intent", "cancelled", "phase"]
)
def test_bound_build_refuses_changed_claim_even_when_fresh_capacity_fits(
    tmp_path, postgres_engine, change
):
    sessions, profiles, planner, node_id, application_id, selected = (
        _accepted_build_profile(tmp_path, postgres_engine)
    )
    parent_id, request_id = _parent_build_request(sessions, profiles)
    with sessions.begin() as session:
        parent = session.get(Job, parent_id)
        node = session.get(AgentNode, node_id)
        snapshot = session.scalar(select(NodeInventorySnapshot))
        claim = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == application_id,
                ResourceReservation.kind == "unified-memory",
            )
        )
        assert (
            parent is not None
            and node is not None
            and snapshot is not None
            and claim is not None
        )
        snapshot.host_memory_free_bytes = snapshot.gpu_memory_free_bytes = 1_000
        if change == "amount":
            claim.amount_bytes += 1
        elif change == "released":
            claim.state = "released"
        elif change == "digest":
            claim.plan_digest = "e" * 64
        elif change == "pool":
            snapshot.memory_pool = "separate"
        elif change == "intent":
            node.workload_intent_ordinal += 1
        elif change == "cancelled":
            parent.state = "cancelled"
        else:
            assert parent.result is not None
            parent.result = {**parent.result, "phase_index": 1}
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    with pytest.raises(ValueError, match="profile|preparation"):
        lifecycle.build(
            selected,
            build_input_sha256=selected.build_input_sha256,
            actor="admin",
            request_id=request_id,
        )
    with sessions() as session:
        assert not tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.build.v1"))
        )


def test_build_reconnects_after_child_commit_before_parent_checkpoint(
    tmp_path, postgres_engine, monkeypatch
):
    sessions, profiles, planner, _, _, _ = _accepted_build_profile(
        tmp_path, postgres_engine
    )
    executor = planner._phase_executor
    assert executor is not None
    execute = executor.execute

    def crash_after_build(*args, **kwargs):
        result = execute(*args, **kwargs)
        if result.operation_id is not None:
            raise SystemExit("build child committed")
        return result

    monkeypatch.setattr(executor, "execute", crash_after_build)
    with pytest.raises(SystemExit, match="build child committed"):
        for _ in range(12):
            profiles.tick()
            planner.tick()
    with sessions.begin() as session:
        child = session.scalar(select(Job).where(Job.kind == "recipe.build.v1"))
        assert child is not None
        child_id = child.id
        snapshot = session.scalar(select(NodeInventorySnapshot))
        assert snapshot is not None
        snapshot.host_memory_free_bytes = snapshot.gpu_memory_free_bytes = 0
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    profiles, planner = _profile_service(sessions, lifecycle)
    for _ in range(4):
        profiles.tick()
        planner.tick()
    with sessions() as session:
        assert tuple(
            session.scalars(select(Job.id).where(Job.kind == "recipe.build.v1"))
        ) == (child_id,)
        parent = session.scalar(select(Job).where(Job.kind == "recipe.run-switch.v2"))
        assert parent is not None and parent.result is not None
        assert parent.result["child_operation_id"] == child_id


def test_busy_build_claim_retries_without_releasing_the_parent_promise(
    tmp_path, postgres_engine
):
    sessions, profiles, planner, _, application_id, _ = _accepted_build_profile(
        tmp_path, postgres_engine
    )
    parent_id, _ = _parent_build_request(sessions, profiles)
    with sessions.begin() as holder:
        claim = holder.scalar(
            select(ResourceReservation)
            .where(
                ResourceReservation.owner_id == application_id,
                ResourceReservation.kind == "unified-memory",
            )
            .with_for_update()
        )
        assert claim is not None
        claim_id = claim.id
        assert planner.tick()
        parent = planner.get(parent_id)
        assert (
            parent.result is not None
            and parent.result.retry_reason == "build.capacity_busy"
        )
        due = parent.result.observation_due_at
        assert due is not None
        assert not planner.tick()
        with sessions() as observer:
            assert not tuple(
                observer.scalars(select(Job).where(Job.kind == "recipe.build.v1"))
            )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    planner._clock = profiles._clock = lifecycle._clock = lambda: (
        due + timedelta(seconds=1)
    )
    assert planner.tick()
    with sessions() as session:
        assert (
            session.scalar(select(Job).where(Job.kind == "recipe.build.v1")) is not None
        )
        claim = session.get(ResourceReservation, claim_id)
        assert (
            claim is not None
            and claim.owner_id == application_id
            and claim.state == "promised"
        )


def test_profile_build_borrows_promise_and_preserves_it_for_runtime(
    tmp_path, postgres_engine
):
    sessions, profiles, planner, node, application_id, selected = (
        _accepted_build_profile(tmp_path, postgres_engine)
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    # The receipt alone is not authority to borrow a profile's claim. The
    # accepted child must reach the exact preparation phase first.
    with pytest.raises(RecipeBuildError, match="memory"):
        lifecycle.build(
            selected,
            build_input_sha256=selected.build_input_sha256,
            actor="admin",
            request_id=str(uuid4()),
        )
    for _ in range(12):
        profiles.tick()
        planner.tick()
        with sessions() as session:
            child = session.scalar(select(Job).where(Job.kind == "recipe.build.v1"))
            if child is not None:
                child_id = child.id
                break
    else:
        assert application_id is not None
        application = profiles.application(application_id)
        with sessions() as session:
            pending = [
                (job.kind, job.state, job.status_reason, job.result)
                for job in session.scalars(select(Job))
            ]
        pytest.fail(f"{application.state}: {application.status_reason}; {pending}")
    with sessions() as session:
        promise = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == application_id,
                ResourceReservation.kind == "unified-memory",
            )
        )
        assert promise is not None and promise.state == "promised"
        claim_id = promise.id
        totals = memory_reservations(session, node, memory_pool="shared")
        assert totals.committed_bytes_by_kind == {
            "unified-memory": promise.amount_bytes
        }
        assert totals.unmaterialized_bytes_by_kind == {
            "unified-memory": promise.amount_bytes
        }
        assert lifecycle.get(child_id).owner_id == selected.build_id
    lifecycle.record_node_result(
        child_id,
        node,
        succeeded=True,
        evidence={
            "build_input_sha256": selected.build_input_sha256,
            "image_digest": "sha256:" + "1" * 64,
            "oci_layout_sha256": "2" * 64,
            "image_bytes": 30,
            "policy": {"passed": True, "findings": [], "dockerfile": "Dockerfile"},
        },
    )
    with sessions() as session:
        promise = session.get(ResourceReservation, claim_id)
        assert promise is not None and promise.owner_id == application_id
        assert promise.state == "promised"
        totals = memory_reservations(session, node, memory_pool="shared")
        assert totals.committed_bytes_by_kind == {
            "unified-memory": promise.amount_bytes
        }
        assert totals.unmaterialized_bytes_by_kind == {
            "unified-memory": promise.amount_bytes
        }
