from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".github/dependabot.yml"


def test_dependabot_covers_every_container_and_action_location_weekly() -> None:
    config = yaml.safe_load(CONFIG.read_text())
    assert config["version"] == 2
    required = {
        "docker": {"/control"},
        "docker-compose": {"/deploy/compose", "/deploy/compose/tailscale"},
        "github-actions": {"/"},
        "cargo": {"/"},
    }
    covered: dict[str, set[str]] = {}
    for update in config["updates"]:
        assert update["schedule"]["interval"] == "weekly"
        assert "automerge" not in update
        directories = update.get("directories", [update.get("directory")])
        covered.setdefault(update["package-ecosystem"], set()).update(directories)
    for ecosystem, directories in required.items():
        assert directories <= covered[ecosystem]


DEPENDABOT_SKIP = "github.event.pull_request.user.login != 'dependabot[bot]'"
WORKFLOWS = ROOT / ".github/workflows"


def test_a_dependabot_pull_request_schedules_no_runner() -> None:
    # Its update lands through an integration pull request, which runs CI once.
    # Every job that could start on its own on a pull request (no `needs`, or
    # `always()` and not push-only) skips it; every proof is a ci.yml job.
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        if not {"pull_request", "pull_request_target"} & set(workflow[True]):
            continue
        for name, job in workflow["jobs"].items():
            condition = str(job.get("if", ""))
            if "github.event_name == 'push'" in condition and "||" not in condition:
                continue
            if (
                "needs" not in job
                or "always()" in condition
                or "cancelled()" in condition
            ):
                assert DEPENDABOT_SKIP in condition, (path.name, name)
