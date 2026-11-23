"""Read-only Library assessments composed from the actual Controller owners."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from itertools import combinations
from typing import get_args

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import LibraryAssessmentCode
from vonk_agent_protocol.state_machines import NodeIdentityState

from ..model_cache import ModelCacheService
from ..model_cache.resolution import artifact_sets_resolved_once
from ..models import AgentNode
from ..run_switch_contract import RunSwitchPlan, RunSwitchReason
from ..run_switch_operations import RunSwitchOperationService
from ..run_switch_operations.artifact_inspection import (
    failed_copies_read_once,
    missing_spark_bytes,
    spark_stores,
)
from ..run_switch_operations.phase_planning import targets_hold_model
from .contract import LibraryRecipeProjection, RecipeReadiness, RecipeReadinessCheck


def _reason(code: str, detail: str) -> RunSwitchReason:
    return RunSwitchReason(
        code=code, detail=detail, severity="blocker", scope="operation"
    )


def unavailable(detail: str) -> RecipeReadinessCheck:
    return RecipeReadinessCheck(
        state="unavailable",
        reasons=[_reason(LibraryAssessmentCode.ASSESSMENT_UNAVAILABLE, detail)],
    )


def unassessed(clock: Callable[[], datetime], detail: str) -> RecipeReadiness:
    unknown = unavailable(detail)
    return RecipeReadiness(
        observed_at=clock(), fleet_fit=unknown, cache=unknown, readiness=unknown
    )


ASSESSMENT_BUDGET_SECONDS = 5.0
# The check states, as the contract names them.
READY, _BLOCKED, _UNAVAILABLE = get_args(
    RecipeReadinessCheck.model_fields["state"].annotation
)


class LibraryAssessment:
    """Search complete groups under a time budget, never a candidate-count cap.

    A proven candidate can end the search. An exhausted time budget cannot prove
    that no candidate exists, and produces explicit unavailable evidence.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        run_switch: RunSwitchOperationService,
        model_cache: ModelCacheService,
        clock: Callable[[], datetime],
        budget_seconds: float = ASSESSMENT_BUDGET_SECONDS,
    ) -> None:
        if type(budget_seconds) not in (int, float) or not 0 < budget_seconds <= 10:
            raise ValueError("library assessment budget must be in (0, 10] seconds")
        self._sessions = sessions
        self._run_switch = run_switch
        self._model_cache = model_cache
        self._clock = clock
        self._budget = budget_seconds
        self._active = threading.Lock()

    def __call__(
        self,
        recipes: Sequence[LibraryRecipeProjection],
        *,
        budget_seconds: float | None = None,
    ) -> list[LibraryRecipeProjection]:
        """Assess in order until the budget ends, then mark the rest unavailable.

        The budget is a hard bound on this call: the assessment runs on its own
        thread and an assessment still running at the deadline is discarded, so
        no single slow read can hold the response. Recipes not finished in time
        say so in their own assessment instead of failing the listing.
        """

        budget = (
            self._budget
            if budget_seconds is None
            else min(self._budget, budget_seconds)
        )
        deadline = time.monotonic() + budget
        exceeded = f"Assessment exceeded its {budget:g}-second read budget."
        assessments: list[RecipeReadiness | None] = [None] * len(recipes)
        # One worker reads at a time, so a read that never returns cannot
        # gather workers. A worker left past its deadline stops at its next
        # check; the next assessment waits out that last read within its own
        # budget instead of being refused at once.
        if not self._active.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return [
                item.model_copy(
                    update={"assessment": unassessed(self._clock, exceeded)}
                )
                for item in recipes
            ]
        stop = threading.Event()

        def assess_in_order() -> None:
            # The Sparks' store scans are read once for this whole assessment,
            # and each exact artifact set is resolved once, not per placement.
            held: dict[str, Mapping[str, int] | None] = {}
            try:
                # Roster discovery shares the same observation deadline as cache
                # and placement reads, rather than blocking before the timer.
                with self._sessions() as session:
                    nodes = tuple(
                        session.scalars(
                            select(AgentNode.node_id)
                            .where(
                                AgentNode.state == NodeIdentityState.ACTIVE,
                                AgentNode.revoked_at.is_(None),
                            )
                            .order_by(AgentNode.node_id)
                        )
                    )
                with artifact_sets_resolved_once(), failed_copies_read_once():
                    for index, recipe in enumerate(recipes):
                        if stop.is_set() or time.monotonic() >= deadline:
                            return
                        assessments[index] = self._assess(recipe, nodes, held, deadline)
            except (SQLAlchemyError, OSError, RuntimeError, TypeError, ValueError):
                return
            finally:
                self._active.release()

        worker = threading.Thread(
            target=assess_in_order, name="library-assessment", daemon=True
        )
        try:
            worker.start()
        except RuntimeError:
            self._active.release()
            return [
                item.model_copy(
                    update={"assessment": unassessed(self._clock, exceeded)}
                )
                for item in recipes
            ]
        worker.join(timeout=max(0.0, deadline - time.monotonic()))
        stop.set()
        return [
            recipe.model_copy(
                update={
                    "assessment": assessment
                    if assessment is not None
                    else unassessed(self._clock, exceeded)
                }
            )
            for recipe, assessment in zip(recipes, list(assessments), strict=True)
        ]

    def _cache(
        self, recipe: LibraryRecipeProjection
    ) -> tuple[RecipeReadinessCheck, str | None]:
        try:
            resolved = self._model_cache.resolve_latest_cached(
                recipe_identity=recipe.identity.recipe_id,
                exact_revision_id=recipe.identity.recipe_revision_id,
            )
            image = resolved.recipe
            model = resolved.model
            blockers = [reason.value for reason in resolved.blockers]
            if (
                image.recipe_revision_id != recipe.identity.recipe_revision_id
                or image.content_sha256 != recipe.identity.content_sha256
                or model.content_sha256 != recipe.projected.models[0].content_sha256
            ):
                raise ValueError("cache resolution changed an exact identity")
            # The profile resolver supplies revision/image authority. Its
            # primary-model cache set alone cannot prove that every selected
            # file and companion dependency in this recipe is present.
            preview = self._model_cache.download_preview(
                model_content_sha256=recipe.projected.models[0].content_sha256,
                recipe_revision_id=recipe.identity.recipe_revision_id,
            )
            missing_bytes = preview.get("new_bytes")
            if type(missing_bytes) is not int or missing_bytes < 0:
                raise ValueError("exact model artifact availability is invalid")
            if missing_bytes:
                blockers = [
                    *blockers,
                    f"{missing_bytes} exact model artifact bytes are missing",
                ]
            if not blockers:
                digest = image.image_digest
                if not image.cached or not model.cached or digest is None:
                    raise ValueError("cache availability contradicts its blockers")
                return RecipeReadinessCheck(state="ready"), digest
            return RecipeReadinessCheck(
                state="blocked",
                reasons=[
                    _reason(
                        LibraryAssessmentCode.CACHE_MISSING,
                        f"{reason} for {recipe.selector}; the Controller prepares the exact assets when a load needs them.",
                    )
                    for reason in blockers
                ],
            ), None
        except (
            SQLAlchemyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            KeyError,
        ):
            return unavailable(
                "The exact NAS model and runtime-image evidence could not be read."
            ), None

    @staticmethod
    def _fit(plan: RunSwitchPlan) -> RecipeReadinessCheck:
        fit = plan.fit_current
        unknown = any(
            item.state != "fresh"
            for item in plan.freshness
            if item.source.endswith(":inventory")
        ) or any(
            node.resource_demand is not None
            and node.resource_demand.evidence_state == "unknown"
            for node in fit.nodes
        )
        if unknown:
            return RecipeReadinessCheck(
                state="unavailable",
                reasons=fit.blockers
                or [
                    _reason(
                        LibraryAssessmentCode.CAPACITY_UNAVAILABLE,
                        "Fresh capacity and resource-demand evidence is unavailable.",
                    )
                ],
            )
        return RecipeReadinessCheck(
            state="ready" if fit.allowed else "blocked", reasons=fit.blockers
        )

    @staticmethod
    def _ready(
        plan: RunSwitchPlan,
        fit: RecipeReadinessCheck,
        cache: RecipeReadinessCheck,
        image_digest: str | None,
    ) -> RecipeReadinessCheck:
        preparation = plan.preparation
        # The planner's rule: a load whose target Sparks hold the model
        # files downloads no model, so the NAS model copy does not decide and
        # the placement's fit stands in for the cache check. The Controller
        # image copy still decides, because compile reads it.
        nas_free = (
            preparation is not None
            and preparation.runtime_image.controller.missing_bytes == 0
            and targets_hold_model(plan.storage)
        )
        if nas_free:
            cache = fit
        if cache.state == "blocked":
            return cache
        if fit.state == "blocked":
            return fit
        if cache.state == "unavailable" or fit.state == "unavailable":
            return cache if cache.state == "unavailable" else fit
        if not plan.allowed:
            return RecipeReadinessCheck(state="blocked", reasons=plan.blockers)
        if not nas_free and (
            plan.image_digest != image_digest
            or preparation is None
            or not preparation.controller_ready
        ):
            return unavailable(
                "The placement and NAS cache owners do not yet attest the same complete artifact set."
            )
        return RecipeReadinessCheck(state="ready")

    def _groups_holding_first(
        self,
        recipe: LibraryRecipeProjection,
        nodes: tuple[str, ...],
        held: dict[str, Mapping[str, int] | None],
        now: datetime,
    ) -> list[tuple[tuple[str, ...], int | None]]:
        """Candidate groups with the model bytes their Sparks lack, least first.

        ``held`` is this assessment's store scan per Spark, read on first use.
        The bytes are None when the ordering read failed."""

        groups = list(combinations(nodes, recipe.node_count))
        try:
            manifest = self._model_cache.resolve_artifact_set(
                recipe_revision_id=recipe.identity.recipe_revision_id
            )
            if not held:
                with self._sessions() as session:
                    held.update(spark_stores(session, nodes, now=now))
            missing = missing_spark_bytes(manifest, held)
        except (
            SQLAlchemyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            KeyError,
        ):
            return [(group, None) for group in groups]
        ordered: list[tuple[tuple[str, ...], int | None]] = [
            (group, sum(missing[node] for node in group)) for group in groups
        ]
        return sorted(ordered, key=lambda entry: entry[1] or 0)

    def _assess(
        self,
        recipe: LibraryRecipeProjection,
        nodes: tuple[str, ...],
        held: dict[str, Mapping[str, int] | None],
        deadline: float,
    ) -> RecipeReadiness:
        observed = self._clock()
        cache, image_digest = self._cache(recipe)
        if len(nodes) < recipe.node_count:
            fit = RecipeReadinessCheck(
                state="blocked",
                reasons=[
                    _reason(
                        LibraryAssessmentCode.INSUFFICIENT_NODES,
                        f"This topology requires {recipe.node_count} Sparks; {len(nodes)} active enrolled Sparks are available.",
                    )
                ],
            )
            return RecipeReadiness(
                observed_at=observed,
                cache=cache,
                fleet_fit=fit,
                readiness=cache if cache.state == "blocked" else fit,
            )
        best: (
            tuple[RunSwitchPlan, RecipeReadinessCheck, RecipeReadinessCheck] | None
        ) = None
        unknown: RecipeReadinessCheck | None = None
        fit_unknown: RecipeReadinessCheck | None = None
        any_fit = False
        if cache.state == READY:
            candidates: list[tuple[tuple[str, ...], int | None]] = [
                (group, None) for group in combinations(nodes, recipe.node_count)
            ]
        else:
            candidates = self._groups_holding_first(recipe, nodes, held, observed)
        for group, missing_bytes in candidates:
            if time.monotonic() >= deadline:
                unknown = unavailable(
                    f"Assessment exceeded its {self._budget:g}-second read budget; narrow the library filters and retry."
                )
                fit_unknown = unknown
                break
            try:
                plan = self._run_switch.inspect_candidate(
                    recipe.identity.recipe_revision_id,
                    group,
                    actor="controller:library-assessment",
                )
                if plan.recipe_content_sha256 != recipe.identity.content_sha256:
                    raise ValueError("recipe changed during assessment")
                if time.monotonic() >= deadline:
                    unknown = unavailable(
                        f"Assessment exceeded its {self._budget:g}-second read budget; retry a narrower selection."
                    )
                    fit_unknown = unknown
                    break
                fit = self._fit(plan)
                ready = self._ready(plan, fit, cache, image_digest)
            except (
                SQLAlchemyError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                KeyError,
            ):
                unknown = unavailable(
                    "The Controller could not assess an exact placement; no work was created."
                )
                fit_unknown = unknown
                continue
            any_fit |= fit.state == READY
            if fit.state == "unavailable":
                fit_unknown = fit
            if ready.state == "unavailable":
                unknown = ready
            if best is None or fit.state == READY and best[1].state != READY:
                best = (plan, fit, ready)
            # A ready group ends the search. Otherwise, with a cache that is
            # not ready, groups are ordered by missing model bytes: once a
            # fitting group lacks some, no later group can hold all of it.
            if ready.state == READY or (
                fit.state == READY
                and (
                    cache.state == READY
                    or missing_bytes is not None
                    and missing_bytes > 0
                )
            ):
                best = (plan, fit, ready)
                break
        if best is None:
            missing = unknown or unavailable(
                "No complete placement assessment is available."
            )
            return RecipeReadiness(
                observed_at=observed,
                cache=cache,
                fleet_fit=missing,
                readiness=missing,
            )
        plan, fit, ready = best
        if not any_fit and fit_unknown is not None:
            fit = fit_unknown
        if ready.state != READY and unknown is not None:
            ready = unknown
        return RecipeReadiness(
            observed_at=observed,
            cache=cache,
            fleet_fit=fit,
            readiness=ready,
            group=plan.spark_group,
            fit=plan.fit_current,
        )
