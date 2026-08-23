from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET_ROOT = ROOT / "config" / "model-targets"
RECIPE_ROOT = ROOT / "config" / "recipes"


def _targets() -> list[dict[str, object]]:
    targets: list[dict[str, object]] = []
    for path in sorted(TARGET_ROOT.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["schema_version"] == 1
        assert document["kind"] == "model-target-set"
        targets.extend(document["targets"])
    return targets


def test_target_ledger_covers_every_v1_modality_and_is_explicit_about_readiness() -> (
    None
):
    targets = _targets()
    assert {target["modality"] for target in targets} == {
        "language",
        "image",
        "three-d",
        "video",
        "audio",
    }
    assert len(targets) >= 25

    for target in targets:
        assert target["group"]
        assert target["model"]
        assert target["version"]
        assert target["status"] in {"accepted", "candidate", "blocked"}
        assert target["harnesses"]
        assert target["topologies"]
        assert target["source"]
        assert target["notes"]
        assert "excluded_territories" not in target
        if target["status"] == "accepted":
            recipes = target["recipe_slugs"]
            assert recipes
            assert all((RECIPE_ROOT / f"{slug}.json").is_file() for slug in recipes)
        if target["status"] == "blocked":
            assert target["blocker"]
            assert not target["recipe_slugs"]


def test_only_currently_qualified_deepseek_targets_are_accepted() -> None:
    targets = _targets()
    accepted = {
        (target["group"], target["version"])
        for target in targets
        if target["status"] == "accepted"
    }
    assert accepted == {("DeepSeek Flash", "V4 Flash 0731 DS4 IQ2/Q2 mixed")}

    mia = next(
        target
        for target in targets
        if target["version"] == "V4 Flash 0731 official DSpark FP4/FP8"
    )
    assert mia["status"] == "candidate"
    assert "physical canary" in str(mia["notes"])
