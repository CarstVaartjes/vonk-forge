"""Rule-based first classification of a fail-closed raise.

The blocker ratchet (``blocker_boundaries``) needs every error-class raise to
belong to a family with a category.  This module proposes that category from the
exception class, the module and the leading text of the message, so a new site
(or a module's first review) starts from a consistent answer a reviewer can
confirm or move.  It decides nothing by itself: ``--classify-new`` only appends
families for sites the allowlist does not list yet, and every proposal names the
rule that made it.

The rules follow the blocker audit and the owner's principles: fail closed only
at a security edge; reject an invalid *request* before effects; an operation that
is busy or waiting is retried by its owner; everything else (a persisted record
that does not parse, evidence that is unavailable, a receipt that is missing) is
bookkeeping that should become *unknown*: observe, reconcile, continue.

``python -m control.tests.blocker_classifier --summary`` prints the counts per
category and rule; ``--classify-new`` appends the proposed families; ``--demote-unproven`` moves
every already-retried unknown-outcome site without a proven retry loop to debt;
``--promote-proven`` never grants credit from syntax alone; product behavior
and outcome regressions must justify reviewed changes. ``--rebalance`` demotes
unsupported credit and preserves unresolved behavioral debt.
"""

from __future__ import annotations

import ast
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cache

from vonk_agent_protocol import SECURITY_REFUSAL_SUFFIXES, BlockerCategory

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
from .blocker_retries import demote_unproven, promote_proven, proven, unknown_classes

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

_SECURITY_CLASSES = frozenset(
    {
        "AuthError",
        "BrowserAuthenticationError",
        "BrowserAuthenticationThrottledError",
        "EnrollmentDenied",
        "FleetProfilePermissionDenied",
        "HostHelperAuthorityError",
        "RangeResponseError",
        "RuntimeSecretError",
        "StaleAgentAttempt",
        "StaleAttempt",
        "StepCAError",
    }
)
#: Classes that verify content at its ingress: integrity findings fail closed,
#: while an unreachable or unreadable source is bookkeeping.
_INGRESS_CLASSES = frozenset(
    {"OciImageStoreError", "RecipePackageError", "SourceBundleError"}
)
_INTEGRITY_TEXT = re.compile(
    r"digest|forbidden|invalid|corrupt|damaged|collision|mismatch|unsafe|escape|"
    r"symlink|traversal|too_(?:many|large)|unsupported|duplicate|incompatible",
    re.IGNORECASE,
)
_INPUT_CLASSES = frozenset(
    {
        "BoundedJSONError",
        "CatalogConflict",
        "CatalogRevisionContractError",
        "CatalogValidationError",
        "ClusterMappingError",
        "CompiledExecutionPlanError",
        "ContractGraphError",
        "CursorError",
        "ExecutionPlanCompilationError",
        "GatewayKeyConflict",
        "HarnessCompileError",
        "InterfaceAdapterError",
        "InstallPlanConflict",
        "LibraryProjectionError",
        "LibrarySelectorAmbiguous",
        "LiteLlmPolicyError",
        "OperationProjectionError",
        "PasswordPolicyError",
        "PresenceError",
        "RecipeExecutionContractError",
        "RecipeRuntimeSpecError",
        "RecipeSourcePolicyError",
        "RecipeStartPayloadError",
        "RequestFault",
        "RunPlanConflict",
        "RuntimeAdapterError",
        "SettingsError",
        "SourcePolicyError",
        "TopologyError",
        "_DuplicateJsonKey",
        "_RequestBodyTooLarge",
    }
)
#: Classes whose category neither the name nor the message shows; each was read.
_CLASS_VERDICTS = {
    "FleetProfileResourceRecheckUnavailable": (
        RETRIED,
        "subclass of the admission-effect busy handoff",
    ),
    "FleetProfileUnavailable": (
        INPUT,
        (
            "owner decision: a load whose fleet profile becomes unavailable ends "
            "superseded (RETRY_SUPERSEDE) so it never blocks other work; the "
            "client loads again"
        ),
    ),
    "FleetProfileReviewStale": (INPUT, "the accepted review no longer matches"),
    "RecipeReconciliationBlocked": (
        RETRIED,
        "the caller projects it as a typed blocker and the plan observes again",
    ),
    "_RunSwitchBuildParentChanged": (
        RETRIED,
        "the build parent changed: the phase replans",
    ),
}
_RETRIED_SUFFIXES = (
    "Busy",
    "InProgress",
    "NotReady",
    "Pending",
    "PreflightExpired",
    "Superseded",
)
_INPUT_MODULE_SUFFIXES = ("_contract", "_api")

