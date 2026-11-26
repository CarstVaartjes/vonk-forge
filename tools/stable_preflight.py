"""Stable release preflight: a pure checker over a snapshot of GitHub state.

``scripts/stable-preflight`` reads the repository settings with ``gh api`` and
hands the :class:`Snapshot` to :func:`check`, which returns every problem.
Nothing here touches the network, Git or the filesystem, and nothing writes any
state.
"""

from __future__ import annotations

import re
from typing import TypedDict


class EnvironmentState(TypedDict):
    """One GitHub environment: its policies, setting names and reviewers."""

    policies: list[str]
    secrets: list[str]
    variables: dict[str, str]
    reviewers: list[str]
    prevent_self_review: bool | None


class ImageState(TypedDict):
    """The `dev-sha-<sha>` tag digest (None if absent) and attestation result."""

    digest: str | None
    attested: bool


class Snapshot(TypedDict):
    """Everything the checks read; ``behind_by`` is from compare ``<sha>...main``."""

    tag: str
    sha: str
    variables: dict[str, str]
    environments: dict[str, EnvironmentState | None]
    workspace_version: str
    behind_by: int
    release_conclusion: str | None
    images: dict[str, ImageState]
    signers_bytes: int


TAG = "tag:v*"
MAIN = "branch:main"
SWITCHES = ("VONK_CONTAINER_RELEASES_ENABLED", "VONK_PLATFORM_RELEASES_ENABLED")
POLICIES: dict[str, frozenset[str]] = {
    "agent-release": frozenset({TAG}),
    "apt-release": frozenset({TAG}),
    "platform-release": frozenset({TAG}),
    "installer-candidate-stable": frozenset({TAG}),
    "installer-acceptance-stable": frozenset({TAG}),
    "installer-canary-stable": frozenset({TAG, MAIN}),
    "installer-promotion-stable": frozenset({TAG, MAIN}),
}
TWINS: dict[str, str] = {
    "agent-development": "agent-release",
    "apt-development": "apt-release",
    "installer-candidate-dev": "installer-candidate-stable",
    "installer-canary-dev": "installer-canary-stable",
    "installer-acceptance-dev": "installer-acceptance-stable",
    "installer-promotion-dev": "installer-promotion-stable",
}
# Names a dev twin may hold without its stable twin: the optional tailnet
# cleanup credential and the canary leftovers no workflow reads.
ALLOWED_DEV_ONLY_PREFIXES = ("VONK_ACCEPTANCE_TAILNET_CLEANUP_",)
ALLOWED_DEV_ONLY_NAMES = frozenset(
    {
        "VONK_ACCEPTANCE_TAILNET_DNS_SUFFIX",
        "VONK_ACCEPTANCE_TAILSCALE_OIDC_AUDIENCE",
        "VONK_ACCEPTANCE_TAILSCALE_OIDC_CLIENT_ID",
        "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_ID",
        "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET",
    }
)
IMAGES = ("api", "worker", "litellm")
ACCEPTANCE_FINGERPRINT = "VONK_INSTALLER_ACCEPTANCE_KEY_FINGERPRINT"
ACCEPTANCE_FINGERPRINT_ENVIRONMENTS = (
    "installer-acceptance-stable",
    "installer-promotion-stable",
)
AGENT_FINGERPRINT_ENVIRONMENTS = ("agent-development", "agent-release")
AGENT_FINGERPRINT = "VONK_AGENT_RELEASE_KEY_FINGERPRINT"
REVIEWER_ENVIRONMENTS = ("installer-acceptance-stable", "installer-promotion-stable")
_TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")


def _environment(snapshot: Snapshot, name: str) -> EnvironmentState | None:
    return snapshot["environments"].get(name)


def _switches(snapshot: Snapshot) -> list[str]:
    return [
        f"repository variable {name} is not 'true'"
        for name in SWITCHES
        if snapshot["variables"].get(name) != "true"
    ]


