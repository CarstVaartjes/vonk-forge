"""Provider contracts."""

from __future__ import annotations

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
