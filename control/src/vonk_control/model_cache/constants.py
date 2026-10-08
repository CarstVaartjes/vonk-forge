"""Constants."""

from __future__ import annotations

import hashlib
import logging

from vonk_agent_protocol import ModelCacheCode, SecurityRefusalReason

SCHEMA_VERSION = 2


SOURCE_POLICY = "nas-first"


_DIGEST_LENGTH = 64


_DIGEST_PATTERN = r"[0-9a-f]{64}"


_MAX_ARTIFACTS = 1024


_MAX_ARTIFACT_PARTS = 1024


_MAX_MANIFEST_BYTES = 1_048_576


_CHUNK_BYTES = 1024 * 1024


_PARALLEL_RANGE_MIN_BYTES = 64 * 1024 * 1024


_PARALLEL_RANGE_WORKERS = 4


_MAX_HTTP_REDIRECTS = 3


_DEFAULT_MAX_PARALLEL_DOWNLOADS = 16


_MAX_PARALLEL_DOWNLOADS = 32


_DEFAULT_MAX_DOWNLOAD_STREAMS = 16


_RETRY_BASE_SECONDS = 5


_SOURCE_GONE_STATUSES = frozenset({404, 410})


_SOURCE_GONE_ATTEMPTS = 5


_REVERIFY_ACTOR = "system:model-cache-reverify"


_MAX_RETRY_HINT_SECONDS = 365 * 24 * 60 * 60


_TRANSFER_CLAIM_SECONDS = 120


_UPSTREAM_CHECK_SECONDS = 8.0


_UPSTREAM_CHECK_WORKERS = 4


_HF_CANONICAL_HOST = "huggingface.co"


_GITHUB_API_HOST = "api.github.com"


_GITHUB_USER_AGENT = "vonk-forge/0.1.1"


_GITHUB_RELEASE_ASSET_HOST = "release-assets.githubusercontent.com"


_MAX_GITHUB_RELEASE_METADATA_BYTES = 4 * 1024 * 1024


_MAX_GITHUB_ERROR_METADATA_BYTES = 64 * 1024


_USE_MANIFEST_BYTES = object()


_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


_LOGGER = logging.getLogger(__name__)


_WEIGHT_ROLES = frozenset({"model", "weight", "weights"})


_CREDENTIAL_FAILURE_CODES = frozenset(
    {
        ModelCacheCode.CREDENTIALS_MISSING,
        SecurityRefusalReason.MODEL_CACHE_CREDENTIALS_DENIED.value,
        SecurityRefusalReason.MODEL_CACHE_CREDENTIALS_INVALID.value,
    }
)


_CREDENTIAL_FAILURE_PUBLIC_CODES = frozenset(
    {"access_required", "access_denied", "credentials_invalid"}
)


_TERMINAL_FAILURE_CODES = _CREDENTIAL_FAILURE_CODES | frozenset(
    {
        SecurityRefusalReason.MODEL_CACHE_SOURCE_ACCESS_DENIED.value,
        ModelCacheCode.SOURCE_GONE,
        ModelCacheCode.SOURCE_INVALID,
        ModelCacheCode.SOURCE_UNSUPPORTED,
        ModelCacheCode.SOURCE_UNTRUSTED,
        ModelCacheCode.REDIRECT_FORBIDDEN,
        ModelCacheCode.RELEASE_ASSET_IDENTITY_CONFLICT,
    }
)