def _policies(snapshot: Snapshot) -> list[str]:
    problems: list[str] = []
    for name, expected in POLICIES.items():
        environment = _environment(snapshot, name)
        if environment is None:
            problems.append(f"environment {name} does not exist")
        elif frozenset(environment["policies"]) != expected:
            problems.append(
                f"environment {name} deployment policies are "
                f"{sorted(environment['policies'])}, expected {sorted(expected)}"
            )
    return problems


def _allowed_dev_only(name: str) -> bool:
    return name in ALLOWED_DEV_ONLY_NAMES or name.startswith(ALLOWED_DEV_ONLY_PREFIXES)


def _parity(snapshot: Snapshot) -> list[str]:
    problems: list[str] = []
    for dev, stable in TWINS.items():
        dev_env, stable_env = (
            _environment(snapshot, dev),
            _environment(snapshot, stable),
        )
        if dev_env is None or stable_env is None:
            continue  # a missing stable environment is reported by the policy check
        for kind, dev_names, stable_names in (
            ("secret", set(dev_env["secrets"]), set(stable_env["secrets"])),
            ("variable", set(dev_env["variables"]), set(stable_env["variables"])),
        ):
            problems.extend(
                f"{kind} {name} is in {dev} but not in {stable}"
                for name in sorted(dev_names - stable_names)
                if not _allowed_dev_only(name)
            )
    return problems


def _fingerprints(snapshot: Snapshot) -> list[str]:
    problems: list[str] = []
    for name, left, right in (
        (ACCEPTANCE_FINGERPRINT, *ACCEPTANCE_FINGERPRINT_ENVIRONMENTS),
        (AGENT_FINGERPRINT, *AGENT_FINGERPRINT_ENVIRONMENTS),
    ):
        values = [
            (environment["variables"].get(name) if environment else None)
            for environment in (
                _environment(snapshot, left),
                _environment(snapshot, right),
            )
        ]
        if values[0] is None or values[0] != values[1]:
            problems.append(f"{name} differs or is missing between {left} and {right}")
    return problems


def _reviewers(snapshot: Snapshot, owner: str, reviewer_environment: str) -> list[str]:
    problems: list[str] = []
    for name in ("apt-release", reviewer_environment):
        environment = _environment(snapshot, name)
        if environment is None:
            continue  # reported by the policy check
        if owner not in environment["reviewers"]:
            problems.append(f"{owner} is not a required reviewer on {name}")
        if environment["prevent_self_review"] is not False:
            problems.append(f"{name} must set prevent_self_review to false")
    return problems


def _source(snapshot: Snapshot) -> list[str]:
    problems: list[str] = []
    match = _TAG_RE.fullmatch(snapshot["tag"])
    if match is None or snapshot["workspace_version"] != match.group(1):
        problems.append(
            f"workspace version at the commit is {snapshot['workspace_version']!r}, "
            f"the tag is {snapshot['tag']!r}"
        )
    if snapshot["behind_by"] != 0:
        problems.append("the commit is not an ancestor of main")
    if snapshot["release_conclusion"] != "success":
        problems.append(
            f"main Release run for the commit concluded {snapshot['release_conclusion']!r}"
        )
    for name in IMAGES:
        image = snapshot["images"].get(name)
        if image is None or image["digest"] is None:
            problems.append(f"image {name} has no dev-sha tag for the commit")
        elif not image["attested"]:
            problems.append(f"image {name} attestation does not verify for the commit")
    if snapshot["signers_bytes"] <= 0:
        problems.append(".github/release-allowed-signers on main is empty")
    return problems


def check(
    snapshot: Snapshot,
    *,
    owner: str = "CarstVaartjes",
    reviewer_environment: str = "installer-acceptance-stable",
) -> list[str]:
    """Return every problem the snapshot shows; empty means ready."""

    if reviewer_environment not in REVIEWER_ENVIRONMENTS:
        raise ValueError(f"reviewer environment must be one of {REVIEWER_ENVIRONMENTS}")
    return [
        *_switches(snapshot),
        *_policies(snapshot),
        *_parity(snapshot),
        *_fingerprints(snapshot),
        *_reviewers(snapshot, owner, reviewer_environment),
        *_source(snapshot),
    ]
