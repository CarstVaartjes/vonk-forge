"""Read-only Library assessments composed from the actual Controller owners."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from itertools import combinations

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .library_contract import (
    LibraryRecipeProjection,
    RecipeReadiness,
    RecipeReadinessCheck,
)
from .model_cache import ModelCacheService
from .models import AgentNode
from .run_switch_contract import RunSwitchPlan, RunSwitchReason
from .run_switch_operations import RunSwitchOperationService


def _reason(code: str, detail: str) -> RunSwitchReason:
    return RunSwitchReason(
        code=code, detail=detail, severity="blocker", scope="operation"
    )


def unavailable(detail: str) -> RecipeReadinessCheck:
    return RecipeReadinessCheck(
        state="unavailable", reasons=[_reason("library.assessment_unavailable", detail)]
    )


def unassessed(clock: Callable[[], datetime], detail: str) -> RecipeReadiness:
    unknown = unavailable(detail)
    return RecipeReadiness(
        observed_at=clock(), fleet_fit=unknown, cache=unknown, readiness=unknown
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
        budget_seconds: float = 5.0,
    ) -> None:
        if type(budget_seconds) not in (int, float) or not 0 < budget_seconds <= 10:
            raise ValueError("library assessment budget must be in (0, 10] seconds")
        self._sessions = sessions
        self._run_switch = run_switch
        self._model_cache = model_cache
        self._clock = clock
        self._budget = budget_seconds

    def __call__(
        self, recipes: Sequence[LibraryRecipeProjection]
    ) -> list[LibraryRecipeProjection]:
        deadline = time.monotonic() + self._budget
        try:
            with self._sessions() as session:
                nodes = tuple(
                    session.scalars(
                        select(AgentNode.node_id)
                        .where(
                            AgentNode.state == "active",
                            AgentNode.revoked_at.is_(None),
                        )
                        .order_by(AgentNode.node_id)
                    )
                )
        except (SQLAlchemyError, OSError, RuntimeError, TypeError, ValueError):
            return [
                item.model_copy(
                    update={
                        "assessment": unassessed(
                            self._clock,
                            "The authorized fleet roster could not be read.",
                        )
                    }
                )
                for item in recipes
            ]
        result = []
        for recipe in recipes:
            if time.monotonic() >= deadline:
                assessment = unassessed(
                    self._clock,
                    f"Assessment exceeded its {self._budget:g}-second read budget; narrow the library filters and retry.",
                )
            else:
                assessment = self._assess(recipe, nodes, deadline)
            result.append(recipe.model_copy(update={"assessment": assessment}))
        return result

    def _cache(
        self, recipe: LibraryRecipeProjection
    ) -> tuple[RecipeReadinessCheck, str | None]:
        try:
            resolved = self._model_cache.resolve_latest_cached(
                recipe_identity=recipe.identity.recipe_id,
                exact_revision_id=recipe.identity.recipe_revision_id,
            )
            image = resolved.get("recipe")
            model = resolved.get("model")
            blockers = resolved.get("blockers")
            if (
                not isinstance(image, Mapping)
                or not isinstance(model, Mapping)
                or not isinstance(blockers, list)
            ):
                raise TypeError("cache resolution is invalid")
            if (
                image.get("recipe_revision_id") != recipe.identity.recipe_revision_id
                or image.get("content_sha256") != recipe.identity.content_sha256
                or model.get("content_sha256")
                != recipe.document.models[0].model.content_sha256
            ):
                raise ValueError("cache resolution changed an exact identity")
            if (
                type(image.get("cached")) is not bool
                or type(model.get("cached")) is not bool
            ):
                raise TypeError("cache availability is invalid")
            if not all(isinstance(reason, str) for reason in blockers):
                raise TypeError("cache blockers are invalid")
            # The profile resolver supplies revision/image authority. Its
            # primary-model cache set alone cannot prove that every selected
            # file and companion dependency in this recipe is present.
            preview = self._model_cache.download_preview(
                model_content_sha256=recipe.document.models[0].model.content_sha256,
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
                digest = image.get("image_digest")
                if (
                    not image["cached"]
                    or not model["cached"]
                    or not isinstance(digest, str)
                ):
                    raise ValueError("cache availability contradicts its blockers")
                return RecipeReadinessCheck(state="ready"), digest
            return RecipeReadinessCheck(
                state="blocked",
                reasons=[
                    _reason(
                        "library.cache_missing",
                        f"{reason} for {recipe.selector}; prepare the exact assets with vonkctl recipe download {recipe.selector}.",
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
                        "library.capacity_unavailable",
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
        if cache.state == "blocked":
            return cache
        if fit.state == "blocked":
            return fit
        if cache.state == "unavailable" or fit.state == "unavailable":
            return cache if cache.state == "unavailable" else fit
        if not plan.allowed:
            return RecipeReadinessCheck(state="blocked", reasons=plan.blockers)
        if (
            plan.image_digest != image_digest
            or plan.preparation is None
            or not plan.preparation.controller_ready
        ):
            return unavailable(
                "The placement and NAS cache owners do not yet attest the same complete artifact set."
            )
        return RecipeReadinessCheck(state="ready")

    def _assess(
        self, recipe: LibraryRecipeProjection, nodes: tuple[str, ...], deadline: float
    ) -> RecipeReadiness:
        observed = self._clock()
        cache, image_digest = self._cache(recipe)
        if len(nodes) < recipe.node_count:
            fit = RecipeReadinessCheck(
                state="blocked",
                reasons=[
                    _reason(
                        "library.insufficient_nodes",
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
        for group in combinations(nodes, recipe.node_count):
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
            any_fit |= fit.state == "ready"
            if fit.state == "unavailable":
                fit_unknown = fit
            if ready.state == "unavailable":
                unknown = ready
            if best is None or fit.state == "ready" and best[1].state != "ready":
                best = (plan, fit, ready)
            if (
                ready.state == "ready"
                or fit.state == "ready"
                and cache.state != "ready"
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
                readiness=cache if cache.state == "blocked" else missing,
            )
        plan, fit, ready = best
        if not any_fit and fit_unknown is not None:
            fit = fit_unknown
        if ready.state != "ready" and unknown is not None and cache.state != "blocked":
            ready = unknown
        return RecipeReadiness(
            observed_at=observed,
            cache=cache,
            fleet_fit=fit,
            readiness=ready,
            group=plan.spark_group,
            fit=plan.fit_current,
        )
