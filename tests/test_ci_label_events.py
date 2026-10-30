"""Only the `release-acceptance` label may restart ci.yml.

GitHub cannot filter `labeled` events by label name, so ci.yml receives all of
them. Any other label must neither cancel the run in progress nor run the suite
again, and its skipped jobs must not satisfy a required check by name.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
IGNORED = "github.event.action == 'labeled' && github.event.label.name != 'release-acceptance'"
REQUIRED_CHECKS = {
    "Ruff",
    "Generated control clients",
    "Compose integration",
    "CI gate",
}


def test_other_labels_get_their_own_group_so_they_cancel_nothing() -> None:
    group = CI["concurrency"]["group"]
    assert f"({IGNORED}) && format('ignored-label-{{0}}', github.run_id)" in group
    assert "labeled" in CI[True]["pull_request"]["types"]


def test_every_root_job_skips_an_ignored_label() -> None:
    roots = [name for name, job in CI["jobs"].items() if "needs" not in job]
    assert roots, "expected root jobs"
    missing = [name for name in roots if IGNORED not in CI["jobs"][name].get("if", "")]
    assert missing == []
    assert IGNORED in CI["jobs"]["ci-gate"]["if"]


def test_required_checks_are_renamed_on_an_ignored_label() -> None:
    names = {job.get("name", "") for job in CI["jobs"].values()}
    for check in REQUIRED_CHECKS:
        (expression,) = [n for n in names if n.endswith(f"|| '{check}' }}}}")]
        assert IGNORED in expression
        assert f"'{check} (label ignored)'" in expression
