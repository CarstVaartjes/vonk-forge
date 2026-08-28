"""PostgreSQL-authoritative custom recipe and immutable revision authority."""

from __future__ import annotations

import copy
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import Recipe, RecipeRevision
from .recipe_contract import RecipeContractError, recipe_content_sha256, validate_recipe

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,126}$")


class LibraryRecipeError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


class LibraryRecipeConflict(LibraryRecipeError):
    pass


@dataclass(frozen=True, slots=True)
class LibraryRecipeRevision:
    revision_id: str
    recipe_id: str
    slug: str
    title: str
    source: str
    revision_number: int
    content: dict[str, object]
    content_digest: str
    created_by: str
    created_at: datetime


class LibraryRecipeService:
    """Create, edit, and remove recipes without mutating revision history."""

    def __init__(self, sessions: sessionmaker[Session], *, clock) -> None:
        self._sessions = sessions
        self._clock = clock

    def save(
        self,
        *,
        slug: str,
        content: Mapping[str, object],
        actor: Any,
        source: str = "custom",
    ) -> LibraryRecipeRevision:
        self._require_write(actor)
        document = self._validate(slug, content)
        now = _aware(self._clock())
        recipe_id = str(uuid.uuid4())
        revision_id = str(uuid.uuid4())
        digest = recipe_content_sha256(document)
        title = _text(
            document.get("title") or document.get("metadata", {}).get("title"), "title"
        )
        with self._sessions.begin() as session:
            if session.scalar(select(Recipe).where(Recipe.slug == slug)) is not None:
                raise LibraryRecipeConflict(
                    "library.recipe_exists", "recipe slug already exists"
                )
            recipe = Recipe(
                recipe_id=recipe_id,
                slug=slug,
                title=title,
                source=source,
                created_by=self._actor(actor),
                created_at=now,
                updated_at=now,
            )
            revision = RecipeRevision(
                revision_id=revision_id,
                recipe_id=recipe_id,
                revision_number=1,
                content=copy.deepcopy(dict(document)),
                content_digest=digest,
                created_by=self._actor(actor),
                created_at=now,
            )
            session.add_all((recipe, revision))
        return self._view(recipe, revision)

    def edit(
        self, recipe_id: str, content: Mapping[str, object], actor: Any
    ) -> LibraryRecipeRevision:
        self._require_write(actor)
        with self._sessions.begin() as session:
            recipe = session.scalar(
                select(Recipe).where(Recipe.recipe_id == recipe_id).with_for_update()
            )
            if recipe is None:
                raise KeyError(recipe_id)
            document = self._validate(recipe.slug, content)
            digest = recipe_content_sha256(document)
            latest = session.scalar(
                select(RecipeRevision)
                .where(RecipeRevision.recipe_id == recipe_id)
                .order_by(RecipeRevision.revision_number.desc())
                .limit(1)
            )
            assert latest is not None
            if latest.content_digest == digest:
                raise LibraryRecipeConflict(
                    "library.duplicate_revision", "recipe content is unchanged"
                )
            now = _aware(self._clock())
            revision = RecipeRevision(
                revision_id=str(uuid.uuid4()),
                recipe_id=recipe_id,
                revision_number=latest.revision_number + 1,
                content=copy.deepcopy(dict(document)),
                content_digest=digest,
                created_by=self._actor(actor),
                created_at=now,
            )
            recipe.title = _text(
                document.get("title") or document.get("metadata", {}).get("title"),
                "title",
            )
            recipe.updated_at = now
            session.add(revision)
        return self._view(recipe, revision)

    def remove(self, recipe_id: str, actor: Any) -> None:
        """Delete recipe identity and revisions; does not uninstall services."""
        self._require_write(actor)
        with self._sessions.begin() as session:
            recipe = session.scalar(
                select(Recipe).where(Recipe.recipe_id == recipe_id).with_for_update()
            )
            if recipe is None:
                raise KeyError(recipe_id)
            session.delete(recipe)

    def get(
        self, recipe_id: str, revision_number: int | None = None
    ) -> LibraryRecipeRevision:
        with self._sessions() as session:
            recipe = session.get(Recipe, recipe_id)
            if recipe is None:
                raise KeyError(recipe_id)
            query = select(RecipeRevision).where(RecipeRevision.recipe_id == recipe_id)
            if revision_number is None:
                query = query.order_by(RecipeRevision.revision_number.desc())
            else:
                query = query.where(RecipeRevision.revision_number == revision_number)
            revision = session.scalar(query.limit(1))
            if revision is None:
                raise KeyError((recipe_id, revision_number))
            return self._view(recipe, revision)

    def list(self) -> list[LibraryRecipeRevision]:
        with self._sessions() as session:
            rows = session.execute(
                select(Recipe, RecipeRevision)
                .join(RecipeRevision, RecipeRevision.recipe_id == Recipe.recipe_id)
                .order_by(Recipe.slug, RecipeRevision.revision_number)
            ).all()
            return [self._view(recipe, revision) for recipe, revision in rows]

    @staticmethod
    def _validate(slug: str, content: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(slug, str) or _SLUG.fullmatch(slug) is None:
            raise LibraryRecipeError(
                "library.invalid_slug",
                "slug must be lowercase alphanumeric-hyphen text",
            )
        if not isinstance(content, Mapping):
            raise LibraryRecipeError(
                "library.invalid_content", "recipe content must be an object"
            )
        document = copy.deepcopy(dict(content))
        try:
            validate_recipe(document)
        except (RecipeContractError, TypeError, KeyError) as error:
            raise LibraryRecipeError("library.invalid_recipe", str(error)) from error
        return document

    @staticmethod
    def _require_write(actor: Any) -> None:
        capabilities = getattr(actor, "capabilities", None)
        if capabilities is None and isinstance(actor, Mapping):
            capabilities = actor.get("capabilities")
        if capabilities is not None and "library:write" in capabilities:
            return
        if getattr(actor, "role", None) == "administrator":
            return
        raise PermissionError("library:write capability required")

    @staticmethod
    def _actor(actor: Any) -> str:
        return str(getattr(actor, "subject", actor))

    @staticmethod
    def _view(recipe: Recipe, revision: RecipeRevision) -> LibraryRecipeRevision:
        return LibraryRecipeRevision(
            revision.revision_id,
            recipe.recipe_id,
            recipe.slug,
            recipe.title,
            recipe.source,
            revision.revision_number,
            copy.deepcopy(revision.content),
            revision.content_digest,
            revision.created_by,
            revision.created_at,
        )


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LibraryRecipeError(
            "library.invalid_recipe", f"recipe {field} is required"
        )
    return value.strip()


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = [
    "LibraryRecipeConflict",
    "LibraryRecipeError",
    "LibraryRecipeRevision",
    "LibraryRecipeService",
]
