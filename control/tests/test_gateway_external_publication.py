"""External models in the gateway: the state machine, publication, ack failure and withdrawal."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from vonk_agent_protocol.lifecycle_vocabulary import (
    ErrorCategory,
    OperatorActionName,
    WaitReason,
)
from vonk_agent_protocol.outcome import UnknownError
from vonk_agent_protocol.reason_codes.runs import RunSwitchCode
from vonk_agent_protocol.route_activation import ActivationMarker, SupervisorPhase
from vonk_agent_protocol.state_machines import (
    ExternalBlockReason,
    ExternalPublicationState,
)
from vonk_control.gateway.external_contract import (
    ApprovedDestination,
    CredentialBinding,
    ExternalDeploymentSpec,
    ExternalDestination,
    ExternalModelInput,
)
from vonk_control.gateway.external_states import (
    ExternalEvent,
    apply_event,
    next_state,
    settle,
)
from vonk_control.gateway.external_validation import validate_external_entry
from vonk_control.gateway.litellm_surface import load_surface
from vonk_control.gateway.route_runtime import (
    AtomicRouteBundlePublisher,
    verify_active_route_bundle,
)
from vonk_control.model_primitives import Base
from vonk_control.models.gateway import (
    GatewayCredentialBinding,
    GatewayExternalDeployment,
)
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_routes.publisher import AtomicRecipeRoutePublisher
from vonk_control.recipe_routes.service import RecipeRouteService
from vonk_control.recipe_routes.shared import RecipeRouteNotReady
from vonk_control.waits.contract import WaitEvidence
from vonk_control.waits.persisted import read_wait

from .recipe_operations_support import setup_services
from .recipe_routes_support import (
    NOW,
    MutableClock,
    _live_models,
    atomic_service,
    setup,
)
from .run_switch_operations_support import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _request,
)
from .run_switch_operations_support import (
    _service as _run_switch_service,
)
from .test_route_runtime import _supervisor

ROOT = Path(__file__).resolve().parents[2]
PARTNER = "https://api.partner.example"
WIDER = "https://eu.partner.example"


def _spec(alias: str = "partner-large", model: str = "openai/partner-large"):
    surface = load_surface(ROOT / "deploy/compose/litellm/surface.json")
    bindings = {
        "PARTNER": CredentialBinding(
            name="PARTNER",
            revision=1,
            destinations=[ApprovedDestination(origin=PARTNER)],
        )
    }
    spec = validate_external_entry(
        ExternalModelInput(
            model_name=alias,
            litellm_params={
                "model": model,
                "api_base": f"{PARTNER}/v1",
                "api_key": "os.environ/VONK_CREDENTIAL_PARTNER",
            },
        ),
        surface,
        bindings,
        taken_aliases=(),
    )
    assert isinstance(spec, ExternalDeploymentSpec)
    return spec


def _seed(sessions, spec: ExternalDeploymentSpec, *, approve=(PARTNER,)):
    with sessions.begin() as session:
        if session.get(GatewayCredentialBinding, "PARTNER") is None:
            session.add(
                GatewayCredentialBinding(
                    name="PARTNER",
                    revision=1,
                    destinations=[ApprovedDestination(origin=item) for item in approve],
                    request_id=str(uuid4()),
                    actor="admin",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        row = GatewayExternalDeployment(
            id=str(uuid4()),
            alias=spec.alias,
            desired_revision=1,
            desired_spec=spec,
            enabled=True,
            removal_requested=False,
            state=ExternalPublicationState.PENDING.value,
            request_id=str(uuid4()),
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(row)
        return row.id


def _supervisor_fake(monkeypatch, root: Path):
    """A faithful LiteLLM supervisor: the real one reads the bundle, a ``bad/`` model never starts."""

    supervisor = _supervisor(monkeypatch, root)

    def acknowledge(
        marker: ActivationMarker, required: SupervisorPhase
    ) -> UnknownError | None:
        request = supervisor._active_request()
        assert request is not None, "the supervisor cannot read the bundle"
        assert request.marker == marker.model_dump()
        config = json.loads(request.config.read_bytes())
        if any(
            entry["litellm_params"]["model"].startswith("bad/")
            for entry in config["model_list"]
        ):
            return UnknownError(
                category=ErrorCategory.UNKNOWN,
                reason=WaitReason.RUNTIME_EFFECT_UNCONFIRMED,
            )
        return None

    return acknowledge


def _service(
    tmp_path: Path,
    monkeypatch,
    clock,
    engine=None,
    ack=None,
) -> RecipeRouteService:
    if engine is None:
        engine = create_engine(f"sqlite:///{tmp_path / 'externals.sqlite'}")
    Base.metadata.create_all(engine)
    root = tmp_path / "live"
    runtime = AtomicRouteBundlePublisher(
        root, await_supervisor_ack=ack or _supervisor_fake(monkeypatch, root)
    )
    return RecipeRouteService(
        sessionmaker(engine, expire_on_commit=False),
        publisher=AtomicRecipeRoutePublisher(runtime),
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=clock,
        credentials=lambda: frozenset({"PARTNER"}),
    )


def _digest(root: Path) -> str:
    bundle = verify_active_route_bundle(root)
    assert not isinstance(bundle, UnknownError)
    return bundle.marker.digest


def _row(service: RecipeRouteService, alias: str) -> GatewayExternalDeployment | None:
    with service.sessions() as session:
        return session.scalar(select(GatewayExternalDeployment).filter_by(alias=alias))


def test_an_external_model_publishes_with_no_local_route_and_leaves_with_its_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = MutableClock(NOW)
    service = _service(tmp_path, monkeypatch, clock)
    _seed(service.sessions, _spec())
    root = tmp_path / "live"

    assert service.maintain() is True
    assert _live_models(root) == ["partner-large"]
    live = _digest(root)
    row = _row(service, "partner-large")
    assert row is not None
    assert row.state == ExternalPublicationState.PUBLISHED.value
    assert row.applied_revision == 1
    # A pass that finds nothing changed publishes nothing: no gateway restart.
    assert service.maintain() is False
    assert _digest(root) == live

    with service.sessions.begin() as session:
        stored = session.scalar(select(GatewayExternalDeployment))
        assert stored is not None
        stored.enabled = False
        stored.removal_requested = True
    assert service.maintain() is True
    assert _live_models(root) == []
    assert _row(service, "partner-large") is None  # confirmed gone: the name is free
    assert service.maintain() is False


def _generations(root: Path) -> int:
    return len(list((root / "generations").iterdir()))


def test_a_failed_start_blocks_the_entry_and_the_next_pass_publishes_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The failed generation is live, and dark, until the next pass republishes
    # without the entry; the review discloses that (the other pass is the API
    # tick about five seconds later in production).
    clock = MutableClock(NOW)
    base, _publisher, _applied, run_id = setup(tmp_path / "database", clock=clock)
    service = atomic_service(base, tmp_path / "live", clock)
    service._publisher._publisher._await_supervisor_ack = _supervisor_fake(
        monkeypatch, tmp_path / "live"
    )
    service._credentials = lambda: frozenset({"PARTNER"})
    service.publish_run(run_id)
    assert _live_models(tmp_path / "live") == ["qwen"]
    # An entry the pinned LiteLLM no longer accepts (admitted before a pin change).
    _seed(service.sessions, _bad_spec())

    assert service.maintain() is False  # the generation was not acknowledged
    assert "odd-model" in _live_models(tmp_path / "live")  # live, and dark
    row = _row(service, "odd-model")
    assert row is not None
    assert row.state == ExternalPublicationState.BLOCKED.value
    assert row.reason == ExternalBlockReason.START_FAILED.value
    assert row.start_failures == 1
    with service.sessions() as session:
        owned = session.get(GatewayExternalDeployment, row.id)
        assert owned is not None
        wait = read_wait(owned)
    assert wait is not None
    assert wait.next_check > clock.now

    clock.now += timedelta(
        seconds=120
    )  # past the owner's own backoff, before the retry
    assert service.maintain() is True
    assert _live_models(tmp_path / "live") == ["qwen"]


def _bad_spec() -> ExternalDeploymentSpec:
    return ExternalDeploymentSpec(
        alias="odd-model",
        litellm_params={"model": "bad/odd"},
        destination=ExternalDestination(origins=["provider:bad"]),
    )


def test_three_failed_starts_stop_the_retries_until_an_edit_or_an_enable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The owner's identical-exit rule: three failed starts of the same content leave
    # the entry blocked with evidence naming the resume action; a content change or
    # an enable writes exactly one more generation.
    clock = MutableClock(NOW)
    service = _service(tmp_path, monkeypatch, clock)
    _seed(service.sessions, _bad_spec())
    root = tmp_path / "live"

    for _failure in range(3):
        clock.now += timedelta(minutes=6)  # the retry of a blocked entry is due
        service.maintain()  # introduces the entry; the generation fails
        clock.now += timedelta(seconds=90)
        service.maintain()  # publishes without it
    row = _row(service, "odd-model")
    assert row is not None
    assert row.start_failures == 3
    assert row.state == ExternalPublicationState.BLOCKED.value
    assert row.next_action_at is None
    with service.sessions() as session:
        owned = session.get(GatewayExternalDeployment, row.id)
        assert owned is not None
        assert isinstance(owned.wait_evidence, WaitEvidence)
        assert owned.wait_evidence.operator_action == OperatorActionName.RESUME
    parked = _generations(root)
    clock.now += timedelta(hours=1)
    assert service.maintain() is False  # no fourth try
    assert _generations(root) == parked

    # An enable starts the entry again: exactly one generation, which fails once.
    with service.sessions.begin() as session:
        stored = session.get(GatewayExternalDeployment, row.id)
        assert stored is not None
        apply_event(stored, ExternalEvent.ENABLE, now=clock.now)
    service.maintain()
    assert _generations(root) == parked + 1
    again = _row(service, "odd-model")
    assert again is not None
    assert again.start_failures == 1
    assert again.state == ExternalPublicationState.BLOCKED.value

    # So does an edit (a content change): the failures are forgotten.
    with service.sessions.begin() as session:
        stored = session.get(GatewayExternalDeployment, row.id)
        assert stored is not None
        stored.desired_revision += 1
        stored.desired_spec = _bad_spec().model_copy(
            update={"litellm_params": {"model": "bad/odd2"}}
        )
        apply_event(stored, ExternalEvent.REVISION_ACCEPTED, now=clock.now)
        assert stored.start_failures == 0


def test_a_refusal_before_the_generation_is_written_blames_no_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Break caught: any failure of the publication effect (here, preparing the
    # keys) marked the introduced entries start-failed though nothing was written.
    clock = MutableClock(NOW)
    service = _service(tmp_path, monkeypatch, clock)
    _seed(service.sessions, _spec())

    def refuse(_runs: frozenset[str]) -> None:
        raise RecipeRouteNotReady("gateway keys are unavailable")

    service._prepare_key_routes = refuse
    service.maintain()

    row = _row(service, "partner-large")
    assert row is not None
    assert row.state == ExternalPublicationState.PENDING.value
    assert row.reason is None
    assert not (tmp_path / "live" / "activation.json").exists()


def test_a_crash_after_the_acknowledged_generation_repeats_no_effect_and_loses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Break caught: the record was written before the effect, or the effect was
    # repeated as a new generation (another gateway restart) after a crash.
    clock = MutableClock(NOW)
    service = _service(tmp_path, monkeypatch, clock)
    _seed(service.sessions, _spec())
    root = tmp_path / "live"
    record = RecipeRouteService._record_completion

    def crash(self, publication, generation):
        raise RuntimeError("process died after the acknowledgement")

    monkeypatch.setattr(RecipeRouteService, "_record_completion", crash)
    service.maintain()
    monkeypatch.setattr(RecipeRouteService, "_record_completion", record)
    live = _digest(root)
    row = _row(service, "partner-large")
    assert row is not None
    assert row.state == ExternalPublicationState.PENDING.value

    clock.now += timedelta(minutes=5)
    assert service.maintain() is True
    assert _digest(root) == live  # the exact acknowledged bytes were adopted
    row = _row(service, "partner-large")
    assert row is not None
    assert row.state == ExternalPublicationState.PUBLISHED.value
    assert _live_models(root) == ["partner-large"]


def test_a_missing_credential_blocks_then_retries_when_the_supervisor_loads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = MutableClock(NOW)
    service = _service(tmp_path, monkeypatch, clock)
    loaded: set[str] = set()
    service._credentials = lambda: frozenset(loaded)
    _seed(service.sessions, _spec())

    service.maintain()
    row = _row(service, "partner-large")
    assert row is not None
    assert row.state == ExternalPublicationState.BLOCKED.value
    assert row.reason == ExternalBlockReason.CREDENTIAL_MISSING.value
    assert not (tmp_path / "live" / "activation.json").exists()

    loaded.add("PARTNER")
    clock.now += timedelta(minutes=2)
    service.maintain()
    row = _row(service, "partner-large")
    assert row is not None
    assert row.state == ExternalPublicationState.PUBLISHED.value
    assert row.reason is None


def test_narrowing_a_binding_withdraws_a_deployment_and_widening_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = MutableClock(NOW)
    service = _service(tmp_path, monkeypatch, clock)
    _seed(service.sessions, _spec(), approve=(PARTNER, WIDER))
    root = tmp_path / "live"
    service.maintain()
    live = _digest(root)

    # Widening the approved destinations changes no deployment.
    with service.sessions.begin() as session:
        binding = session.get(GatewayCredentialBinding, "PARTNER")
        assert binding is not None
        binding.destinations = [
            ApprovedDestination(origin=item)
            for item in (PARTNER, WIDER, "https://x.example")
        ]
    assert service.maintain() is False
    assert _digest(root) == live

    # Narrowing past the origin the deployment uses withdraws it.
    with service.sessions.begin() as session:
        binding = session.get(GatewayCredentialBinding, "PARTNER")
        assert binding is not None
        binding.destinations = [ApprovedDestination(origin=WIDER)]
    assert service.maintain() is True
    assert _live_models(root) == []
    row = _row(service, "partner-large")
    assert row is not None
    assert row.state == ExternalPublicationState.WITHDRAWN.value
    assert row.reason == ExternalBlockReason.DESTINATION_NOT_APPROVED.value


def test_the_declared_edges_and_an_unknown_word_that_self_heals() -> None:
    pending, published = (
        ExternalPublicationState.PENDING,
        ExternalPublicationState.PUBLISHED,
    )
    blocked, withdrawing = (
        ExternalPublicationState.BLOCKED,
        ExternalPublicationState.WITHDRAWING,
    )
    withdrawn = ExternalPublicationState.WITHDRAWN
    edges = [
        (pending, ExternalEvent.ACKED, published),
        (pending, ExternalEvent.BLOCK, blocked),
        (published, ExternalEvent.REVISION_ACCEPTED, pending),
        (blocked, ExternalEvent.RETRY_DUE, pending),
        (blocked, ExternalEvent.REVISION_ACCEPTED, pending),
        (blocked, ExternalEvent.ENABLE, pending),
        (withdrawn, ExternalEvent.ENABLE, pending),
        (withdrawing, ExternalEvent.DRAINED, withdrawn),
        (pending, ExternalEvent.WITHDRAW, withdrawing),
        (published, ExternalEvent.WITHDRAW, withdrawing),
        (blocked, ExternalEvent.WITHDRAW, withdrawing),
        # An event with no edge from the state changes nothing.
        (published, ExternalEvent.RETRY_DUE, published),
        (withdrawing, ExternalEvent.ENABLE, withdrawing),
        (withdrawn, ExternalEvent.ACKED, withdrawn),
        (published, ExternalEvent.REPLAN, published),
    ]
    for state, event, expected in edges:
        assert next_state(state.value, event) == expected, (state, event)
    # A stored word the machine never had re-plans from intent instead of blocking.
    assert (
        next_state("retired-word", ExternalEvent.ACKED)
        == ExternalPublicationState.PENDING
    )
    wanted = GatewayExternalDeployment(
        state="retired-word", enabled=True, removal_requested=False
    )
    assert settle(wanted, now=NOW) == ExternalPublicationState.PENDING
    removed = GatewayExternalDeployment(
        state="retired-word", enabled=False, removal_requested=True
    )
    assert settle(removed, now=NOW) == ExternalPublicationState.WITHDRAWING
    # A wanted deployment that is withdrawn serves again; the intent decides.
    back = GatewayExternalDeployment(
        state=ExternalPublicationState.WITHDRAWN.value,
        enabled=True,
        removal_requested=False,
    )
    assert settle(back, now=NOW) == ExternalPublicationState.PENDING


def test_a_direct_run_load_shows_alias_taken_up_front(tmp_path: Path) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    _seed(sessions, _spec("qwen"))
    service = _run_switch_service(
        sessions,
        NOW,
        lifecycle,
        RecordingArtifactExecutor(),
        artifacts=CompleteArtifactInspector(),
    )
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")
    assert plan.allowed is False
    assert RunSwitchCode.ALIAS_TAKEN in {item.code for item in plan.blockers}
