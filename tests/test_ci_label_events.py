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


def test_only_the_always_running_gate_is_renamed_on_an_ignored_label() -> None:
    # A job skipped by its own `if` keeps its raw name expression (run
    # 40906603157, job 123574403196), so renaming a job that can be skipped
    # would stop a required check from ever being reported. ci-gate always runs.
    renamed = {key for key, job in CI["jobs"].items() if IGNORED in job.get("name", "")}
    assert renamed == {"ci-gate"}
    expression = CI["jobs"]["ci-gate"]["name"]
    assert IGNORED in expression
    assert "'CI gate (label ignored)'" in expression
    for check in REQUIRED_CHECKS - {"CI gate"}:
        assert check in {job.get("name") for job in CI["jobs"].values()}
