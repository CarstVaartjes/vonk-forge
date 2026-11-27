from __future__ import annotations

import json
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tools.stable_preflight import (
    POLICIES,
    TWINS,
    EnvironmentState,
    ImageState,
    Snapshot,
    check,
)

FACTORY = (
    "VONK_ACCEPTANCE_TAILNET_FACTORY_OAUTH_CLIENT_ID",
    "VONK_ACCEPTANCE_TAILNET_FACTORY_OAUTH_CLIENT_SECRET",
)
ACCEPTANCE = "VONK_INSTALLER_ACCEPTANCE_KEY_FINGERPRINT"
AGENT = "VONK_AGENT_RELEASE_KEY_FINGERPRINT"
VERSION_FILES = (
    "Cargo.toml",
    "pyproject.toml",
    "control/pyproject.toml",
    "nas_setup/pyproject.toml",
)


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
        "version_files": dict.fromkeys(VERSION_FILES, "1.0.0"),
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


def test_every_gap_kind_is_reported() -> None:
    gaps = complete()
    environments = gaps["environments"]
    environments["platform-release"] = None
    environments["installer-acceptance-stable"] = None
    _live(gaps, "agent-release")["policies"] = ["branch:v*"]
    _live(gaps, "apt-release")["policies"] = ["branch:v*"]
    for name in (
        "installer-candidate-stable",
        "installer-canary-stable",
        "installer-promotion-stable",
    ):
        _live(gaps, name)["policies"] = []
    _live(gaps, "installer-promotion-stable")["variables"] = {}
    _live(gaps, "installer-canary-dev")["secrets"] += list(FACTORY)
    _live(gaps, "installer-promotion-dev")["variables"] = {ACCEPTANCE: "b" * 64}

    subjects = sorted(problem.subject for problem in check(gaps))

    assert subjects == sorted(
        [
            "platform-release",
            "agent-release",
            "apt-release",
            "installer-candidate-stable",
            "installer-acceptance-stable",
            "installer-canary-stable",
            "installer-promotion-stable",
            f"installer-canary-stable/{FACTORY[0]}",
            f"installer-canary-stable/{FACTORY[1]}",
            f"installer-promotion-stable/{ACCEPTANCE}",
            ACCEPTANCE,
        ]
    )


def test_allowed_dev_only_names_are_not_problems() -> None:
    state = complete()
    _live(state, "installer-canary-dev")["secrets"] += [
        "VONK_ACCEPTANCE_TAILNET_CLEANUP_OAUTH_CLIENT_ID",
        "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET",
    ]
    assert check(state) == []


def test_each_source_input_is_checked() -> None:
    state = complete()
    state["version_files"] = dict.fromkeys(VERSION_FILES, "0.1.1")
    state["behind_by"] = 3
    state["release_conclusion"] = "failure"
    state["signers_bytes"] = 0
    state["images"]["worker"]["attested"] = False
    state["images"]["litellm"]["digest"] = None
    subjects = sorted(problem.subject for problem in check(state))
    assert subjects == sorted(
        ["tag", "main-ancestry", "release", "image/worker", "image/litellm", "signers"]
    )


def _script() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts/release/stable-preflight"
    loader = SourceFileLoader("stable_preflight_script", str(path))
    module = ModuleType(loader.name)
    module.__file__ = str(path)
    loader.exec_module(module)
    return module


def test_an_environment_without_a_branch_policy_reads_as_no_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _script()
    answers = {
        "": {"protection_rules": []},
        "/secrets?per_page=100": {"secrets": []},
        "/variables?per_page=100": {"variables": []},
    }

    def run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        suffix = command[-1].removeprefix("repos/o/r/environments/e")
        if suffix == "/deployment-branch-policies":
            return subprocess.CompletedProcess(command, 1, "", "gh: HTTP 404")
        return subprocess.CompletedProcess(command, 0, json.dumps(answers[suffix]), "")

    monkeypatch.setattr(script.subprocess, "run", run)

    state = script.environment_state("o/r", "e")

    assert state is not None
    assert state["policies"] == []


def test_the_image_digest_is_read_for_linux_whatever_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _script()
    monkeypatch.setattr(script.platform, "machine", lambda: "arm64")
    commands: list[list[str]] = []

    def run(command: list[str], **_: Any) -> str:
        commands.append(command)
        if command[0] == "gh":
            return ""
        return "sha256:" + "c" * 64 + "\n"

    monkeypatch.setattr(script, "run", run)

    state = script.image_state("o/r", "api", "0" * 40)

    assert state["attested"] is True
    inspect = commands[0]
    assert inspect[:2] == ["skopeo", "inspect"]
    assert inspect[inspect.index("--override-os") + 1] == "linux"
    assert inspect[inspect.index("--override-arch") + 1] == "arm64"


def test_disagreeing_version_files_are_one_gap_among_the_others() -> None:
    state = complete()
    state["version_files"]["nas_setup/pyproject.toml"] = "1.0.1"
    state["signers_bytes"] = 0
    subjects = sorted(problem.subject for problem in check(state))
    assert subjects == ["signers", "version-files"]


def test_the_preflight_reads_every_version_file_at_the_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _script()
    files = {
        "Cargo.toml": '[workspace.package]\nversion = "1.0.0"\n',
        "pyproject.toml": '[project]\nversion = "1.0.0"\n',
        "control/pyproject.toml": '[project]\nversion = "1.0.0"\n',
        "nas_setup/pyproject.toml": '[project]\nversion = "1.0.1"\n',
    }
    monkeypatch.setattr(script, "file_text", lambda repo, path, ref: files[path])

    assert script.version_files("o/r", "0" * 40) == {
        "Cargo.toml": "1.0.0",
        "pyproject.toml": "1.0.0",
        "control/pyproject.toml": "1.0.0",
        "nas_setup/pyproject.toml": "1.0.1",
    }
