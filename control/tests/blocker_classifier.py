"""Inventory proposals without semantic credit from names or diagnostic text.

Only the connected all-callers retry proof can establish automatic retry credit.
A proposal is not behavioral review or completion evidence.
"""

from __future__ import annotations

import ast
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from vonk_agent_protocol import BlockerCategory

from .blocker_boundaries import (
    ALLOWLIST_PATH,
    CONTROL_SOURCE_ROOT,
    REPO_ROOT,
    RaiseSite,
    _raise_keys,
    audited_paths,
    dump_document,
    load_allowlist,
    parsed_modules,
    scan_raises,
)
from .blocker_retries import demote_unproven, promote_proven, proven

SECURITY = BlockerCategory.SECURITY_EDGE.value
INPUT = BlockerCategory.INPUT_VALIDATION.value
RETRIED = BlockerCategory.ALREADY_RETRIED.value
DEBT = BlockerCategory.BOOKKEEPING_DEBT.value

#: What a category means, as the reason a family carries.
CATEGORY_REASONS = {
    SECURITY: (
        "Fail closed by design: authentication, authorization, signature, digest "
        "at ingress, path or secret safety, certificate authority, fence authority "
        "or a destructive-effect owner guard."
    ),
    INPUT: (
        "Rejects an invalid or out-of-contract request, document or configuration "
        "at the boundary that owns it, before any effect; the caller fixes the "
        "input and asks again."
    ),
    RETRIED: (
        "Busy, waiting or superseded: the owner of the operation retries or "
        "re-observes, so the raise is a handoff and never a dead end."
    ),
    DEBT: (
        "Bookkeeping, unavailable evidence or a damaged record: this should become "
        "an unknown outcome that is observed and reconciled, not a refusal. Counted "
        "against the debt ceiling until it is converted."
    ),
}


@dataclass(frozen=True)
class Verdict:
    category: str
    rule: str


def classify(site: RaiseSite, document: dict[str, object] | None = None) -> Verdict:
    """Only an actual all-callers retry proof can earn automatic credit.

    Security and caller-input edges require review of effects and ingress.
    Names, ancestry, messages and file extensions establish none of those facts.
    Unreviewed proposals remain inventory, never evidence of completion.
    """
    if document is not None and proven(
        document, site.path, site.exception_class, site.function
    ):
        return Verdict(RETRIED, "every caller reaches a registered observe/retry owner")
    return Verdict(DEBT, "no caller/effect/authority/reconciliation proof supplied")


# ------------------------------------------------------------------ families


def propose_families(
    sites: Sequence[RaiseSite], document: dict[str, object]
) -> list[dict[str, object]]:
    """Families for the sites ``document`` does not list, one per module and category."""

    listed = {
        (path, exception, function, code)
        for family in document["fail_closed"]  # type: ignore[attr-defined]
        for path, exception, function, code, _count in family["sites"]
    }
    grouped: dict[tuple[str, str], Counter[tuple[str, str, str, str]]] = defaultdict(
        Counter
    )
    rules: dict[tuple[str, str], set[str]] = defaultdict(set)
    for site in sites:
        if site.identity in listed:
            continue
        verdict = classify(site, document)
        grouped[(site.path, verdict.category)][site.identity] += 1
        rules[(site.path, verdict.category)].add(verdict.rule)
    families: list[dict[str, object]] = []
    for (path, category), counts in sorted(grouped.items()):
        stem = path.rsplit("/", 1)[-1].removesuffix(".py")
        review = "Unreviewed inventory: "
        families.append(
            {
                "family": f"{path if path.endswith('.rs') else stem}.{category}",
                "category": category,
                "reason": (
                    f"{CATEGORY_REASONS[category]} {review}"
                    + "; ".join(sorted(rules[(path, category)]))
                    + "."
                ),
                "sites": [[*key, count] for key, count in sorted(counts.items())],
            }
        )
    return families


# ------------------------------------------------------------------- summary

_PROGRAMMING = frozenset({"ValueError", "KeyError", "TypeError", "AssertionError"})


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return "?"


def unscanned_raises() -> Counter[str]:
    """Raises that are not an error-class family, by kind (for the summary)."""

    sites = {(site.path, site.line) for site in scan_raises()}
    kinds: Counter[str] = Counter()
    for module, tree in parsed_modules(CONTROL_SOURCE_ROOT).items():
        relative = module.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise):
                continue
            if node.exc is None:
                kinds["bare re-raise"] += 1
            elif (relative, node.lineno) in sites:
                continue
            else:
                name = _call_name(node.exc)
                if name == "HTTPException":
                    kinds["HTTPException (HTTP mapping layer)"] += 1
                elif name in _PROGRAMMING:
                    kinds[
                        "ValueError/KeyError/TypeError (programming or contract guard)"
                    ] += 1
                elif name[:1].islower() or name[:1] == "_":
                    kinds[
                        "raise through a factory function or private control-flow class"
                    ] += 1
                elif name in {
                    "RuntimeError",
                    "OSError",
                    "InterruptedError",
                    "TimeoutError",
                }:
                    kinds[
                        "RuntimeError/OSError/InterruptedError (invariant or I/O)"
                    ] += 1
                else:
                    kinds[f"other ({name})"] += 1
    return kinds


def summary(document: dict[str, object]) -> str:
    audited = audited_paths(document)
    sites = scan_raises()
    listed = _raise_keys(document["fail_closed"])  # type: ignore[arg-type]
    table: Counter[tuple[str, str]] = Counter()
    for site in sites:
        category = (
            listed[site.identity][1]
            if site.identity in listed
            else classify(site, document).category
        )
        table[("audited" if site.path in audited else "other", category)] += 1
    lines = [f"error-class raise sites: {len(sites)}"]
    for place in ("audited", "other"):
        for category in (SECURITY, INPUT, RETRIED, DEBT):
            lines.append(f"  {place:8} {category:18} {table[(place, category)]}")
    lines.append("raises that are not an error-class family:")
    for kind, count in unscanned_raises().most_common():
        lines.append(f"  {count:5} {kind}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    document = load_allowlist()
    if arguments == ["--summary"]:
        print(summary(document))
        return 0
    if arguments == ["--classify-new"]:
        families = propose_families(scan_raises(), document)
        document["fail_closed"].extend(families)  # type: ignore[attr-defined]
        ALLOWLIST_PATH.write_text(
            dump_document(document, record_content=True), encoding="utf-8"
        )
        added = sum(len(family["sites"]) for family in families)  # type: ignore[arg-type]
        print(f"appended {len(families)} families covering {added} site keys")
        return 0
    if arguments == ["--demote-unproven"]:
        moved = demote_unproven(document)
        ALLOWLIST_PATH.write_text(
            dump_document(document, record_content=True), encoding="utf-8"
        )
        print(f"moved {moved} unproven already-retried sites to bookkeeping-debt")
        return 0
    if arguments == ["--promote-proven"]:
        moved = promote_proven(document)
        ALLOWLIST_PATH.write_text(
            dump_document(document, record_content=True), encoding="utf-8"
        )
        print(f"moved {moved} proven unknown-outcome sites out of bookkeeping-debt")
        return 0
    if arguments == ["--rebalance"]:
        demoted = demote_unproven(document)
        promoted = promote_proven(document)
        ALLOWLIST_PATH.write_text(
            dump_document(document, record_content=True), encoding="utf-8"
        )
        print(f"moved {demoted} sites to bookkeeping-debt and {promoted} out of it")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
