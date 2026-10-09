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
                expected_bytes = self.input_upload_size(
                    job_id,
                    name=name,
                    media_type=media_type,
                    expected_sha256=expected_sha256,
                )
                if len(content) != expected_bytes:
                    raise ArtifactJobInvalid(
                        "artifact input length does not match its declaration",
                        reason=InvalidRequestReason.CONFLICT,
                    )
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
            declaration = self._input_declaration(session, job, name)
            if declaration is None:
                raise ArtifactJobUnavailableError(
                    "artifact input declaration observation is unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            if (
                declaration.media_type != media_type
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
            declaration = self._input_declaration(session, job, name)
            if declaration is None:
                raise ArtifactJobUnavailableError(
                    "artifact input declaration observation is unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            if (
                declaration.media_type != media_type
                or declaration.sha256 != stored.sha256
                or declaration.size_bytes != stored.size_bytes
            ):
                raise ArtifactJobUnavailableError(
                    "artifact input declaration changed during attachment",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            if ajs.preparation_of(job) != ajs.DRAFT:
                return self._view_in_session(session, job)
            self._put_blob_in_session(session, stored, now)
            existing = self._file_in_session(session, job_id, "input", name)
            if existing is not None:
                # The declaration and ingress receipt already bind these bytes.
                # A conflicting local attachment is repaired, not another gate.
                existing.slot = declaration.slot
                existing.media_type = media_type
                existing.size_bytes = stored.size_bytes
                existing.blob_sha256 = stored.sha256
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
        for _attempt in bounded_attempts():
            with self._sessions.begin() as session:
                job = session.get(ArtifactJob, job_id, with_for_update=True)
                if job is None:
                    raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
                # Replays reconnect to the same job, even after it moved on.
                if ajs.preparation_of(job) != ajs.DRAFT:
                    return self._view_in_session(session, job)
                manifest = self._stored_input_manifest(session, job, repair=True)
                uploaded = self._files_in_session(session, job_id, "input")
                observed = self._input_files(uploaded, manifest)
                if (
                    not isinstance(manifest, Residue)
                    and list(manifest.files) == observed
                    and self._available_inputs(session, job)
                ):
                    ArtifactJobAdapter.mark_ready(job, self._clock())
                    return self._view_in_session(session, job)
        with self._sessions.begin() as session:
            job = session.get(ArtifactJob, job_id, with_for_update=True)
            if job is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            return self._end_preparation(session, job)
