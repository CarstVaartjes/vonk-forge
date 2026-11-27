"""Stable release preflight: a pure checker over a snapshot of GitHub state.

``scripts/stable-preflight`` reads the repository settings with ``gh api`` and
hands the :class:`Snapshot` to :func:`check`, which returns every problem.
Nothing here touches the network, Git or the filesystem, and nothing writes any
state.
"""

from __future__ import annotations

import re
from typing import NamedTuple, TypedDict


class Problem(NamedTuple):
    """One gap: what it is about (an environment, a setting, an image) and why."""

    subject: str
    detail: str


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
    #: Each version file's version at the commit, by repository path.
    version_files: dict[str, str]
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


def _switches(snapshot: Snapshot) -> list[Problem]:
    return [
        Problem(name, "repository variable is not 'true'")
        for name in SWITCHES
        if snapshot["variables"].get(name) != "true"
    ]


def _policies(snapshot: Snapshot) -> list[Problem]:
    problems: list[Problem] = []
    for name, expected in POLICIES.items():
        environment = _environment(snapshot, name)
        if environment is None:
            problems.append(Problem(name, "environment does not exist"))
        elif frozenset(environment["policies"]) != expected:
            problems.append(
                Problem(
                    name,
                    f"deployment policies are {sorted(environment['policies'])}, "
                    f"expected {sorted(expected)}",
                )
            )
    return problems


def _allowed_dev_only(name: str) -> bool:
    return name in ALLOWED_DEV_ONLY_NAMES or name.startswith(ALLOWED_DEV_ONLY_PREFIXES)


def _parity(snapshot: Snapshot) -> list[Problem]:
    problems: list[Problem] = []
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
                Problem(f"{stable}/{name}", f"{kind} is in {dev} but not here")
                for name in sorted(dev_names - stable_names)
                if not _allowed_dev_only(name)
            )
    return problems


def _fingerprints(snapshot: Snapshot) -> list[Problem]:
    problems: list[Problem] = []
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
            problems.append(
                Problem(name, f"differs or is missing between {left} and {right}")
            )
    return problems


def _reviewers(
    snapshot: Snapshot, owner: str, reviewer_environment: str
) -> list[Problem]:
    problems: list[Problem] = []
    for name in ("apt-release", reviewer_environment):
        environment = _environment(snapshot, name)
        if environment is None:
            continue  # reported by the policy check
        if owner not in environment["reviewers"]:
            problems.append(
                Problem(f"{name}/reviewer", f"{owner} is not a required reviewer")
            )
        if environment["prevent_self_review"] is not False:
            problems.append(Problem(f"{name}/prevent_self_review", "must be false"))
    return problems


def _source(snapshot: Snapshot) -> list[Problem]:
    problems: list[Problem] = []
    match = _TAG_RE.fullmatch(snapshot["tag"])
    versions = snapshot["version_files"]
    if len(set(versions.values())) != 1:
        found = ", ".join(f"{path} {version}" for path, version in versions.items())
        problems.append(
            Problem(
                "version-files", f"the version files disagree at the commit: {found}"
            )
        )
    elif match is None or set(versions.values()) != {match.group(1)}:
        problems.append(
            Problem(
                "tag",
                f"workspace version at the commit is {next(iter(versions.values()))}, "
                f"the tag is {snapshot['tag']}",
            )
        )
    if snapshot["behind_by"] != 0:
        problems.append(
            Problem("main-ancestry", "the commit is not an ancestor of main")
        )
    if snapshot["release_conclusion"] != "success":
        problems.append(
            Problem(
                "release",
                f"main Release run for the commit concluded {snapshot['release_conclusion']}",
            )
        )
    for name in IMAGES:
        image = snapshot["images"].get(name)
        if image is None or image["digest"] is None:
            problems.append(
                Problem(f"image/{name}", "has no dev-sha tag for the commit")
            )
        elif not image["attested"]:
            problems.append(
                Problem(f"image/{name}", "attestation does not verify for the commit")
            )
    if snapshot["signers_bytes"] <= 0:
        problems.append(
            Problem("signers", ".github/release-allowed-signers on main is empty")
        )
    return problems


def check(
    snapshot: Snapshot,
    *,
    owner: str = "CarstVaartjes",
    reviewer_environment: str = "installer-acceptance-stable",
) -> list[Problem]:
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
