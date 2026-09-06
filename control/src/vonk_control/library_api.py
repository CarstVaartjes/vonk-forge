"""Authenticated read-only Library HTTP routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Path, Query

from .library_contract import LibraryRecipeDetail, LibraryRecipeList, LibrarySnapshot
from .operation_api import bounded_error_responses

LIBRARY_OPERATION_IDS = {
    ("get", "/api/v1/library"): "listLibrary",
    ("get", "/api/v1/library/recipes"): "listLibraryRecipes",
    ("get", "/api/v1/library/recipes/{recipe_id}"): "getLibraryRecipe",
}
_UUID = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def install_library_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    projection: Any | None,
) -> None:
    """Register the bounded Library read surface without mutation authority."""

    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(LIBRARY_OPERATION_IDS)
    authenticated = actor_dependency

    def library() -> Any:
        if projection is None:
            raise HTTPException(
                status_code=503, detail="Library projection unavailable"
            )
        return projection

    @app.get(
        "/api/v1/library",
        response_model=LibrarySnapshot,
        responses=bounded_error_responses(401, 422, 503),
        operation_id="listLibrary",
    )
    def list_library(
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        _actor: Any = authenticated,
    ) -> LibrarySnapshot:
        try:
            return library().list(limit=limit, cursor=cursor)
        except HTTPException:
            raise
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)[:256]) from None
        except (OSError, RuntimeError, TypeError):
            raise HTTPException(
                status_code=503, detail="Library projection unavailable"
            ) from None

    @app.get(
        "/api/v1/library/recipes",
        response_model=LibraryRecipeList,
        responses=bounded_error_responses(401, 422, 503),
        operation_id="listLibraryRecipes",
    )
    def list_library_recipes(
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        _actor: Any = authenticated,
    ) -> LibraryRecipeList:
        try:
            return library().recipes(limit=limit, cursor=cursor)
        except HTTPException:
            raise
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)[:256]) from None
        except (OSError, RuntimeError, TypeError):
            raise HTTPException(
                status_code=503, detail="Library projection unavailable"
            ) from None

    @app.get(
        "/api/v1/library/recipes/{recipe_id}",
        response_model=LibraryRecipeDetail,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getLibraryRecipe",
    )
    def get_library_recipe(
        recipe_id: Annotated[str, Path(pattern=_UUID)],
        _actor: Any = authenticated,
    ) -> LibraryRecipeDetail:
        try:
            return library().detail(recipe_id)
        except HTTPException:
            raise
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Library recipe not found"
            ) from None
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503, detail="Library projection unavailable"
            ) from None
