"""``model remove --unused``: remove every cached model that nothing keeps.

The Controller owns the rule for "kept" (saved and loaded profiles with their
recipes' models, live operations, running workloads): each model's fleet
removal review says whether the NAS copy may go. This module only asks that
review for every cached model, shows one combined answer, and on consent
submits the same per-model fleet removals ``model remove MODEL --fleet`` does.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass, field

from ..cli_render import render_payload
from ..control_client import (
    ControlClientError,
    ControlConflict,
    ControlMalformedResponse,
)
from ..generated_control.models.cache_removal_review import CacheRemovalReview
from ..generated_control.models.fleet_model_removal_effects import (
    FleetModelRemovalEffects,
)
from .cache_removal import (
    _cache_operation_id,
    _cache_removal_review,
    _submit_model_removal,
)
from .common import ControllerClient, _query
from .confirmation import _can_prompt, _confirm_action

_PAGE = 512
_MAX_PAGES = 64
_REVIEW_WORKERS = 4


@dataclass(frozen=True)
class ReviewedModel:
    """One cached model as its fleet removal review answered."""

    selector: str
    removable: bool
    reason: str
    nas_bytes: int | None
    sparks: dict[str, int | None]
    review_digest: str


@dataclass(frozen=True)
class Unreviewed:
    selector: str
    failure: str


@dataclass(frozen=True)
class Outcome:
    """What became of one removable model after consent."""

    selector: str
    operation_id: str | None = None
    retained: str | None = None
    failure: str | None = None


@dataclass(frozen=True)
class Totals:
    nas_bytes: int
    sparks: dict[str, int]


@dataclass(frozen=True)
class UnusedRemoval:
    """The one document of ``model remove --unused`` (unpublished, like the review)."""

    unused_removal: bool
    removals: list[ReviewedModel]
    retained: list[ReviewedModel]
    unreviewed: list[Unreviewed]
    totals: Totals
    submissions: list[Outcome] | None = field(default=None)

    def to_dict(self) -> dict:
        document = asdict(self)
        if self.submissions is None:
            del document["submissions"]
        return document


def _cached_selectors(client: ControllerClient) -> list[str]:
    selectors: list[str] = []
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        page = client.request(
            "GET",
            "/api/model/library",
            query=_query(cached=True, limit=_PAGE, cursor=cursor),
        )
        models = page.get("models")
        for item in models if isinstance(models, list) else ():
            selector = item.get("selector") if isinstance(item, dict) else None
            if isinstance(selector, str) and selector not in selectors:
                selectors.append(selector)
        next_cursor = page.get("next_cursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            break
        cursor = next_cursor
    return selectors


def _reviewed(selector: str, document: dict) -> ReviewedModel:
    review = CacheRemovalReview.from_dict(document)
    fleet = review.fleet
    if not isinstance(fleet, FleetModelRemovalEffects):
        raise ControlMalformedResponse("Fleet removal review has no Spark effects")
    nas = review.nas_reclaimable_bytes
    return ReviewedModel(
        selector=selector,
        # The Controller's own answer: its fleet review keeps nothing back.
        removable=bool(fleet.nas_remove) and not review.blockers,
        reason=fleet.nas_detail,
        nas_bytes=nas if isinstance(nas, int) else None,
        sparks={
            target.node_id: target.reclaimable_bytes
            if isinstance(target.reclaimable_bytes, int)
            else None
            for target in fleet.targets
        },
        review_digest=review.review_digest,
    )


def _review_one(client: ControllerClient, selector: str) -> ReviewedModel:
    return _reviewed(
        selector,
        _cache_removal_review(client, "model", selector, with_model=None, fleet=True),
    )


def _combined_review(client: ControllerClient) -> UnusedRemoval:
    selectors = _cached_selectors(client)

    def review(selector: str) -> ReviewedModel | Unreviewed:
        try:
            return _review_one(client, selector)
        except (ControlClientError, OSError) as error:
            # A model that cannot be reviewed is kept and reported; running
            # the command again reviews it afresh.
            return Unreviewed(selector, str(error))

    with ThreadPoolExecutor(max_workers=_REVIEW_WORKERS) as pool:
        entries = list(pool.map(review, selectors))
    reviewed = [e for e in entries if isinstance(e, ReviewedModel)]
    removals = [e for e in reviewed if e.removable]
    nodes = sorted({node for e in removals for node in e.sparks})
    return UnusedRemoval(
        unused_removal=True,
        removals=removals,
        retained=[e for e in reviewed if not e.removable],
        unreviewed=[e for e in entries if isinstance(e, Unreviewed)],
        totals=Totals(
            nas_bytes=sum(e.nas_bytes or 0 for e in removals),
            sparks={
                node: sum(e.sparks.get(node) or 0 for e in removals) for node in nodes
            },
        ),
    )


def _submit_one(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
    entry: ReviewedModel,
) -> Outcome:
    one = argparse.Namespace(**vars(args))
    one.selector = entry.selector
    one.fleet = True
    one.request_key = None
    one.fleet_review_digest = entry.review_digest
    try:
        receipt = _submit_model_removal(client, one, factory)
    except ControlConflict:
        # An earlier removal changed what this model shares: plan again with
        # the same rule and submit only if it is still unused.
        fresh = _review_one(client, entry.selector)
        if not fresh.removable:
            return Outcome(entry.selector, retained=fresh.reason)
        one.fleet_review_digest = fresh.review_digest
        receipt = _submit_model_removal(client, one, factory)
    return Outcome(entry.selector, operation_id=_cache_operation_id("model", receipt))


def _remove_unused(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
) -> dict:
    if args.selector is not None or args.request_key is not None:
        raise ValueError("model remove --unused takes no selector or request key")
    if getattr(args, "fleet", False):
        raise ValueError("model remove --unused already removes on the whole fleet")
    document = _combined_review(client)
    if args.review:
        if args.yes or args.detach:
            raise ValueError("model remove --review cannot be combined with consent")
        args.outcome_context = "read"
        return document.to_dict()
    if not args.yes and not _can_prompt(args):
        raise ValueError("model remove --unused requires --yes in noninteractive mode")
    if not document.removals:
        return document.to_dict()
    if not args.yes:
        with redirect_stdout(sys.stderr):
            render_payload(document.to_dict(), "model", action="preview")
        _confirm_action(args, f"Remove {len(document.removals)} unused models?")
    submissions: list[Outcome] = []
    for entry in document.removals:
        try:
            submissions.append(_submit_one(client, args, factory, entry))
        except (ControlClientError, OSError) as error:
            # One refusal does not stop the others; the next run reviews again.
            submissions.append(Outcome(entry.selector, failure=str(error)))
    return UnusedRemoval(
        document.unused_removal,
        document.removals,
        document.retained,
        document.unreviewed,
        document.totals,
        submissions,
    ).to_dict()
