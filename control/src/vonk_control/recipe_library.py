"""Read the live reviewed recipe library from its public Git repository."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .recipe_contract import (
    RecipeContractError,
    parse_recipe_json,
    recipe_content_sha256,
    validate_recipe,
)

_URI = re.compile(
    r"^vonk://catalog/(?P<publisher>[a-z0-9][a-z0-9-]{1,62})/"
    r"(?P<slug>[a-z0-9][a-z0-9-]{1,62})@sha256:(?P<digest>[0-9a-f]{64})$"
)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_MAX_RESPONSE_BYTES = 512 * 1024
_REPOSITORY = "CarstVaartjes/vonk-forge-recipes"


class RecipeLibraryError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail[:256]
        super().__init__(self.detail)


@dataclass(frozen=True, slots=True)
class RecipeLibraryItem:
    library_commit: str
    source_path: str
    publisher: str
    slug: str
    title: str
    description: str
    tags: tuple[str, ...]
    content_sha256: str
    uri: str
    document: dict[str, object]


@dataclass(frozen=True, slots=True)
class RecipeLibrarySnapshot:
    commit: str
    items: tuple[RecipeLibraryItem, ...]
    repository: str = _REPOSITORY


class RecipeLibraryClient:
    """Fetch exact recipe documents from the current repository main branch."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.github.com",
        repository: str = _REPOSITORY,
        ref: str = "main",
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise RecipeLibraryError(
                "recipe_library.url_insecure",
                "recipe library URL must use HTTPS",
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RecipeLibraryError(
                "recipe_library.url_invalid",
                "recipe library URL contains forbidden components",
            )
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise RecipeLibraryError(
                "recipe_library.repository_invalid",
                "recipe library repository is invalid",
            )
        self._repository = repository
        self._ref = ref
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def close(self) -> None:
        self._client.close()

    def list(self) -> RecipeLibrarySnapshot:
        commit = self._current_revision()
        tree = self._get_json(
            f"/repos/{self._repository}/git/trees/{commit}?recursive=1"
        )
        entries = tree.get("tree")
        if not isinstance(entries, list) or tree.get("truncated") is True:
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library tree is incomplete",
            )
        paths = sorted(
            str(entry["path"])
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("type") == "blob"
            and isinstance(entry.get("path"), str)
            and re.fullmatch(
                r"recipes/[a-z0-9][a-z0-9-]{1,62}\.json", entry["path"]
            )
        )
        items = tuple(self._item(commit, path) for path in paths)
        return RecipeLibrarySnapshot(
            commit=commit,
            items=items,
            repository=self._repository,
        )

    def fetch(self, uri: str) -> RecipeLibraryItem:
        match = _URI.fullmatch(uri)
        if match is None:
            raise RecipeLibraryError(
                "recipe_library.uri_invalid",
                "use vonk://catalog/PUBLISHER/SLUG@sha256:DIGEST",
            )
        publisher, slug = (
            match.group("publisher"),
            match.group("slug"),
        )
        if publisher != "vonk-forge":
            raise RecipeLibraryError(
                "recipe_library.not_found",
                "recipe is not in the default recipe library",
            )
        item = self._item(self._current_revision(), f"recipes/{slug}.json")
        if item.uri != uri or item.publisher != publisher:
            raise RecipeLibraryError(
                "recipe_library.digest_mismatch",
                "recipe library content does not match the requested digest",
            )
        return item

    def _current_revision(self) -> str:
        value = self._get_json(f"/repos/{self._repository}/commits/{self._ref}")
        commit = value.get("sha")
        if not isinstance(commit, str) or not _SHA.fullmatch(commit):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library commit is invalid",
            )
        return commit

    def _item(self, commit: str, source_path: str) -> RecipeLibraryItem:
        encoded = self._get_json(
            f"/repos/{self._repository}/contents/{quote(source_path, safe='/')}"
            f"?ref={quote(commit, safe='')}"
        )
        if encoded.get("encoding") != "base64" or not isinstance(
            encoded.get("content"), str
        ):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library recipe content is invalid",
            )
        try:
            document = dict(
                parse_recipe_json(
                    base64.b64decode(
                        "".join(encoded["content"].split()), validate=True
                    )
                )
            )
        except (binascii.Error, RecipeContractError, UnicodeDecodeError) as error:
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library recipe content is invalid",
            ) from error
        try:
            validate_recipe(document)
        except RecipeContractError as error:
            raise RecipeLibraryError(
                "recipe_library.schema_incompatible",
                f"recipe library recipe is incompatible at {error.path}",
            ) from error
        identity = document.get("identity")
        metadata = document.get("metadata")
        if not isinstance(identity, dict) or not isinstance(metadata, dict):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library recipe metadata is incomplete",
            )
        publisher = identity.get("publisher")
        slug = identity.get("slug")
        title = metadata.get("title")
        description = metadata.get("description")
        tags = metadata.get("tags", [])
        if (
            not isinstance(publisher, str)
            or not _SLUG.fullmatch(publisher)
            or not isinstance(slug, str)
            or not _SLUG.fullmatch(slug)
            or not isinstance(title, str)
            or not title
            or not isinstance(description, str)
            or not description
            or not isinstance(tags, list)
            or any(not isinstance(tag, str) for tag in tags)
            or source_path != f"recipes/{slug}.json"
        ):
            raise RecipeLibraryError(
                "recipe_library.identity_invalid",
                "recipe library recipe identity or metadata is invalid",
            )
        digest = recipe_content_sha256(document)
        return RecipeLibraryItem(
            library_commit=commit,
            source_path=source_path,
            publisher=publisher,
            slug=slug,
            title=title,
            description=description,
            tags=tuple(tags),
            content_sha256=digest,
            uri=f"vonk://catalog/{publisher}/{slug}@sha256:{digest}",
            document=document,
        )

    def _get_json(self, path: str) -> dict[str, Any]:
        try:
            with self._client.stream("GET", path) as response:
                if 300 <= response.status_code < 400:
                    raise RecipeLibraryError(
                        "recipe_library.redirect_forbidden",
                        "recipe library redirects are not followed",
                    )
                if response.status_code == 404:
                    raise RecipeLibraryError(
                        "recipe_library.not_found",
                        "recipe library entry was not found",
                    )
                if response.status_code != 200:
                    raise RecipeLibraryError(
                        "recipe_library.unavailable",
                        "recipe library request failed",
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise RecipeLibraryError(
                            "recipe_library.response_too_large",
                            "recipe library response exceeds 512 KiB",
                        )
        except RecipeLibraryError:
            raise
        except (httpx.HTTPError, OSError) as error:
            raise RecipeLibraryError(
                "recipe_library.unavailable",
                "recipe library is unavailable",
            ) from error
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library response is invalid JSON",
            ) from error
        if not isinstance(value, dict):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library response must be an object",
            )
        return value


__all__ = [
    "RecipeLibraryClient",
    "RecipeLibraryError",
    "RecipeLibraryItem",
    "RecipeLibrarySnapshot",
]
