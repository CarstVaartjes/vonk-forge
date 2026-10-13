"""A security refusal is terminal, so only a reviewed class at a real edge may be one.

Every class in ``control/src`` that derives from ``SecurityRefusalError`` is
listed here with the closed security edges it guards.  A new subclass, or a
class re-based onto drift, fails until it is reviewed; bookkeeping drift (an
owner that is unavailable, state that changed, evidence that is unreadable)
belongs to a wait, a conflict or an unknown instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

from vonk_agent_protocol import SecurityRefusalReason
from vonk_control.failure_classification import is_security_failure
from vonk_control.security_edges import SECURITY_REFUSAL_EDGES, SecurityEdge

from .added_line_guards import check_source

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "control/src"

_AUTHN = SecurityEdge.AUTHENTICATION
_AUTHZ = SecurityEdge.AUTHORISATION
_IDENTITY = SecurityEdge.IDENTITY
_REVOKED = SecurityEdge.REVOCATION
_SIGNED = SecurityEdge.SIGNED_METADATA
_DIGEST = SecurityEdge.INGRESS_DIGEST
_FENCE = SecurityEdge.FENCE

#: The reviewed security classes and the edges their raises guard.
SECURITY_CLASSES: dict[str, frozenset[SecurityEdge]] = {
    "vonk_control.agent_jobs.contracts:AgentContactIdentityMismatch": frozenset(
        {_IDENTITY}
    ),
    "vonk_control.agent_jobs.contracts:StaleAgentFence": frozenset({_FENCE, _IDENTITY}),
    "vonk_control.agent_upgrades.errors:AgentUpgradeRefused": frozenset(
        {_SIGNED, _IDENTITY}
    ),
    "vonk_control.artifact_blob_store:ArtifactBlobDigestMismatch": frozenset({_DIGEST}),
    "vonk_control.artifact_blob_store:ArtifactBlobUnsafePath": frozenset({_AUTHZ}),
    "vonk_control.artifact_jobs.contracts:ArtifactJobRefused": frozenset(
        {_IDENTITY, _AUTHZ, _DIGEST}
    ),
    "vonk_control.artifact_jobs.contracts:ArtifactResultRefused": frozenset(
        {_IDENTITY}
    ),
    "vonk_control.auth:AuthError": frozenset({_AUTHN, _AUTHZ}),
    "vonk_control.categorized_errors:SecurityRefused": frozenset(SecurityEdge),
    "vonk_control.distribution.types:DistributionIntegrityError": frozenset(
        {_DIGEST, _SIGNED}
    ),
    "vonk_control.distribution.types:DistributionRefused": frozenset(
        {_AUTHZ, _REVOKED, _SIGNED}
    ),
    "vonk_control.enrollment.types:EnrollmentDenied": frozenset(
        {_AUTHN, _AUTHZ, _IDENTITY, _REVOKED}
    ),
    "vonk_control.enrollment.types:ExpiredRenewalGraceExhausted": frozenset(
        {_IDENTITY}
    ),
    "vonk_control.fleet_profiles.contracts:FleetProfilePermissionDenied": frozenset(
        {_AUTHZ}
    ),
    "vonk_control.host_runtime_plan_authority:RuntimePlanAuthorityRefused": frozenset(
        {_SIGNED}
    ),
    "vonk_control.jobs:StaleAttempt": frozenset({_FENCE}),
    "vonk_control.model_cache.errors:ModelCacheCredentialPathUnsafe": frozenset(
        {_AUTHN, _AUTHZ}
    ),
    "vonk_control.model_cache.errors:ModelCacheDeletionFenceLost": frozenset({_FENCE}),
    "vonk_control.model_cache.errors:ModelCacheResolutionRefused": frozenset({_AUTHZ}),
    "vonk_control.model_cache.errors:ModelCacheStorageRefused": frozenset(
        {_AUTHN, _AUTHZ, _DIGEST}
    ),
    "vonk_control.model_cache.errors:_CacheRefusal": frozenset(
        {_AUTHN, _AUTHZ, _DIGEST}
    ),
    "vonk_control.recipe_builds.common:RecipeBuildRefused": frozenset(
        {_DIGEST, _AUTHZ}
    ),
    "vonk_control.recipe_image_availability.contracts:RecipeImageAvailabilityRefused": frozenset(
        {_AUTHZ, _SIGNED}
    ),
    "vonk_control.recipe_routes.shared:RecipeEndpointAuthorityRefused": frozenset(
        {_AUTHZ, _REVOKED}
    ),
    "vonk_control.run_switch_operations.errors:RunSwitchRefused": frozenset(
        {_DIGEST, _AUTHZ}
    ),
    "vonk_control.run_switch_operations.errors:_RuntimeImageIdentityMismatch": frozenset(
        {_DIGEST}
    ),
    "vonk_control.runtime_image_preparation.contracts:RuntimeImagePreparationRefused": frozenset(
        {_DIGEST}
    ),
    "vonk_control.source_bundles:SourceBundleIntegrityRefused": frozenset({_DIGEST}),
    "vonk_control.source_bundles:SourceBundleRefused": frozenset({_AUTHZ}),
}


def _modules() -> list[tuple[str, str]]:
    modules = []
    for path in SOURCE.rglob("*.py"):
        module = ".".join(path.relative_to(SOURCE).with_suffix("").parts)
        modules.append((module.removesuffix(".__init__"), path.read_text()))
    return modules


def _security_subclasses(modules: list[tuple[str, str]]) -> set[str]:
    """Every class that derives from SecurityRefusalError, also through an
    import alias (``import SecurityRefusalError as Denied``)."""

    classes: dict[str, set[str]] = {}
    aliases: dict[str, str] = {}
    for module, source in modules:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ClassDef):
                classes[f"{module}:{node.name}"] = {
                    ast.unparse(base).split(".")[-1] for base in node.bases
                }
            elif isinstance(node, ast.ImportFrom):
                aliases.update(
                    (alias.asname, alias.name) for alias in node.names if alias.asname
                )
    names = {"SecurityRefusalError"}
    found: set[str] = set()
    while True:
        names |= {alias for alias, owner in aliases.items() if owner in names}
        new = {
            key for key, bases in classes.items() if key not in found and bases & names
        }
        if not new:
            return found
        found |= new
        names |= {key.partition(":")[2] for key in new}


def test_every_security_refusal_class_is_reviewed() -> None:
    found = _security_subclasses(_modules())
    assert found - set(SECURITY_CLASSES) == set(), (
        "a new SecurityRefusalError subclass must guard a reviewed security edge"
    )
    assert set(SECURITY_CLASSES) - found == set(), "remove retired classes"
    assert all(edges for edges in SECURITY_CLASSES.values())
    # An aliased or indirect subclass is found too.
    aliased = (
        "vonk_control.example",
        (
            "from vonk_agent_protocol import SecurityRefusalError as Denied\n"
            "class Refusal(Denied): pass\n"
            "class Indirect(Refusal): pass\n"
        ),
    )
    assert _security_subclasses([aliased]) == {
        "vonk_control.example:Refusal",
        "vonk_control.example:Indirect",
    }


def test_every_security_reason_names_its_edge() -> None:
    assert set(SECURITY_REFUSAL_EDGES) == set(SecurityRefusalReason)
    assert all(is_security_failure(reason.value) for reason in SecurityRefusalReason)


def test_bookkeeping_drift_is_never_added_as_a_security_refusal() -> None:
    path = "control/src/vonk_control/security/example.py"
    for message in (
        "current profile Stop owner is unavailable",
        "accepted Stop document is unreadable",
        "profile Stop parent payload digest changed",
        "accepted Stop disappeared",
    ):
        source = f"raise SecurityRefused({message!r})\n"
        assert check_source(path, source, {1}, modes=("security",)) == [
            f"{path}:1: security refusal for bookkeeping drift"
        ]
    # A real edge at a declared ingress owner stays a refusal.
    assert not check_source(
        path,
        "raise SecurityRefused('bearer token is invalid')\n",
        {1},
        modes=("security",),
    )
    # The same drift raised as a wait or a conflict passes.
    assert not check_source(
        "control/src/vonk_control/example.py",
        "raise RecipeRetryLater('current profile Stop owner is unavailable')\n",
        {1},
        modes=("security",),
    )
