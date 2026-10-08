"""Source helpers."""

from __future__ import annotations

import ipaddress
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from vonk_agent_protocol import ModelCacheCode

from .constants import _GITHUB_RELEASE_ASSET_HOST, _HF_CANONICAL_HOST
from .errors import ModelCacheConflictInvalid, ModelCacheResolutionInvalid


def _model_selector(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 256:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.SELECTOR_INVALID, "model selector is required"
        )
    return value.strip()


def _request_key(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise ModelCacheConflictInvalid(
            ModelCacheCode.REQUEST_KEY_INVALID, "request key is invalid"
        ) from error


def _is_private_host(value: str) -> bool:
    normalized = value.lower().rstrip(".")
    if normalized in {"localhost", "localhost.localdomain", "ip6-localhost"}:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _is_hf_authority(value: str | None) -> bool:
    if not value:
        return False
    host = value.lower().rstrip(".")
    return host == _HF_CANONICAL_HOST or host.endswith((".huggingface.co", ".hf.co"))


def _is_hf_canonical_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower().rstrip(".") == _HF_CANONICAL_HOST
        and parsed.port is None
    )


def _is_allowed_huggingface_redirect(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and port is None
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and _is_hf_authority(parsed.hostname)
        and not _is_private_host(parsed.hostname)
    )


def _is_allowed_github_release_redirect(value: str) -> bool:
    """Release downloads use one exact anonymous CDN authority only."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == _GITHUB_RELEASE_ASSET_HOST
        and parsed.netloc == _GITHUB_RELEASE_ASSET_HOST
        and port is None
        and parsed.username is None
        and parsed.password is None
        and parsed.path.startswith("/")
        and not parsed.fragment
    )


def _valid_relative_path(value: str) -> bool:
    return bool(
        value
        and len(value) <= 512
        and not value.startswith("/")
        and "\\" not in value
        and "\x00" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _contains_digest(
    value: object, model_digest: str | None, recipe_digest: str | None
) -> bool:
    if model_digest is None and recipe_digest is None:
        return False
    if isinstance(value, Mapping):
        return any(
            _contains_digest(child, model_digest, recipe_digest)
            for child in value.values()
        ) or (
            (
                model_digest is not None
                and value.get("model_content_sha256") == model_digest
            )
            or (
                recipe_digest is not None
                and value.get("recipe_revision_sha256") == recipe_digest
            )
        )
    if isinstance(value, list):
        return any(
            _contains_digest(child, model_digest, recipe_digest) for child in value
        )
    return False


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