_SECURITY_TEXT = re.compile(
    r"unauthori[sz]ed|not (?:been )?authori[sz]ed|no longer authori[sz]ed|"
    r"authorization (?:is )?(?:invalid|revoked|missing)|forbidden|permission|"
    r"signature|signing key|certificate|credential|secret|\btoken\b|revoked|"
    r"tombstone|fence|replay|\bnonce\b|untrusted|trusted host|redirect|symlink|"
    r"traversal|escapes|digest[ _-]?mismatch|identity[ _-]?mismatch|checksum|"
    r"authentication|private key|\bcsr\b|ed25519|nas-eviction",
    re.IGNORECASE,
)
_RETRIED_TEXT = re.compile(
    r"\bbusy\b|capacity_busy|in progress|is waiting|awaiting|being reconciled|"
    r"try again|temporar|waits for|was superseded|superseded",
    re.IGNORECASE,
)
_INPUT_TEXT = re.compile(
    r"request[ _.-]?(?:key|invalid|conflict)|key[ _](?:was[ _])?already|already used|"
    r"reuse|not[ _](?:retryable|cancellable)|plan[ _.-]?(?:stale|blocked)|"
    r"choice[ _]invalid|too[ _]long|"
    r"stale[ _.-]?plan|cursor|selector|limit is|filter|parameter[s_]|"
    r"\bmust (?:be|contain|not|use|name)\b|\brequired\b|unsupported|"
    r"unknown (?:field|parameter|interface)|too (?:large|long|many)|"
    r"already exists|duplicate|ambiguous",
    re.IGNORECASE,
)
_DEBT_TEXT = re.compile(
    r"persisted|\bstored\b|stored plan|state[ _.-]?invalid|inconsistent|damaged|"
    r"corrupt|unavailable|missing|lacks|receipt|evidence|unreadable|"
    r"uncertain|unknown|not exact|incomplete|changed",
    re.IGNORECASE,
)


#: The contract's raisable base of each category, and the family category a raise
#: of a class derived from it belongs to.  Security and invalid-request classes
#: name their category by type.  An unknown outcome does *not*: raising one is a
#: handoff only where a registered retry or observe loop is proven to catch and
#: retry it (``blocker_retries``); everywhere else it is still bookkeeping debt.
_TYPED_BASES = {
    "SecurityRefusalError": SECURITY,
    "InvalidRequestError": INPUT,
    "UnknownOutcomeError": DEBT,
}


@cache
def _typed_classes() -> dict[str, str]:
    """Local classes that derive from one of the three category bases."""

    bases: dict[str, set[str]] = defaultdict(set)
    for tree in parsed_modules(CONTROL_SOURCE_ROOT).values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        bases[node.name].add(base.id)
                    elif isinstance(base, ast.Attribute):
                        bases[node.name].add(base.attr)
    typed = dict(_TYPED_BASES)
    grew = True
    while grew:
        grew = False
        for name, parents in sorted(bases.items()):
            if name in typed:
                continue
            for parent in sorted(parents):
                if parent in typed:
                    typed[name] = typed[parent]
                    grew = True
                    break
    return typed


@dataclass(frozen=True)
class Verdict:
    category: str
    rule: str


Rule = Callable[[RaiseSite], str | None]


def _text(site: RaiseSite) -> str:
    return f"{site.code} {site.message}"


def _stem(site: RaiseSite) -> str:
    return site.path.rsplit("/", 1)[-1].removesuffix(".py")


def _security_class(site: RaiseSite) -> str | None:
    return (
        f"class {site.exception_class} guards a security boundary"
        if site.exception_class in _SECURITY_CLASSES
        else None
    )


def _ingress_class(site: RaiseSite) -> str | None:
    if site.exception_class in _INGRESS_CLASSES and _INTEGRITY_TEXT.search(_text(site)):
        return f"class {site.exception_class} verifies content at its ingress"
    return None


