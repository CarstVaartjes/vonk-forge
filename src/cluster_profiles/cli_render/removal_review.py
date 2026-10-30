"""Terminal presentation of a cache-removal review.

The default is the answer an operator needs before consenting: what is removed,
what is kept and why, what blocks, and the exact next command. ``--verbose``
(``--wide``) lists every asset and every owner finding, one line per shared
object. The Controller owns every value shown; nothing is classified here.
"""

from __future__ import annotations

import shlex
from collections import defaultdict
from collections.abc import Iterable, Sequence

from ..cli_states_generated import (
    ASSET_MODEL_OBJECT,
    ASSET_MODEL_SET,
    ASSET_REMOVE,
    ASSET_RETAIN_REFERENCED,
    ASSET_RETAIN_SHARED,
    ASSET_RUNTIME_IMAGE,
    ASSET_VERIFIED,
)
from ..generated_control.models.cache_removal_asset import CacheRemovalAsset
from ..generated_control.models.cache_removal_finding import CacheRemovalFinding
from ..generated_control.models.cache_removal_review import CacheRemovalReview
from ..generated_control.models.fleet_model_removal_effects import (
    FleetModelRemovalEffects,
)
from ..generated_control.types import UNSET
from .common import _actions, _bytes, _field, _object, _time

_NAMED_OWNERS = 3


def _size(assets: Sequence[CacheRemovalAsset]) -> str:
    known = [a.available_bytes for a in assets if isinstance(a.available_bytes, int)]
    text = _bytes(sum(known))
    unknown = len(assets) - len(known)
    return text if not unknown else f"{text} measured; {unknown} not measured"


def _owner(finding: CacheRemovalFinding) -> str:
    return f"{finding.owner_kind} {finding.owner_id}"


def _owners(findings: Iterable[CacheRemovalFinding]) -> str:
    names = sorted({_owner(f) for f in findings})
    shown = ", ".join(names[:_NAMED_OWNERS])
    extra = len(names) - _NAMED_OWNERS
    return shown if extra <= 0 else f"{shown} and {extra} more"


def _confirm_command(review: CacheRemovalReview) -> str:
    parts = [
        "vonkctl",
        review.resource_kind,
        str(review.action),
        shlex.quote(review.selector),
    ]
    if isinstance(review.fleet, FleetModelRemovalEffects):
        parts.append("--fleet")
    if review.with_model is not None:
        parts.append("--with-model" if review.with_model else "--keep-model")
    return " ".join(parts)


def _group(
    findings: Iterable[CacheRemovalFinding],
) -> dict[tuple[str, str], list[CacheRemovalFinding]]:
    groups: dict[tuple[str, str], list[CacheRemovalFinding]] = defaultdict(list)
    for finding in findings:
        groups[(finding.owner_kind, finding.state)].append(finding)
    return dict(sorted(groups.items()))


def _fleet(
    fleet: FleetModelRemovalEffects, nas_bytes: object, *, verbose: bool
) -> None:
    _field("NAS removal", fleet.nas_remove)
    _field("NAS bytes freed", _bytes(nas_bytes))
    _field("NAS reason", fleet.nas_detail)
    for target in fleet.targets:
        _field("Spark", target.node_id)
        _field("  Observation available", target.observation_available)
        _field("  Model bytes freed", _bytes(target.reclaimable_bytes))
        if verbose:
            for installation in target.installations:
                _field("  Effect", installation.disposition)
                _field("  Reason", installation.detail)
                _field("  Installation", installation.installation_id)
            for stored in target.objects:
                _field("  Effect", stored.disposition)
                _field("  Reason", stored.detail)
                _field("  Object", stored.object_.to_dict())
            continue
        effects: dict[str, int] = defaultdict(int)
        for item in (*target.installations, *target.objects):
            effects[item.disposition] += 1
        if effects:
            _field(
                "  Effects", ", ".join(f"{n} {k}" for k, n in sorted(effects.items()))
            )


def _summary(
    review: CacheRemovalReview, sharing: Sequence[CacheRemovalFinding]
) -> None:
    removed = [a for a in review.assets if a.disposition == ASSET_REMOVE]
    sets = [a for a in removed if a.kind == ASSET_MODEL_SET]
    # A set's bytes total its member objects, so bytes and files count objects.
    files = [a for a in removed if a.kind == ASSET_MODEL_OBJECT]
    images = [a for a in removed if a.kind == ASSET_RUNTIME_IMAGE]
    line = f"Removes {_size(files + images)} in {len(files)} files ({len(sets)} model sets)"
    if images:
        line += f" and {len(images)} runtime images"
    print(line)
    states: dict[str, int] = defaultdict(int)
    for asset in removed:
        states[asset.availability] += 1
    if set(states) - {ASSET_VERIFIED}:
        # Incomplete bytes are part of consent: say how much is not ready.
        print(
            "Readiness: "
            + ", ".join(
                k if len(states) == 1 else f"{n} {k}" for k, n in sorted(states.items())
            )
        )
    kept = [a for a in review.assets if a.disposition == ASSET_RETAIN_SHARED]
    if kept:
        users = {f.owner_id for f in sharing}
        print(
            f"Keeps {len(kept)} shared files ({_size(kept)}) also used by "
            f"{len(users)} other cached models"
        )
        for (owner_kind, state), group in _group(sharing).items():
            print(
                f"  {owner_kind} ({state}): {len({f.asset_sha256 for f in group})} "
                f"files kept for {len({f.owner_id for f in group})} owners"
            )
    referenced = [a for a in review.assets if a.disposition == ASSET_RETAIN_REFERENCED]
    if referenced:
        print(f"Keeps {len(referenced)} assets that a saved reference still uses")


