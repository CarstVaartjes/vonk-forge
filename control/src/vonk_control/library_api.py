"""Authenticated Model and Recipe operator read routes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Path, Query

from .library_contract import (
    ModelDetailResponse,
    ModelLibraryResponse,
    RecipeDetailResponse,
    RecipeLibraryResponse,
)
from .library_projection import LibrarySelectorAmbiguous
from .operation_api import bounded_error_responses

# Friendly names may contain spaces.  Keep selectors bounded and reject control
# characters; path routing remains safe because the projection resolves only an
# exact publisher/slug or slug match.
_SELECTOR_PATTERN = r"^[^\x00-\x1f\x7f]{1,256}$"

LIBRARY_OPERATION_IDS = {
    ("get", "/api/model"): "getModelStatus",
    ("get", "/api/model/library"): "listModelLibrary",
    ("get", "/api/model/{selector}"): "getModel",
    ("get", "/api/recipe"): "getRecipeStatus",
    ("get", "/api/recipe/library"): "listRecipeLibrary",
    ("get", "/api/recipe/{selector}"): "getRecipe",
}


def _error(error: Exception) -> HTTPException:
    if isinstance(error, LibrarySelectorAmbiguous):
        candidates = ", ".join(error.candidates[:16])
        return HTTPException(
            status_code=422,
            detail=f"selector is ambiguous: {error.selector}; candidates: {candidates}",
        )
    if isinstance(error, KeyError):
        return HTTPException(status_code=404, detail="operator object not found")
    if isinstance(error, ValueError):
        return HTTPException(status_code=422, detail=str(error)[:256])
    return HTTPException(status_code=503, detail="library projection unavailable")


def install_library_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    projection: Any | None,
) -> None:
    """Install the singular read hierarchy; no legacy library aliases remain."""

    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(LIBRARY_OPERATION_IDS)
    authenticated = actor_dependency

    def library() -> Any:
        if projection is None:
            raise HTTPException(status_code=503, detail="library projection unavailable")
        return projection

    @app.get(
        "/api/model",
        response_model=ModelLibraryResponse,
        responses=bounded_error_responses(401, 503),
        operation_id="getModelStatus",
    )
    def model_status(_actor: Any = authenticated) -> ModelLibraryResponse:
        try:
            return library().models(local_only=True)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            raise _error(error) from None

    @app.get(
        "/api/model/library",
        response_model=ModelLibraryResponse,
        responses=bounded_error_responses(401, 422, 503),
        operation_id="listModelLibrary",
    )
    def model_library(
        limit: Annotated[int, Query(ge=1, le=512)] = 100,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        usage: Annotated[list[str] | None, Query(max_length=64)] = None,
        family: Annotated[list[str] | None, Query(max_length=64)] = None,
        version: Annotated[list[str] | None, Query(max_length=64)] = None,
        quantization: Annotated[list[str] | None, Query(max_length=64)] = None,
        search: Annotated[str | None, Query(max_length=256)] = None,
        updated_since: Annotated[datetime | None, Query()] = None,
        sort: Annotated[Literal["updated", "name"], Query()] = "updated",
        _actor: Any = authenticated,
    ) -> ModelLibraryResponse:
        try:
            return library().models(
                limit=limit, cursor=cursor, usage=usage or [], family=family or [],
                version=version or [], quantization=quantization or [], search=search,
                updated_since=updated_since, sort=sort,
            )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            raise _error(error) from None

    @app.get(
        "/api/model/{selector:path}",
        response_model=ModelDetailResponse,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getModel",
    )
    def model_detail(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        _actor: Any = authenticated,
    ) -> ModelDetailResponse:
        try:
            return library().model_detail(selector)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            raise _error(error) from None

    @app.get(
        "/api/recipe",
        response_model=RecipeLibraryResponse,
        responses=bounded_error_responses(401, 503),
        operation_id="getRecipeStatus",
    )
    def recipe_status(_actor: Any = authenticated) -> RecipeLibraryResponse:
        try:
            return library().recipe_library()
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            raise _error(error) from None

    @app.get(
        "/api/recipe/library",
        response_model=RecipeLibraryResponse,
        responses=bounded_error_responses(401, 422, 503),
        operation_id="listRecipeLibrary",
    )
    def recipe_library(
        limit: Annotated[int, Query(ge=1, le=512)] = 100,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        model: Annotated[str | None, Query(max_length=256)] = None,
        all_models: Annotated[bool, Query()] = False,
        usage: Annotated[list[str] | None, Query(max_length=64)] = None,
        search: Annotated[str | None, Query(max_length=256)] = None,
        updated_since: Annotated[datetime | None, Query()] = None,
        sort: Annotated[Literal["updated", "name"], Query()] = "updated",
        _actor: Any = authenticated,
    ) -> RecipeLibraryResponse:
        try:
            return library().recipe_library(
                limit=limit, cursor=cursor, model_selector=model,
                all_models=all_models, usage=usage or [], search=search,
                updated_since=updated_since, sort=sort,
            )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            raise _error(error) from None

    @app.get(
        "/api/recipe/{selector:path}",
        response_model=RecipeDetailResponse,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getRecipe",
    )
    def recipe_detail(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        _actor: Any = authenticated,
    ) -> RecipeDetailResponse:
        try:
            return library().recipe_detail(selector)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            raise _error(error) from None


__all__ = ["LIBRARY_OPERATION_IDS", "install_library_routes"]