def _uncertain_class(site: RaiseSite) -> str | None:
    return (
        f"class {site.exception_class} is an uncertain outcome: observe, do not park"
        if site.exception_class.endswith("Uncertain")
        else None
    )


def _security_text(site: RaiseSite) -> str | None:
    text = _text(site)
    if any(suffix in site.code for suffix in SECURITY_REFUSAL_SUFFIXES):
        return "code carries a security-refusal suffix of the contract"
    if _SECURITY_TEXT.search(text):
        return "message names an authentication, signature, secret or path-safety check"
    return None


def _retried_class(site: RaiseSite) -> str | None:
    return (
        f"class {site.exception_class} is a busy/pending/superseded handoff"
        if site.exception_class.endswith(_RETRIED_SUFFIXES)
        else None
    )


def _retried_text(site: RaiseSite) -> str | None:
    return (
        "message says the work is busy, waiting or superseded"
        if _RETRIED_TEXT.search(_text(site))
        else None
    )


def _input_text(site: RaiseSite) -> str | None:
    return (
        "message rejects the shape or identity of a request"
        if _INPUT_TEXT.search(_text(site))
        else None
    )


def _debt_text(site: RaiseSite) -> str | None:
    return (
        "message reports a missing, damaged or unobservable record"
        if _DEBT_TEXT.search(_text(site))
        else None
    )


def _input_class(site: RaiseSite) -> str | None:
    if site.exception_class in _INPUT_CLASSES:
        return f"class {site.exception_class} validates a contract or request"
    if _stem(site).endswith(_INPUT_MODULE_SUFFIXES):
        return "raised in a contract or API module"
    return None


def classify(site: RaiseSite, document: dict[str, object] | None = None) -> Verdict:
    """The proposed category of ``site`` and the rule that chose it.

    Order matters.  A security class is security whatever its message; a busy or
    pending class is a handoff whatever its message.  For a class that names the
    domain only, the message decides: security words first, then a retried
    handoff, then a bad request.  A class that validates input stays input unless
    its message says a *stored or missing* record is the problem, and everything
    left is bookkeeping debt.
    """

    if site.exception_class in _CLASS_VERDICTS:
        return Verdict(*_CLASS_VERDICTS[site.exception_class])
    typed = _typed_classes().get(site.exception_class)
    if typed == DEBT and site.exception_class in unknown_classes():
        if document is not None and proven(
            document, site.path, site.exception_class, site.function
        ):
            return Verdict(
                RETRIED,
                f"class {site.exception_class} is caught and retried by a "
                "registered retry loop",
            )
        return Verdict(
            DEBT,
            f"class {site.exception_class} is an unknown outcome with no proven "
            "retry loop",
        )
    if typed is not None:
        return Verdict(
            typed, f"class {site.exception_class} is of the {typed} error type"
        )
    if (rule := _uncertain_class(site)) is not None:
        return Verdict(DEBT, rule)
    if (rule := _security_class(site)) is not None:
        return Verdict(SECURITY, rule)
    if (rule := _ingress_class(site)) is not None:
        return Verdict(SECURITY, rule)
    if (rule := _retried_class(site)) is not None:
        return Verdict(RETRIED, rule)
    if (rule := _security_text(site)) is not None:
        return Verdict(SECURITY, rule)
    if (rule := _retried_text(site)) is not None:
        return Verdict(RETRIED, rule)
    input_rule = _input_class(site)
    if input_rule is not None:
        if _stored_record(site):
            return Verdict(DEBT, "validation class, but a stored record is the problem")
        return Verdict(INPUT, input_rule)
    if (rule := _input_text(site)) is not None:
        return Verdict(INPUT, rule)
    return Verdict(DEBT, _debt_text(site) or "operation bookkeeping by default")


_STORED = re.compile(r"persisted|\bstored\b|unavailable|missing|receipt", re.IGNORECASE)


def _stored_record(site: RaiseSite) -> bool:
    return bool(_STORED.search(_text(site)))


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
        families.append(
            {
                "family": f"{stem}.{category}",
                "category": category,
                "reason": (
                    f"{CATEGORY_REASONS[category]} Rule-classified, then reviewed: "
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
