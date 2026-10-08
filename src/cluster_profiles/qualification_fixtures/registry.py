"""Digest-bound fixture registry loading and lookup."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path

from .contracts import (
    _DIGEST,
    _KEY,
    _MEDIA_TYPE,
    _NAME,
    Fixture,
    FixtureError,
    RecipeFixture,
    ServiceCase,
    ServiceRecipe,
    _validate_manifest_contract,
)
from .media import _load_content, _validate_magic
from .recipe_cases import _blocker, _parse_recipe_fixture
from .service_cases import _parse_service_case, _parse_service_recipe
from .values import _object, _strict_json_loads


def _fixture_format(media_type: str) -> str | None:
    return {
        "application/json": "json",
        "audio/wav": "wav",
        "image/jpeg": "jpeg",
        "image/png": "png",
        "model/gltf-binary": "glb",
        "video/mp4": "mp4",
    }.get(media_type)


class FixtureRegistry:
    def __init__(
        self,
        fixtures: Mapping[str, Fixture],
        recipes: Mapping[str, RecipeFixture],
        special: Mapping[str, Mapping[str, object]],
        *,
        service_cases: Mapping[str, ServiceCase] | None = None,
        service_recipes: Mapping[str, ServiceRecipe] | None = None,
    ) -> None:
        self.fixtures = dict(fixtures)
        self.recipes = dict(recipes)
        self.special = {key: dict(value) for key, value in special.items()}
        self.service_cases = dict(service_cases or {})
        self.service_recipes = dict(service_recipes or {})

    @classmethod
    def load(cls, path: Path) -> FixtureRegistry:
        try:
            raw = path.read_bytes()
            value = _strict_json_loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise FixtureError(
                "qualification fixture manifest is unreadable"
            ) from error
        document = _object(value, "qualification fixture manifest")
        _validate_manifest_contract(document)
        if document.get("schema_version") != 2:
            raise FixtureError("qualification fixture schema_version must be 2")
        fixture_root = path.resolve().parent
        raw_fixtures = _object(document.get("fixtures"), "fixtures")
        fixtures: dict[str, Fixture] = {}
        for fixture_id, raw_fixture in raw_fixtures.items():
            if not isinstance(fixture_id, str) or not _NAME.fullmatch(fixture_id):
                raise FixtureError("fixture ID is invalid")
            item = _object(raw_fixture, f"fixture {fixture_id}")
            if set(item) - {
                "path",
                "encoding",
                "name",
                "media_type",
                "size_bytes",
                "sha256",
                "provenance",
            }:
                raise FixtureError(f"fixture {fixture_id} fields are invalid")
            content = _load_content(fixture_root, item)  # type: ignore[arg-type]
            name = item.get("name")
            media_type = item.get("media_type")
            digest = item.get("sha256")
            size = item.get("size_bytes")
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise FixtureError(f"fixture {fixture_id} name is invalid")
            if not isinstance(media_type, str) or not _MEDIA_TYPE.fullmatch(media_type):
                raise FixtureError(f"fixture {fixture_id} media type is invalid")
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                raise FixtureError(f"fixture {fixture_id} digest is invalid")
            if size != len(content) or hashlib.sha256(content).hexdigest() != digest:
                raise FixtureError(f"fixture {fixture_id} identity does not match")
            format_name = _fixture_format(media_type)
            if format_name is not None:
                _validate_magic(content, format_name)
            provenance_value = item.get("provenance")
            if provenance_value is None:
                raise FixtureError(f"fixture {fixture_id} provenance is required")
            provenance: dict[str, str] | None = None
            if provenance_value is not None:
                source = _object(provenance_value, f"fixture {fixture_id} provenance")
                if set(source) != {
                    "origin",
                    "source_url",
                    "source_revision",
                    "license_spdx",
                    "attribution",
                }:
                    raise FixtureError(
                        f"fixture {fixture_id} provenance shape is invalid"
                    )
                if source.get("origin") not in {"generated", "upstream"}:
                    raise FixtureError(
                        f"fixture {fixture_id} provenance origin is invalid"
                    )
                source_url = source.get("source_url")
                source_revision = source.get("source_revision")
                license_spdx = source.get("license_spdx")
                attribution = source.get("attribution")
                if (
                    not isinstance(source_url, str)
                    or not (
                        source_url.startswith("https://")
                        or source_url == "urn:vonk:qualification-fixture"
                    )
                    or not isinstance(source_revision, str)
                    or not _DIGEST.fullmatch(source_revision)
                    and re.fullmatch(r"[0-9a-f]{40}", source_revision) is None
                    or license_spdx not in {"CC-BY-4.0", "CC0-1.0"}
                    or not isinstance(attribution, str)
                    or not 1 <= len(attribution) <= 256
                ):
                    raise FixtureError(f"fixture {fixture_id} provenance is invalid")
                provenance = {str(key): str(value) for key, value in source.items()}
            fixtures[fixture_id] = Fixture(
                fixture_id,
                str(item["path"]),
                str(item["encoding"]),
                name,
                media_type,
                len(content),
                digest,
                content,
                provenance,
            )
        raw_recipes = _object(document.get("recipes"), "recipes")
        recipes = {
            key: _parse_recipe_fixture(key, raw_recipe, fixtures)
            for key, raw_recipe in raw_recipes.items()
        }
        raw_special = _object(document.get("special_fixtures", {}), "special_fixtures")
        special: dict[str, Mapping[str, object]] = {}
        for key, raw_special_item in raw_special.items():
            if not isinstance(key, str) or not _KEY.fullmatch(key):
                raise FixtureError("special fixture recipe key is invalid")
            item = _object(raw_special_item, f"special fixture {key}")
            content_sha256 = item.get("content_sha256")
            detail = item.get("detail")
            if (
                not isinstance(content_sha256, str)
                or not _DIGEST.fullmatch(content_sha256)
                or not isinstance(detail, str)
                or not 1 <= len(detail) <= 512
            ):
                raise FixtureError(f"special fixture {key} is invalid")
            special[key] = item
        if set(recipes) & set(special):
            raise FixtureError(
                "recipe cannot have both executable and special fixtures"
            )
        raw_service_cases = _object(
            document.get("service_case_templates", {}), "service_case_templates"
        )
        service_cases = {
            key: _parse_service_case(key, item)
            for key, item in raw_service_cases.items()
        }
        raw_service_recipes = _object(
            document.get("service_recipes", {}), "service_recipes"
        )
        service_recipes = {
            key: _parse_service_recipe(key, item, service_cases)
            for key, item in raw_service_recipes.items()
        }
        if (set(service_recipes) & set(recipes)) or (
            set(service_recipes) & set(special)
        ):
            raise FixtureError("recipe cannot have both artifact and service fixtures")

        used_fixtures = {
            fixture.fixture_id
            for recipe in recipes.values()
            for case in recipe.all_cases
            for _, fixture in case.inputs
        }

        def collect_service_fixture_references(value: object) -> None:
            if isinstance(value, Mapping):
                for field, child in value.items():
                    if field in {"$fixture_data_uri", "$fixture_base64"}:
                        if isinstance(child, str):
                            used_fixtures.add(child)
                    else:
                        collect_service_fixture_references(child)
            elif isinstance(value, list | tuple):
                for child in value:
                    collect_service_fixture_references(child)

        for recipe in service_recipes.values():
            for case in recipe.cases:
                collect_service_fixture_references(case.body)
                collect_service_fixture_references(case.assertions)
        unused_fixtures = sorted(set(fixtures) - used_fixtures)
        if unused_fixtures:
            raise FixtureError(
                "qualification fixture manifest declares unused fixtures: "
                + ", ".join(unused_fixtures)
            )
        return cls(
            fixtures,
            recipes,
            special,
            service_cases=service_cases,
            service_recipes=service_recipes,
        )

    def resolve(
        self, key: str, content_sha256: str, interface: str
    ) -> tuple[RecipeFixture | None, dict[str, str] | None]:
        special = self.special.get(key)
        if special is not None:
            if special.get("content_sha256") != content_sha256:
                return None, _blocker(
                    "fixture.recipe_digest_mismatch",
                    "The recipe changed after its special-fixture classification.",
                )
            return None, _blocker(
                str(special.get("code") or "fixture.special_required"),
                str(special["detail"]),
            )
        recipe = self.recipes.get(key)
        if recipe is None:
            return None, _blocker(
                "fixture.missing",
                "No reviewed qualification fixture is bound to this artifact recipe.",
            )
        if recipe.content_sha256 != content_sha256:
            return None, _blocker(
                "fixture.recipe_digest_mismatch",
                "The recipe changed after its qualification fixture was reviewed.",
            )
        if recipe.interface != interface:
            return None, _blocker(
                "fixture.interface_mismatch",
                "The recipe interface changed after its qualification fixture was reviewed.",
            )
        return recipe, None

    def resolve_service(
        self, key: str, content_sha256: str
    ) -> tuple[ServiceRecipe | None, dict[str, str] | None]:
        recipe = self.service_recipes.get(key)
        if recipe is None:
            return None, _blocker(
                "service_fixture.missing",
                "No reviewed digest-bound service smoke is available for this recipe.",
            )
        if recipe.content_sha256 != content_sha256:
            return None, _blocker(
                "service_fixture.recipe_digest_mismatch",
                "The recipe changed after its service smoke was reviewed.",
            )
        return recipe, None
