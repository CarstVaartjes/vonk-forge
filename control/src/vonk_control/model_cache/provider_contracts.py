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
