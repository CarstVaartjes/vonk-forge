from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService, RecipeDraftInput
from vonk_control.models import Base
from vonk_control.recipe_contract import validate_recipe
from vonk_control.source_bundles import SourceBundleStore

PRESETS_PATH = (
    Path(__file__).parents[1]
    / "web"
    / "src"
    / "pages"
    / "custom-recipe-presets.json"
)


def _merge_patch(base: Any, patch: Any) -> Any:
    """Mirror the builder's recursive preset merge without mutating fixtures."""
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(base)
    for key, value in patch.items():
        result[key] = (
            _merge_patch(result[key], value)
            if key in result
            else copy.deepcopy(value)
        )
    return result


def _preset_documents() -> list[tuple[str, dict[str, object]]]:
    payload = json.loads(PRESETS_PATH.read_text(encoding="utf-8"))
    return [
        (name, _merge_patch(payload["base"], patch))
        for name, patch in payload["presets"].items()
    ]


@pytest.mark.parametrize(("preset", "document"), _preset_documents())
def test_builder_presets_pass_recipe_v1_and_catalog_create(
    tmp_path: Path, preset: str, document: dict[str, object]
) -> None:
    """Keep the browser payload pinned to the real backend create contract."""
    validate_recipe(document)
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    service = CatalogService(
        sessionmaker(engine, expire_on_commit=False),
        clock=lambda: datetime(2026, 8, 24, tzinfo=UTC),
        cursors=TokenCodec(b"b" * 32).cursor_codec(),
        source_bundles=SourceBundleStore(tmp_path / "source-bundles"),
    )

    created = service.create_recipe(
        "admin",
        RecipeDraftInput(slug=str(document["identity"]["slug"]), document=document),
    )

    assert created.lifecycle == "draft"
    assert created.document == document
    assert created.document["artifacts"]
    assert created.document["interfaces"]
    assert created.document["validation"]["validators"]
    assert preset in {"custom", "vllm", "diffusers"}
