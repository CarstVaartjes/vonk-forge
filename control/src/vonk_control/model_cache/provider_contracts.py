"""Provider contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr


class _GitHubReleaseAssetMetadata(BaseModel):
    """Required GitHub release asset fields; provider extensions stay allowed."""

    model_config = ConfigDict(extra="allow", strict=True)

    id: StrictInt
    name: StrictStr
    size: StrictInt
    state: StrictStr
    digest: StrictStr | None = None


class _GitHubReleaseMetadata(BaseModel):
    """Required release fields used by cache verification."""

    model_config = ConfigDict(extra="allow", strict=True)

    id: StrictInt
    assets: list[_GitHubReleaseAssetMetadata]


class _GitHubErrorMetadata(BaseModel):
    """Bounded GitHub error fields used only to recognize rate limiting."""

    model_config = ConfigDict(extra="allow", strict=True)

    message: StrictStr


class _GitHubLfsOperation(StrEnum):
    """The Git LFS batch API's own operation word."""

    DOWNLOAD = "download"


class _GitHubLfsObjectRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    oid: StrictStr
    size: StrictInt


class _GitHubLfsBatchRequest(BaseModel):
    """The one batch request the cache sends: download one object by oid."""

    model_config = ConfigDict(extra="forbid", strict=True)

    operation: _GitHubLfsOperation
    transfers: list[StrictStr]
    objects: list[_GitHubLfsObjectRef]
    hash_algo: StrictStr


class _GitHubLfsDownload(BaseModel):
    """A signed download link; it stays in process memory only."""

    model_config = ConfigDict(extra="allow", strict=True)

    href: StrictStr
    header: dict[StrictStr, StrictStr] | None = None
    expires_in: StrictInt | None = None
    expires_at: StrictStr | None = None


class _GitHubLfsActions(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    download: _GitHubLfsDownload | None = None


class _GitHubLfsObjectError(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    code: StrictInt
    message: StrictStr


class _GitHubLfsObject(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    oid: StrictStr
    size: StrictInt
    actions: _GitHubLfsActions | None = None
    error: _GitHubLfsObjectError | None = None


class _GitHubLfsBatch(BaseModel):
    """The Git LFS batch API response fields the cache reads."""

    model_config = ConfigDict(extra="allow", strict=True)

    objects: list[_GitHubLfsObject]
    transfer: StrictStr | None = None
    hash_algo: StrictStr | None = None
