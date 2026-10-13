"""The run timeline charges queue time and duration to the right jobs."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ci-timeline"


def _module():
    loader = importlib.machinery.SourceFileLoader("ci_timeline", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def _job(name: str, created: str, started: str | None, completed: str | None, **extra):
    return {
        "name": name,
        "created_at": created,
        "started_at": started,
        "completed_at": completed,
        "conclusion": extra.pop("conclusion", "success"),
        "labels": extra.pop("labels", ["ubuntu-24.04"]),
        **extra,
    }


def test_timeline_separates_queue_wait_from_duration_and_skips_unrun_jobs(tmp_path):
    """Catches a summary that charges runner wait to the job's duration, counts
    skipped jobs, or measures wall clock from the first start instead of the
    first request."""
    module = _module()
    jobs = [
        _job(
            "selector",
            "2026-10-13T05:00:00Z",
            "2026-10-13T05:03:00Z",
            "2026-10-13T05:04:00Z",
        ),
        _job(
            "arm package",
            "2026-10-13T05:04:00Z",
            "2026-10-13T05:05:00Z",
            "2026-10-13T05:15:00Z",
            labels=["ubuntu-24.04-arm"],
        ),
        _job("skipped lane", "2026-10-13T05:04:00Z", None, None, conclusion="skipped"),
        _job(
            "unfinished",
            "2026-10-13T05:04:00Z",
            "2026-10-13T05:05:00Z",
            None,
            conclusion=None,
        ),
    ]

    summary = module.summarize(jobs)

    assert [row.name for row in summary.jobs] == ["selector", "arm package"]
    selector, package = summary.jobs
    assert (selector.queue_minutes, selector.minutes) == (3.0, 1.0)
    assert (package.queue_minutes, package.minutes) == (1.0, 10.0)
    assert summary.wall_minutes == 15.0
    assert summary.runner_minutes == 11.0
    assert summary.queue_minutes == 4.0
    assert summary.arm_minutes == 10.0

    document = tmp_path / "jobs.json"
    document.write_text(json.dumps({"jobs": jobs}))
    text = module.render(module.summarize(jobs), markdown=True)
    assert "| +15.0 | +5.0 | 1.0 | 10.0 | success | arm package |" in text
    assert "Longest runner waits: selector (3 min)" in text
