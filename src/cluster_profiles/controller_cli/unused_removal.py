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
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass, field

from ..cli_outcome import SubmissionAcceptance
from ..cli_render.dispatch import render_payload
from ..cli_states import PARTIAL_FIELD
from ..control_client.errors import (
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
    _model_removal_reconnect,
    _submit_model_removal,
)
from .common import ControllerClient, _query
from .confirmation import _can_prompt, _confirm_action
from .selection import _library_pages

_LIST_BUDGET_SECONDS = 60.0
_REVIEW_WORKERS = 4
_NOTHING_TO_FREE = "Nothing to free: every file is shared with another cached model."


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
    request_key: str | None = None
    operation_id: str | None = None
    retained: str | None = None
    failure: str | None = None
    # Set only when the Controller's answer is not known (not refused).
    reconnect: str | None = None


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
        elif any(item.failure is not None for item in self.submissions):
            # A refused removal must not read as success: exit non-zero.
            document[PARTIAL_FIELD] = True
        return document


def _cached_selectors(client: ControllerClient) -> list[str]:
    deadline = time.monotonic() + _LIST_BUDGET_SECONDS
    selectors: list[str] = []
    for rows in _library_pages(
        client, "/api/model/library", "models", _query(cached=True), deadline=deadline
    ):
        for row in rows:
            selector = row.get("selector") if isinstance(row, Mapping) else None
            if not isinstance(selector, str) or not selector:
                raise ControlMalformedResponse(
                    "model library response contains an invalid selector"
                )
            selectors.append(selector)
    return list(dict.fromkeys(selectors))


def _reviewed(selector: str, document: dict) -> ReviewedModel:
    review = CacheRemovalReview.from_dict(document)
    fleet = review.fleet
    if not isinstance(fleet, FleetModelRemovalEffects):
        raise ControlMalformedResponse("Fleet removal review has no Spark effects")
    nas = review.nas_reclaimable_bytes
    nas_bytes = nas if isinstance(nas, int) else None
    sparks = {
        target.node_id: target.reclaimable_bytes
        if isinstance(target.reclaimable_bytes, int)
        else None
        for target in fleet.targets
    }
    removable = bool(fleet.nas_remove) and not review.blockers
    # A removal that frees nothing anywhere changes nothing (the model stays
    # cached while a sibling holds its files), so it would be resubmitted on
    # every run: list it as kept instead.
    nothing_to_free = removable and all(
        size == 0 for size in (nas_bytes, *sparks.values())
    )
    return ReviewedModel(
        selector=selector,
        # The Controller's own answer: its fleet review keeps nothing back.
        removable=removable and not nothing_to_free,
        reason=_NOTHING_TO_FREE
        if nothing_to_free
        else review.blockers[0].detail
        if review.blockers
        else fleet.nas_detail,
        nas_bytes=nas_bytes,
        sparks=sparks,
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
    # The key exists before the submit so a refusal still names it.
    one.request_key = key = factory()
    one.fleet_review_digest = entry.review_digest
    try:
        return _submit_planned(client, one, factory, entry, key)
    except (ControlClientError, OSError) as error:
        # One refusal does not stop the others; the next run reviews again.
        submission = getattr(one, "submission", None)
        unknown = (
            submission is not None
            and submission.acceptance is SubmissionAcceptance.UNKNOWN
        )
        return Outcome(
            entry.selector,
            request_key=key,
            failure=str(error),
            reconnect=_model_removal_reconnect(entry.selector, key, fleet=True)
            if unknown
            else None,
        )


def _submit_planned(
    client: ControllerClient,
    one: argparse.Namespace,
    factory: Callable[[], str],
    entry: ReviewedModel,
    key: str,
) -> Outcome:
    try:
        receipt = _submit_model_removal(client, one, factory)
    except ControlConflict:
        # An earlier removal changed what this model shares: plan again with
        # the same rule and submit only if it is still unused.
        fresh = _review_one(client, entry.selector)
        if not fresh.removable:
            return Outcome(entry.selector, request_key=key, retained=fresh.reason)
        if fresh.review_digest == entry.review_digest:
            # Nothing changed, so an identical body would be refused again.
            raise
        one.fleet_review_digest = fresh.review_digest
        receipt = _submit_model_removal(client, one, factory)
    return Outcome(
        entry.selector,
        request_key=key,
        operation_id=_cache_operation_id("model", receipt),
    )


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
        # Sequential on purpose: Controller acceptance holds the cache lock and
        # shared objects make the order matter (a later review sees the earlier).
        submissions.append(_submit_one(client, args, factory, entry))
    return UnusedRemoval(
        document.unused_removal,
        document.removals,
        document.retained,
        document.unreviewed,
        document.totals,
        submissions,
    ).to_dict()
