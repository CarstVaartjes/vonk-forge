from __future__ import annotations

from tools.stable_preflight import (
    POLICIES,
    TWINS,
    EnvironmentState,
    ImageState,
    Snapshot,
    check,
)

FACTORY = ("VONK_ACCEPTANCE_TAILNET_FACTORY_OAUTH_CLIENT_ID",)
ACCEPTANCE = "VONK_INSTALLER_ACCEPTANCE_KEY_FINGERPRINT"
AGENT = "VONK_AGENT_RELEASE_KEY_FINGERPRINT"


def _environment(policies: set[str]) -> EnvironmentState:
    return {
        "policies": sorted(policies),
        "secrets": ["VONK_X"],
        "variables": {},
        "reviewers": [],
        "prevent_self_review": None,
    }


def _image() -> ImageState:
    return {"digest": "sha256:" + "c" * 64, "attested": True}


def _live(state: Snapshot, name: str) -> EnvironmentState:
    environment = state["environments"][name]
    assert environment is not None
    return environment


def complete() -> Snapshot:
    environments: dict[str, EnvironmentState | None] = {
        "platform-release": _environment({"tag:v*"})
    }
    for dev, stable in TWINS.items():
        environments[stable] = _environment(set(POLICIES[stable]))
        environments[dev] = _environment({"branch:main"})
    state: Snapshot = {
        "tag": "v1.0.0",
        "sha": "0" * 40,
        "variables": {
            "VONK_CONTAINER_RELEASES_ENABLED": "true",
            "VONK_PLATFORM_RELEASES_ENABLED": "true",
        },
        "environments": environments,
        "workspace_version": "1.0.0",
        "behind_by": 0,
        "release_conclusion": "success",
        "images": {name: _image() for name in ("api", "worker", "litellm")},
        "signers_bytes": 90,
    }
    for name in ("agent-development", "agent-release"):
        _live(state, name)["variables"] = {AGENT: "a" * 64}
    for name in ("installer-acceptance-stable", "installer-promotion-stable"):
        _live(state, name)["variables"] = {ACCEPTANCE: "b" * 64}
    for name in ("apt-release", "installer-acceptance-stable"):
        _live(state, name)["reviewers"] = ["CarstVaartjes"]
        _live(state, name)["prevent_self_review"] = False
    return state


def test_a_complete_shape_has_no_problems() -> None:
    assert check(complete()) == []


def test_the_live_shape_of_2026_11_26_yields_exactly_its_gaps() -> None:
    live = complete()
    environments = live["environments"]
    environments["platform-release"] = None
    environments["installer-acceptance-stable"] = None
    _live(live, "agent-release")["policies"] = ["branch:v*"]
    _live(live, "apt-release")["policies"] = ["branch:v*"]
    for name in (
        "installer-candidate-stable",
        "installer-canary-stable",
        "installer-promotion-stable",
    ):
        _live(live, name)["policies"] = []
    _live(live, "installer-promotion-stable")["variables"] = {}
    _live(live, "installer-canary-dev")["secrets"] += list(FACTORY)
    _live(live, "installer-promotion-dev")["variables"] = {ACCEPTANCE: "b" * 64}

    problems = check(live)

    expected = [
        ("platform-release",),
        ("agent-release", "branch:v*"),
        ("apt-release", "branch:v*"),
        ("installer-candidate-stable", "[]"),
        ("installer-acceptance-stable",),
        ("installer-canary-stable", "[]"),
        ("installer-promotion-stable", "[]"),
        (FACTORY[0], "installer-canary-stable"),
        (ACCEPTANCE, "installer-promotion-stable"),
        (ACCEPTANCE, "installer-promotion-stable"),
    ]
    assert len(problems) == len(expected)
    for tokens in expected:
        assert any(all(t in problem for t in tokens) for problem in problems), tokens


def test_allowed_dev_only_names_are_not_problems() -> None:
    state = complete()
    _live(state, "installer-canary-dev")["secrets"] += [
        "VONK_ACCEPTANCE_TAILNET_CLEANUP_OAUTH_CLIENT_ID",
        "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET",
    ]
    assert check(state) == []


def test_each_source_input_is_checked() -> None:
    state = complete()
    state["workspace_version"] = "0.1.1"
    state["behind_by"] = 3
    state["release_conclusion"] = "failure"
    state["signers_bytes"] = 0
    state["images"]["worker"]["attested"] = False
    state["images"]["litellm"]["digest"] = None
    assert len(check(state)) == 6
