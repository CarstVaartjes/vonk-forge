"""Digest-bound fixture registry loading and lookup."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from pathlib import Path

from .contracts import (
    _DIGEST,
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


class _FixtureMembers(Mapping[str, Fixture]):
    """Decode only a requested member; damaged siblings never enter its path."""

    def __init__(self, root: Path, members: object) -> None:
        self.root = root
        self.members = _object(members, "fixture members")

    def __len__(self) -> int:
        return len(self.members)

    def __iter__(self) -> Iterator[str]:
        return iter(self.members)

    def __getitem__(self, fixture_id: str) -> Fixture:
        try:
            item = _object(self.members[fixture_id], f"fixture {fixture_id}")
            name, media_type = item.get("name"), item.get("media_type")
            digest, size = item.get("sha256"), item.get("size_bytes")
            if (
                not _NAME.fullmatch(fixture_id)
                or not isinstance(name, str)
                or not _NAME.fullmatch(name)
                or not isinstance(media_type, str)
                or not _MEDIA_TYPE.fullmatch(media_type)
                or not isinstance(digest, str)
                or not _DIGEST.fullmatch(digest)
            ):
                raise FixtureError("fixture content descriptor is unavailable")
            content = _load_content(self.root, item)
            if size != len(content) or hashlib.sha256(content).hexdigest() != digest:
                raise FixtureError("fixture ingress digest does not match")
            format_name = _fixture_format(media_type)
            if format_name is not None:
                _validate_magic(content, format_name)
            # Provenance describes history; only bytes/media/semantics identify
            # the fixture. Missing or damaged annotations cannot gate reuse.
            source = item.get("provenance")
            provenance = (
                {key: value for key, value in source.items() if isinstance(value, str)}
                if isinstance(source, dict)
                else None
            )
            return Fixture(
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
        except (FixtureError, OSError, ValueError, TypeError) as error:
            raise KeyError(fixture_id) from error


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
        self.fixtures = fixtures
        self._manifest_path: Path | None = None
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
        # Validate the envelope independently; each member has its own decoder.
        _validate_manifest_contract(
            {
                **document,
                "fixtures": {},
                "recipes": {},
                "special_fixtures": {},
                "service_case_templates": {},
                "service_recipes": {},
            }
        )
        fixture_root = path.resolve().parent
        raw_fixtures = _object(document.get("fixtures"), "fixtures")
        fixtures = _FixtureMembers(fixture_root, raw_fixtures)
        raw_recipes = _object(document.get("recipes"), "recipes")
        recipes: dict[str, RecipeFixture] = {}
        for key, raw_recipe in raw_recipes.items():
            try:
                recipes[key] = _parse_recipe_fixture(key, raw_recipe, fixtures)
            except (FixtureError, KeyError, OSError, TypeError, ValueError):
                continue
        raw_special = _object(document.get("special_fixtures", {}), "special_fixtures")
        special = {}
        for key, item in raw_special.items():
            if isinstance(item, Mapping):
                special[key] = item
        raw_service_cases = _object(
            document.get("service_case_templates", {}), "service_case_templates"
        )
        service_cases: dict[str, ServiceCase] = {}
        for key, item in raw_service_cases.items():
            try:
                service_cases[key] = _parse_service_case(key, item)
            except (FixtureError, KeyError, TypeError, ValueError):
                continue
        raw_service_recipes = _object(
            document.get("service_recipes", {}), "service_recipes"
        )
        service_recipes: dict[str, ServiceRecipe] = {}
        for key, item in raw_service_recipes.items():
            try:
                service_recipes[key] = _parse_service_recipe(key, item, service_cases)
            except (FixtureError, KeyError, TypeError, ValueError):
                continue
        registry = cls(
            fixtures,
            recipes,
            special,
            service_cases=service_cases,
            service_recipes=service_recipes,
        )
        registry._manifest_path = path
        return registry

    def _refresh(self) -> None:
        if self._manifest_path is None:
            return
        try:
            repaired = type(self).load(self._manifest_path)
        except (FixtureError, OSError, ValueError, TypeError):
            return  # retain already verified bytes; the next request observes again
        self.fixtures = repaired.fixtures
        self.recipes = repaired.recipes
        self.special = repaired.special
        self.service_cases = repaired.service_cases
        self.service_recipes = repaired.service_recipes

    def resolve(
        self, key: str, content_sha256: str, interface: str
    ) -> tuple[RecipeFixture | None, dict[str, str] | None]:
        self._refresh()
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
        self._refresh()
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
