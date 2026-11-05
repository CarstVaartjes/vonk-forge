"""Every stored Job payload field that holds an installation id is accounted for.

``owner_liveness.installations_named_by_live_jobs`` decides whether a live
operation still uses an installation, and the residue sweep uninstalls one that
nothing names. A payload contract that gains an installation id the helper does
not read would silently let the sweep uninstall an installation in use, so the
walk below fails until the new path is read (add it to
``INSTALLATION_PAYLOAD_PATHS``) or justified here.
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from typing import Annotated, get_args, get_origin

from pydantic import BaseModel
from vonk_agent_protocol.inventory import InstallationId
from vonk_control.owner_liveness import INSTALLATION_PAYLOAD_PATHS
from vonk_control.stored_json import bindings

_EXCLUDED_OWNER_FIELDS = "owner_id"

#: Paths the helper does not read, each with the reason that makes that safe.
_ELSEWHERE_COVERED: Mapping[str, str] = {
    "phases[][].payload.installation_id": (
        "the agent-operation copies inside an install, uninstall, reconcile, "
        "start, stop or job-run order; those orders name their owner in "
        "owner_kind/owner_id (an installation, or a run that blocks the sweep "
        "until stopped)"
    ),
    "recovery.start_phases[][].payload.installation_id": "as phases: a run-owned order",
    "reconciliation_authority.installation_id": (
        "a reconcile order, owned by the installation through owner_id"
    ),
    "job_run_stop_authorization.installation_id": "a run-owned stop order",
    "profile_stop_authorization.installation_id": "a run-owned stop order",
    "service_stop_review.exact_payloads[].installation_id": "a run-owned stop order",
    "service_stop_review.profile_stop_owner.installation_id": "a run-owned stop order",
    "plan.reconciliation_authority.installation_id": (
        "the plan validator requires it to equal plan.installation_id"
    ),
    "progress.final_observation.installation_id": (
        "recorded progress: the live copy is Job.result, read by the text match; "
        "payload.progress is only the initial empty snapshot"
    ),
    "progress.phase_results[].installation_id": "as progress.final_observation",
}


def installation_paths(contracts: Mapping[str | None, object]) -> set[str]:
    """Every field path, in any contract, that holds an installation id."""

    found: set[str] = set()
    for contract in contracts.values():
        _walk(contract, "", found, frozenset())
    return found


def _walk(tp: object, path: str, out: set[str], seen: frozenset[type]) -> None:
    origin = get_origin(tp)
    if origin is Annotated:
        if tp == InstallationId:
            out.add(path)
            return
        _walk(get_args(tp)[0], path, out, seen)
        return
    if origin is not None or isinstance(tp, types.UnionType):
        container = origin in (list, set, frozenset, tuple, dict)
        for arg in get_args(tp):
            _walk(arg, path + ("[]" if container else ""), out, seen)
        return
    if isinstance(tp, type) and issubclass(tp, BaseModel) and tp not in seen:
        for name, field in tp.model_fields.items():
            sub = f"{path}.{name}" if path else name
            if name == "installation_id":
                out.add(sub)
            _walk(field.annotation, sub, out, seen | {tp})


def unaccounted(contracts: Mapping[str | None, object]) -> set[str]:
    """Installation-id paths neither read by the helper nor justified."""

    return (
        installation_paths(contracts)
        - INSTALLATION_PAYLOAD_PATHS
        - set(_ELSEWHERE_COVERED)
    )


def test_every_installation_id_in_a_job_payload_is_read_or_justified() -> None:
    contracts = bindings()["jobs.payload"].contracts
    assert unaccounted(contracts) == set()
    found = installation_paths(contracts)
    # No stale entries: a path that no contract holds any more is removed.
    assert set(_ELSEWHERE_COVERED) <= found
    assert INSTALLATION_PAYLOAD_PATHS - {_EXCLUDED_OWNER_FIELDS} <= found


def test_a_new_installation_id_path_in_a_payload_model_fails_the_walk() -> None:
    class Inner(BaseModel):
        installation_id: InstallationId

    class Fake(BaseModel):
        elsewhere: list[Inner]

    contracts = dict(bindings()["jobs.payload"].contracts)
    contracts["recipe.fake.v1"] = Fake
    assert unaccounted(contracts) == {"elsewhere[].installation_id"}
