"""A NAS Compose file older than the running release is fleet attention."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from vonk_control.compose_bundle import ENVIRONMENT_VARIABLE, compose_bundle
from vonk_control.metrics import MetricsRegistry

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "deploy/compose/compose.yaml"
DEV_RENDERER = ROOT / "scripts/render-dev-compose"
PRODUCTION_RENDERER = ROOT / "scripts/render-production-compose"
BUNDLE_BUILDER = ROOT / "scripts/build-nas-compose-bundle"
IMAGES = {
    f"{role}_image": f"ghcr.io/carstvaartjes/vonk-forge-{role}:v1.2.3"
    for role in ("api", "worker", "hermes", "litellm")
}
RELEASE = "a" * 64
OLDER = "b" * 64


def _embedded(tmp_path: Path, digest: str = RELEASE) -> Path:
    path = tmp_path / "compose-bundle-digest"
    path.write_text(digest + "\n", encoding="ascii")
    return path


def _stale_rule() -> dict:
    document = json.loads(
        (ROOT / "deploy/compose/prometheus/alerts.yaml").read_text(encoding="utf-8")
    )
    return next(
        rule
        for group in document["groups"]
        for rule in group["rules"]
        if rule["alert"] == "NasComposeBundleStale"
    )


def _fires(rendered: str) -> dict[str, str] | None:
    """Evaluate the rule's ``metric == 0`` against one metrics render."""
    match = re.fullmatch(r"(\w+) == 0", _stale_rule()["expr"])
    assert match is not None
    for line in rendered.splitlines():
        sample = re.fullmatch(rf"{match.group(1)}\{{(.*)\}} (\S+)", line)
        if sample is not None and float(sample.group(2)) == 0:
            return dict(re.findall(r'(\w+)="([^"]*)"', sample.group(1)))
    return None


def _render(state) -> str:
    metrics = MetricsRegistry()
    metrics.set_compose_bundle(state)
    return metrics.render()


def test_matching_compose_file_raises_no_attention(tmp_path: Path) -> None:
    state = compose_bundle({ENVIRONMENT_VARIABLE: RELEASE}, _embedded(tmp_path))

    assert state is not None and state.current
    assert _fires(_render(state)) is None


@pytest.mark.parametrize(
    ("environment", "running"),
    (
        ({ENVIRONMENT_VARIABLE: OLDER}, OLDER),
        # Today's NAS: a Compose file from before the stamp existed.
        ({}, "missing"),
        ({ENVIRONMENT_VARIABLE: "not-a-digest"}, "invalid"),
    ),
)
def test_older_or_unstamped_compose_file_is_attention_with_the_fix(
    tmp_path: Path, environment: dict[str, str], running: str
) -> None:
    state = compose_bundle(environment, _embedded(tmp_path))

    labels = _fires(_render(state))

    assert labels == {"expected": RELEASE, "running": running}
    rule = _stale_rule()
    assert rule["labels"]["severity"] == "warning"
    assert "rerun the NAS installer" in rule["annotations"]["summary"]


def test_without_an_embedded_release_digest_there_is_nothing_to_compare(
    tmp_path: Path,
) -> None:
    assert compose_bundle({}, tmp_path / "absent") is None
    assert compose_bundle({}, _embedded(tmp_path, "garbage")) is None
    assert "vonk_nas_compose_bundle_current" not in _render(None)


def _load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _embedded_digest(template: Path) -> str:
    """What the Controller image build embeds (control/Dockerfile)."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import pathlib, runpy, sys; print(runpy.run_path(sys.argv[1])"
                '["bundle_digest"](pathlib.Path(sys.argv[2])))'
            ),
            str(DEV_RENDERER),
            str(template),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize("renderer", ("production", "dev"))
def test_installer_compose_carries_the_digest_the_controller_image_embeds(
    tmp_path: Path, renderer: str
) -> None:
    rendered = tmp_path / "docker-compose.yaml"
    if renderer == "production":
        _load(PRODUCTION_RENDERER, "digest_production_renderer").render(
            TEMPLATE, rendered, **IMAGES, channel="pinned"
        )
    else:
        _load(DEV_RENDERER, "digest_dev_renderer").render(
            TEMPLATE, rendered, **IMAGES, channel="pinned"
        )
    output = tmp_path / "payload.json"
    subprocess.run(
        [
            sys.executable,
            str(BUNDLE_BUILDER),
            "--compose",
            str(rendered),
            "--output",
            str(output),
            "--channel",
            "dev",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    services = yaml.safe_load(
        json.loads(output.read_text(encoding="utf-8"))["docker_compose_yaml"]
    )["services"]
    embedded = tmp_path / "compose-bundle-digest"
    embedded.write_text(_embedded_digest(TEMPLATE) + "\n", encoding="ascii")

    state = compose_bundle(services["control-api"]["environment"], embedded)

    assert state is not None and state.current
    assert (
        services["control-worker"]["environment"][ENVIRONMENT_VARIABLE] == state.running
    )


def test_bundle_digest_follows_the_compose_graph_not_its_formatting(
    tmp_path: Path,
) -> None:
    includes = ("tailscale/compose.yaml", "hermes-agent/compose.yaml")
    for relative in ("compose.yaml", *includes):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((TEMPLATE.parent / relative).read_bytes())
    template = tmp_path / "compose.yaml"
    released = _embedded_digest(template)

    def rewrite(path: Path, change) -> None:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        change(document)
        path.write_text(yaml.safe_dump(document, sort_keys=True), encoding="utf-8")

    # Comments, anchors, key order and layout are not the graph.
    rewrite(template, lambda document: None)
    assert _embedded_digest(template) == released

    # A third-party image bump in the main file changes it.
    def bump(document: dict) -> None:
        document["services"]["caddy"]["image"] += "-next"

    rewrite(template, bump)
    bumped = _embedded_digest(template)
    assert bumped != released

    # So does a change inside an included file.
    def restart(document: dict) -> None:
        next(iter(document["services"].values()))["restart"] = "no"

    rewrite(tmp_path / includes[0], restart)
    assert _embedded_digest(template) not in {released, bumped}
