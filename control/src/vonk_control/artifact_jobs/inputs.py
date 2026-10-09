"""Artifact Jobs: inputs."""

from __future__ import annotations

from collections.abc import AsyncIterable

from vonk_agent_protocol import InvalidRequestReason, UnknownOutcomeError, WaitReason

from .. import artifact_job_states as ajs
from ..artifact_blob_store import ArtifactBlobStoreError, StoredArtifactBlob
from ..bounded_retry import bounded_attempts
from ..categorized_errors import MissingRecord
from ..lifecycle.artifact_job import ArtifactJobAdapter
from ..lifecycle.evidence import Residue
from ..models import ArtifactJob, ArtifactJobFile
from .contracts import (
    MAX_INPUT_FILE_BYTES,
    ArtifactJobInvalid,
    ArtifactJobUnavailableError,
    ArtifactJobView,
    _translate_blob_error,
)
from .storage import ArtifactJobService as StorageService


class ArtifactJobService(StorageService):
    def put_input(
        self,
        job_id: str,
        *,
        name: str,
        media_type: str,
        expected_sha256: str,
        content: bytes,
    ) -> ArtifactJobView:
        unavailable: UnknownOutcomeError | None = None
        for _attempt in bounded_attempts():
            try:
                with self._blob_store.reference_attachment():
                    try:
                        stored = self._blob_store.put_bytes(
                            expected_sha256, content, maximum_bytes=MAX_INPUT_FILE_BYTES
                        )
                    except UnknownOutcomeError:
                        raise
                    except ArtifactBlobStoreError as error:
                        _translate_blob_error(error)
                    return self._attach_input(
                        job_id, name=name, media_type=media_type, stored=stored
                    )
            except UnknownOutcomeError as error:
                unavailable = error
        assert unavailable is not None
        raise unavailable

    async def put_input_stream(
        self,
        job_id: str,
        *,
        name: str,
        media_type: str,
        expected_sha256: str,
        content_length: int,
        chunks: AsyncIterable[bytes],
    ) -> ArtifactJobView:
        expected_bytes = self.input_upload_size(
            job_id, name=name, media_type=media_type, expected_sha256=expected_sha256
        )
        if content_length != expected_bytes:
            raise ArtifactJobInvalid(
                "artifact input Content-Length does not match",
                reason=InvalidRequestReason.CONFLICT,
            )
        with self._blob_store.reference_attachment():
            try:
                stored = await self._blob_store.put_stream(
                    expected_sha256,
                    chunks,
                    expected_bytes=expected_bytes,
                    maximum_bytes=MAX_INPUT_FILE_BYTES,
                )
            except UnknownOutcomeError:
                raise
            except ArtifactBlobStoreError as error:
                _translate_blob_error(error)
            return self._attach_input(
                job_id, name=name, media_type=media_type, stored=stored
            )

    def input_upload_size(
        self, job_id: str, *, name: str, media_type: str, expected_sha256: str
    ) -> int:
        with self._sessions() as session:
            job = session.get(ArtifactJob, job_id)
            if job is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            if ajs.preparation_of(job) != ajs.DRAFT:
                raise ArtifactJobInvalid(
                    "artifact job inputs are immutable",
                    reason=InvalidRequestReason.IMMUTABLE,
                )
            declaration = self._input_declaration(session, job, name)
            if (
                declaration is None
                or declaration.media_type != media_type
                or declaration.sha256 != expected_sha256
            ):
                raise ArtifactJobInvalid(
                    "artifact input does not match its declaration",
                    reason=InvalidRequestReason.CONFLICT,
                )
            return declaration.size_bytes

    def _attach_input(
        self,
        job_id: str,
        *,
        name: str,
        media_type: str,
        stored: StoredArtifactBlob,
    ) -> ArtifactJobView:
        now = self._clock()
        with self._sessions.begin() as session:
            job = session.get(ArtifactJob, job_id, with_for_update=True)
            if job is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            if ajs.preparation_of(job) != ajs.DRAFT:
                raise ArtifactJobInvalid(
                    "artifact job inputs are immutable",
                    reason=InvalidRequestReason.IMMUTABLE,
                )
            declaration = self._input_declaration(session, job, name)
            if (
                declaration is None
                or declaration.media_type != media_type
                or declaration.sha256 != stored.sha256
                or declaration.size_bytes != stored.size_bytes
            ):
                raise ArtifactJobInvalid(
                    "artifact input does not match its declaration",
                    reason=InvalidRequestReason.CONFLICT,
                )
            self._put_blob_in_session(session, stored, now)
            existing = self._file_in_session(session, job_id, "input", name)
            if existing is not None:
                if existing.blob_sha256 != stored.sha256:
                    raise ArtifactJobInvalid(
                        "artifact input changed", reason=InvalidRequestReason.CONFLICT
                    )
                return self._view_in_session(session, job)
            session.add(
                ArtifactJobFile(
                    artifact_job_id=job_id,
                    direction="input",
                    slot=declaration.slot,
                    name=name,
                    media_type=media_type,
                    size_bytes=stored.size_bytes,
                    blob_sha256=stored.sha256,
                    created_at=now,
                )
            )
            job.updated_at = now
            session.flush()
            return self._view_in_session(session, job)

    def finalize(self, job_id: str) -> ArtifactJobView:
        now = self._clock()
        with self._sessions.begin() as session:
            job = session.get(ArtifactJob, job_id, with_for_update=True)
            if job is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            if ajs.preparation_of(job) == ajs.READY:
                return self._view_in_session(session, job)
            if ajs.preparation_of(job) != ajs.DRAFT:
                raise ArtifactJobInvalid(
                    "artifact job cannot be finalized",
                    reason=InvalidRequestReason.NOT_READY,
                )
            manifest = self._stored_input_manifest(session, job)
            uploaded = self._files_in_session(session, job_id, "input")
            observed = self._input_files(uploaded, manifest)
            if isinstance(manifest, Residue) or list(manifest.files) != observed:
                raise ArtifactJobUnavailableError(
                    "artifact input evidence is unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            ArtifactJobAdapter.mark_ready(job, now)
            return self._view_in_session(session, job)
