from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".github/dependabot.yml"


def test_dependabot_covers_every_container_and_action_location_weekly() -> None:
    config = yaml.safe_load(CONFIG.read_text())
    assert config["version"] == 2
    required = {
        "docker": {"/control", "/deploy/compose/hermes-agent"},
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