def _blockers(
    review: CacheRemovalReview,
    blocking: Sequence[CacheRemovalFinding],
    active: Sequence[CacheRemovalFinding],
) -> bool:
    explained = {f.reason for f in (*blocking, *active)}
    for heading, findings in (("saved reference", blocking), ("active work", active)):
        for (owner_kind, state), group in _group(findings).items():
            print(f"Blocked by {heading} {owner_kind} ({state}): {_owners(group)}")
    for blocker in review.blockers:
        if blocker.detail not in explained:
            print(f"Blocked: {blocker.code}: {blocker.detail}")
            _actions(
                None if blocker.recovery_actions is UNSET else blocker.recovery_actions
            )
    return bool(review.blockers or blocking or active)


def _detail(
    review: CacheRemovalReview,
    sharing: Sequence[CacheRemovalFinding],
    blocking: Sequence[CacheRemovalFinding],
) -> None:
    for asset in review.assets:
        print("Asset:")
        _field("  Kind", asset.kind)
        _field("  SHA-256", asset.sha256)
        _field("  Disposition", asset.disposition)
        _field("  Readiness", asset.availability)
        _field(
            "  Expected bytes",
            _bytes(None if asset.expected_bytes is UNSET else asset.expected_bytes),
        )
        _field(
            "  Available bytes",
            _bytes(None if asset.available_bytes is UNSET else asset.available_bytes),
        )
    by_object: dict[tuple[str, str], list[CacheRemovalFinding]] = defaultdict(list)
    for finding in sharing:
        by_object[(finding.asset_kind, finding.asset_sha256)].append(finding)
    for (kind, sha), group in sorted(by_object.items()):
        names = ", ".join(sorted({_owner(f) for f in group}))
        print(f"Shared {kind} {sha}: kept for {names}")
    for heading, findings in (
        ("Saved references", blocking),
        ("Active work", review.active_work),
    ):
        for finding in findings:
            print(f"{heading}:")
            _field("  Classification", finding.classification)
            _field("  Asset", f"{finding.asset_kind} {finding.asset_sha256}")
            _field("  Owner", _owner(finding))
            _field("  State", finding.state)
            _field("  Detail", finding.detail)
            _field("  Reason", finding.reason)


def _cache_removal_review(payload: object, *, verbose: bool = False) -> None:
    """Present the owning cache policy's removal review without interpretation."""
    review = CacheRemovalReview.from_dict(_object(payload, "removal review"))
    _field("Action", review.action)
    _field("Resource", review.resource_kind)
    _field("Selector", review.selector)
    _field("Target identity", review.target_identity)
    if review.with_model is not None:
        _field("Remove dependent model", review.with_model)
    _field("Observed", _time(review.observed_at))
    if isinstance(review.fleet, FleetModelRemovalEffects):
        nas_bytes = (
            None
            if review.nas_reclaimable_bytes is UNSET
            else review.nas_reclaimable_bytes
        )
        _fleet(review.fleet, nas_bytes, verbose=verbose)
    shared = {
        (a.kind, a.sha256)
        for a in review.assets
        if a.disposition == ASSET_RETAIN_SHARED
    }
    sharing = [r for r in review.references if (r.asset_kind, r.asset_sha256) in shared]
    blocking = [
        r for r in review.references if (r.asset_kind, r.asset_sha256) not in shared
    ]
    if not review.assets:
        print("Assets: none")
    _summary(review, sharing)
    blocked = _blockers(review, blocking, review.active_work)
    if review.active_work:
        print(f"Active work: {len(review.active_work)} ({_owners(review.active_work)})")
    else:
        print("Active work: none")
    command = _confirm_command(review)
    if blocked:
        print("Blockers: resolve the owners above, then review again:")
        print(f"  Next: {command} --review")
    else:
        print("Blockers: none")
        print(f"Confirm: {command} --yes --detach")
    if verbose:
        print("Detail:")
        _detail(review, sharing, blocking)
    else:
        print("Next: add --verbose to list every file and owner")
    _field("Review digest", review.review_digest)
