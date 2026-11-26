"""The pull-request build-cache pruner deletes only what no open PR reads."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/prune-pr-build-caches"
PACKAGE = "/users/owner/packages/container/vonk-forge-api"


def _module():
    loader = importlib.machinery.SourceFileLoader("prune_pr_build_caches", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _version(identifier: int, *tags: str) -> dict[str, Any]:
    return {"id": identifier, "metadata": {"container": {"tags": list(tags)}}}


def test_only_closed_caches_are_deleted_and_a_slow_page_waits_a_week() -> None:
    """Catches a pruner that deletes an open pull request's cache, main's cache
    or a version that also carries another tag, or one that turns a timed-out
    page red instead of pruning the other pages."""
    module = _module()
    pages = {
        1: [
            _version(1, "buildcache"),
            _version(2, "buildcache-pr-10"),
            _version(3, "buildcache-pr-11"),
            _version(4, "buildcache-pr-12", "keep-me"),
            _version(5),
        ],
        3: [_version(6, "buildcache-pr-13")],
    }
    deleted: list[str] = []

    def api(*args: str) -> str:
        endpoint = args[-1]
        if args[:2] == ("--method", "DELETE"):
            deleted.append(endpoint)
            return ""
        if "/pulls?" in endpoint:
            return "11\n"
        if endpoint == PACKAGE:
            return json.dumps({"version_count": 201})
        page = int(endpoint.rsplit("page=", 1)[1])
        if page == 2:
            raise subprocess.TimeoutExpired(["gh", "api", endpoint], 60)
        return json.dumps(pages[page])

    assert module.prune(api, "owner", "owner/repo") == 0
    assert deleted == [f"{PACKAGE}/versions/2", f"{PACKAGE}/versions/6"]


def test_a_refused_listing_fails() -> None:
    """Catches a pruner that hides a real 4xx refusal as housekeeping noise."""
    module = _module()

    def api(*args: str) -> str:
        raise subprocess.CalledProcessError(
            1, ["gh", "api", *args], stderr="gh: Not Found (HTTP 404)"
        )

    with pytest.raises(subprocess.CalledProcessError):
        module.prune(api, "owner", "owner/repo")
