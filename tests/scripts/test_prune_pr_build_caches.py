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
SCRIPT = ROOT / "scripts/ci/prune-pr-build-caches"
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
    page or an already-deleted version red instead of pruning the rest."""
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
            if endpoint.endswith("/6"):
                # Already gone (another run deleted it): done, not a failure.
                raise subprocess.CalledProcessError(1, args, stderr="(HTTP 404)")
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


def test_a_spent_budget_stops_deletes_and_reports_the_rest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catches a pruner that keeps deleting past its time budget (the job is
    then cancelled red) or exits non-zero or silently when it stops early."""
    module = _module()
    deleted: list[str] = []

    def api(*args: str) -> str:
        endpoint = args[-1]
        if args[:2] == ("--method", "DELETE"):
            deleted.append(endpoint)
            return ""
        if "/pulls?" in endpoint:
            return ""
        if endpoint == PACKAGE:
            return json.dumps({"version_count": 5})
        return json.dumps([_version(i, f"buildcache-pr-{i}") for i in range(1, 6)])

    # The clock reads the start, then once before each delete: two fit the budget.
    ticks = iter([0, 0, 1, module.RUN_BUDGET_SECONDS, module.RUN_BUDGET_SECONDS])
    assert module.prune(api, "owner", "owner/repo", clock=lambda: next(ticks)) == 0
    assert deleted == [f"{PACKAGE}/versions/1", f"{PACKAGE}/versions/2"]
    assert "::notice::time budget spent; 3 closed caches remain" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize(
    "stderr",
    ["gh: Not Found (HTTP 404)", "gh: To use GitHub CLI, set the GH_TOKEN variable"],
)
def test_a_real_failure_is_not_skipped(stderr: str) -> None:
    """Catches a pruner that hides a 4xx refusal or a non-HTTP gh error (a
    missing token, a usage error) as housekeeping noise."""
    module = _module()

    def api(*args: str) -> str:
        raise subprocess.CalledProcessError(1, ["gh", "api", *args], stderr=stderr)

    with pytest.raises(subprocess.CalledProcessError):
        module.prune(api, "owner", "owner/repo")
