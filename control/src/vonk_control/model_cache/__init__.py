"""Content-addressed model artifacts on the Controller NAS."""

from __future__ import annotations

import os as os
import shutil as shutil

from ..artifact_reference_scan import (
    model_set_reference_reasons as model_set_reference_reasons,
)
from .artifacts import ArtifactPart as ArtifactPart
from .artifacts import ArtifactSetManifest as ArtifactSetManifest
from .artifacts import ArtifactSpec as ArtifactSpec
from .artifacts import _composed_artifacts as _composed_artifacts
from .artifacts import _has_python_tuple as _has_python_tuple
from .artifacts import _is_digest as _is_digest
from .artifacts import _is_hex as _is_hex
from .artifacts import _optional_digest as _optional_digest
from .artifacts import _part_from_input as _part_from_input
from .artifacts import _sha256_json as _sha256_json
from .artifacts import _split_transient_bytes as _split_transient_bytes
from .artifacts import _unique_artifacts as _unique_artifacts
from .artifacts import _validate_artifact as _validate_artifact
from .artifacts import _validate_manifest as _validate_manifest
from .artifacts import _validate_parts as _validate_parts
from .catalog_helpers import _canonical_model_artifacts as _canonical_model_artifacts
from .catalog_helpers import _datetime as _datetime
from .catalog_helpers import (
    _github_release_asset_binding as _github_release_asset_binding,
)
from .catalog_helpers import _github_repository_parts as _github_repository_parts
from .catalog_helpers import _iso as _iso
from .catalog_helpers import _iso_now as _iso_now
from .catalog_helpers import _model_lineage_signature as _model_lineage_signature
from .catalog_helpers import _parse_iso as _parse_iso
from .catalog_helpers import _recipe_definition as _recipe_definition
from .catalog_helpers import (
    _recipe_model_content_digests as _recipe_model_content_digests,
)
from .catalog_helpers import _recipe_model_file_ids as _recipe_model_file_ids
from .catalog_helpers import _revision_identity as _revision_identity
from .catalog_helpers import (
    _same_model_artifact_identity as _same_model_artifact_identity,
)
from .catalog_helpers import (
    _source_for_catalog_artifact as _source_for_catalog_artifact,
)
from .catalog_helpers import _valid_repository as _valid_repository
from .catalog_helpers import _validate_source as _validate_source
from .constants import _CHUNK_BYTES as _CHUNK_BYTES
from .constants import _CREDENTIAL_FAILURE_CODES as _CREDENTIAL_FAILURE_CODES
from .constants import (
    _CREDENTIAL_FAILURE_PUBLIC_CODES as _CREDENTIAL_FAILURE_PUBLIC_CODES,
)
from .constants import _DEFAULT_MAX_DOWNLOAD_STREAMS as _DEFAULT_MAX_DOWNLOAD_STREAMS
from .constants import (
    _DEFAULT_MAX_PARALLEL_DOWNLOADS as _DEFAULT_MAX_PARALLEL_DOWNLOADS,
)
from .constants import _DIGEST_LENGTH as _DIGEST_LENGTH
from .constants import _DIGEST_PATTERN as _DIGEST_PATTERN
from .constants import _EMPTY_SHA256 as _EMPTY_SHA256
from .constants import _GITHUB_API_HOST as _GITHUB_API_HOST
from .constants import _GITHUB_RELEASE_ASSET_HOST as _GITHUB_RELEASE_ASSET_HOST
from .constants import _GITHUB_USER_AGENT as _GITHUB_USER_AGENT
from .constants import _HF_CANONICAL_HOST as _HF_CANONICAL_HOST
from .constants import _LOGGER as _LOGGER
from .constants import _MAX_ARTIFACT_PARTS as _MAX_ARTIFACT_PARTS
from .constants import _MAX_ARTIFACTS as _MAX_ARTIFACTS
from .constants import (
    _MAX_GITHUB_ERROR_METADATA_BYTES as _MAX_GITHUB_ERROR_METADATA_BYTES,
)
from .constants import (
    _MAX_GITHUB_RELEASE_METADATA_BYTES as _MAX_GITHUB_RELEASE_METADATA_BYTES,
)
from .constants import _MAX_HTTP_REDIRECTS as _MAX_HTTP_REDIRECTS
from .constants import _MAX_MANIFEST_BYTES as _MAX_MANIFEST_BYTES
from .constants import _MAX_PARALLEL_DOWNLOADS as _MAX_PARALLEL_DOWNLOADS
from .constants import _MAX_RETRY_HINT_SECONDS as _MAX_RETRY_HINT_SECONDS
from .constants import _PARALLEL_RANGE_MIN_BYTES as _PARALLEL_RANGE_MIN_BYTES
from .constants import _PARALLEL_RANGE_WORKERS as _PARALLEL_RANGE_WORKERS
from .constants import _RETRY_BASE_SECONDS as _RETRY_BASE_SECONDS
from .constants import _REVERIFY_ACTOR as _REVERIFY_ACTOR
from .constants import _SOURCE_GONE_ATTEMPTS as _SOURCE_GONE_ATTEMPTS
from .constants import _SOURCE_GONE_STATUSES as _SOURCE_GONE_STATUSES
from .constants import _TERMINAL_FAILURE_CODES as _TERMINAL_FAILURE_CODES
from .constants import _TRANSFER_CLAIM_SECONDS as _TRANSFER_CLAIM_SECONDS
from .constants import _UPSTREAM_CHECK_SECONDS as _UPSTREAM_CHECK_SECONDS
from .constants import _UPSTREAM_CHECK_WORKERS as _UPSTREAM_CHECK_WORKERS
from .constants import _USE_MANIFEST_BYTES as _USE_MANIFEST_BYTES
from .constants import _WEIGHT_ROLES as _WEIGHT_ROLES
from .constants import SCHEMA_VERSION as SCHEMA_VERSION
from .constants import SOURCE_POLICY as SOURCE_POLICY
from .errors import ModelCacheConflict as ModelCacheConflict
from .errors import ModelCacheConflictInvalid as ModelCacheConflictInvalid
from .errors import ModelCacheConflictRefused as ModelCacheConflictRefused
from .errors import ModelCacheConflictUnknown as ModelCacheConflictUnknown
from .errors import ModelCacheCredentialPathUnsafe as ModelCacheCredentialPathUnsafe
from .errors import ModelCacheDeletionFenceLost as ModelCacheDeletionFenceLost
from .errors import ModelCacheError as ModelCacheError
from .errors import ModelCacheNotFound as ModelCacheNotFound
from .errors import ModelCacheNotFoundInvalid as ModelCacheNotFoundInvalid
from .errors import ModelCacheNotFoundRefused as ModelCacheNotFoundRefused
from .errors import ModelCacheRemovalOwnerInvalid as ModelCacheRemovalOwnerInvalid
from .errors import ModelCacheResolutionError as ModelCacheResolutionError
from .errors import ModelCacheResolutionInvalid as ModelCacheResolutionInvalid
from .errors import ModelCacheResolutionRefused as ModelCacheResolutionRefused
from .errors import ModelCacheStorageError as ModelCacheStorageError
from .errors import ModelCacheStorageInvalid as ModelCacheStorageInvalid
from .errors import ModelCacheStorageRefused as ModelCacheStorageRefused
from .errors import ModelCacheStorageUnknown as ModelCacheStorageUnknown
from .errors import _ArtifactWriterBusy as _ArtifactWriterBusy
from .errors import _CacheInvalid as _CacheInvalid
from .errors import _CacheRefusal as _CacheRefusal
from .errors import _CacheUnknown as _CacheUnknown
from .errors import _huggingface_access_url as _huggingface_access_url
from .errors import _refusal_reason as _refusal_reason
from .errors import _retry_after_seconds as _retry_after_seconds
from .errors import _retryable_failure as _retryable_failure
from .errors import model_cache_failure_is_terminal as model_cache_failure_is_terminal
from .persistence import _cache_failure as _cache_failure
from .persistence import _derived_result as _derived_result
from .persistence import _fresh_progress as _fresh_progress
from .persistence import _manifest_of as _manifest_of
from .persistence import _model_removal_intent_digest as _model_removal_intent_digest
from .persistence import _operation_cancellation as _operation_cancellation
from .persistence import _operation_payload as _operation_payload
from .persistence import _operation_progress as _operation_progress
from .persistence import _operation_removal as _operation_removal
from .persistence import _parse_operation_envelope as _parse_operation_envelope
from .persistence import _read_manifest_document as _read_manifest_document
from .persistence import _read_operation_payload as _read_operation_payload
from .persistence import _read_operation_progress as _read_operation_progress
from .persistence import _rebuild_operation_payload as _rebuild_operation_payload
from .persistence import _removal_checkpoint as _removal_checkpoint
from .persistence import _store_operation_payload as _store_operation_payload
from .persistence import _updated as _updated
from .persistence import _wait_blockers as _wait_blockers
from .persistence import _write_operation_payload as _write_operation_payload
from .provider_contracts import _GitHubErrorMetadata as _GitHubErrorMetadata
from .provider_contracts import (
    _GitHubReleaseAssetMetadata as _GitHubReleaseAssetMetadata,
)
from .provider_contracts import _GitHubReleaseMetadata as _GitHubReleaseMetadata
from .service import ModelCacheService as ModelCacheService
from .source_helpers import _contains_digest as _contains_digest
from .source_helpers import _fsync_directory as _fsync_directory
from .source_helpers import (
    _is_allowed_github_release_redirect as _is_allowed_github_release_redirect,
)
from .source_helpers import (
    _is_allowed_huggingface_redirect as _is_allowed_huggingface_redirect,
)
from .source_helpers import _is_hf_authority as _is_hf_authority
from .source_helpers import _is_hf_canonical_url as _is_hf_canonical_url
from .source_helpers import _is_private_host as _is_private_host
from .source_helpers import _model_selector as _model_selector
from .source_helpers import _request_key as _request_key
from .source_helpers import _valid_relative_path as _valid_relative_path
from .views import CacheOperationView as CacheOperationView
from .views import ModelCacheRemovalScope as ModelCacheRemovalScope
from .views import StorageSummary as StorageSummary
from .views import _BackgroundTransfer as _BackgroundTransfer
