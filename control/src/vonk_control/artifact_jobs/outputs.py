"""Artifact Jobs: outputs."""

from __future__ import annotations

from collections.abc import AsyncIterable, Mapping
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AgentOperation as WireAgentOperation,
)
from vonk_agent_protocol import (
    AgentProtocolError,
    AgentResultState,
    InvalidRequestReason,
    RecipeJobFile,
    RecipeJobOutputLimits,
    RecipeJobRunResult,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
)

from .. import agent_operation_states
from .. import artifact_job_states as ajs
from ..artifact_blob_store import ArtifactBlobStoreError, StoredArtifactBlob
from ..artifact_job_evidence import ArtifactJobResultEvidence
from ..bounded_retry import bounded_attempts
from ..categorized_errors import MissingRecord
from ..lifecycle import Effect, Outcome, Reported
from ..lifecycle.agent_operation import AgentOperationAdapter
from ..lifecycle.artifact_job import ArtifactJobAdapter
from ..lifecycle.evidence import BookkeepingReason, Residue, retire_as_unknown
from ..models import AgentOperation, ArtifactJob, ArtifactJobBlob, ArtifactJobFile, Job
from .contracts import (
    ArtifactJobInvalid,
    ArtifactJobUnavailableError,
    ArtifactResultInvalid,
    ArtifactResultRefused,
    _translate_blob_error,
    _validate_outputs_against_contract,
)
from .inputs import ArtifactJobService as InputService


class ArtifactJobService(InputService):
    def input_blob(
        self, job_id: str, sha256: str, *, node_id: str
    ) -> tuple[Path, str, int]:
        with self._sessions() as session:
            job = self._authorized_agent_job(session, job_id, node_id)
            row = session.scalar(
                select(ArtifactJobFile).where(
                    ArtifactJobFile.artifact_job_id == job.id,
                    ArtifactJobFile.direction == "input",
                    ArtifactJobFile.blob_sha256 == sha256,
                )
            )
            if row is None:
                raise MissingRecord(sha256, reason=InvalidRequestReason.NOT_FOUND)
            blob = session.get(ArtifactJobBlob, sha256)
            if blob is None:
                # The file row has no blob row: the bytes are unknown, not a
                # refusal. The reader is told "not found" and re-uploads.
                retire_as_unknown(
                    "artifact-job.blob",
                    sha256,
                    BookkeepingReason.ROW_INCOMPLETE,
                    "stored input has no blob row",
                )
                raise MissingRecord(sha256, reason=InvalidRequestReason.NOT_FOUND)
            try:
                path = self._blob_store.resolve(
                    blob.storage_key, sha256, blob.size_bytes
                )
            except UnknownOutcomeError:
                raise
            except ArtifactBlobStoreError as error:
                _translate_blob_error(error)
            if path is None:
                # Bytes the store no longer holds are unknown, not a refusal: the
                # reader is told "not found" and the content is re-uploaded.
                retire_as_unknown(
                    "artifact-job.blob", sha256, note="stored bytes are absent"
                )
                raise MissingRecord(sha256, reason=InvalidRequestReason.NOT_FOUND)
            return path, row.media_type, row.size_bytes

    def put_output(
        self,
        job_id: str,
        *,
        node_id: str,
        name: str,
        media_type: str,
        expected_sha256: str,
        content: bytes,
    ) -> None:
        parsed = RecipeJobFile.parse(
            {
                "name": name,
                "media_type": media_type,
                "size_bytes": len(content),
                "sha256": expected_sha256,
            },
            maximum_bytes=1024**3,
        )
        self._validate_output_upload(job_id, node_id=node_id, parsed=parsed)
        unavailable: UnknownOutcomeError | None = None
        for _attempt in bounded_attempts():
            try:
                with self._blob_store.reference_attachment():
                    try:
                        stored = self._blob_store.put_bytes(
                            expected_sha256, content, maximum_bytes=1024**3
                        )
                    except UnknownOutcomeError:
                        raise
                    except ArtifactBlobStoreError as error:
                        _translate_blob_error(error)
                    self._attach_output(
                        job_id, node_id=node_id, parsed=parsed, stored=stored
                    )
                return
            except UnknownOutcomeError as error:
                unavailable = error
        assert unavailable is not None
        raise unavailable

    async def put_output_stream(
        self,
        job_id: str,
        *,
        node_id: str,
        name: str,
        media_type: str,
        expected_sha256: str,
        content_length: int,
        chunks: AsyncIterable[bytes],
    ) -> None:
        parsed = RecipeJobFile.parse(
            {
                "name": name,
                "media_type": media_type,
                "size_bytes": content_length,
                "sha256": expected_sha256,
            },
            maximum_bytes=1024**3,
        )
        self._validate_output_upload(job_id, node_id=node_id, parsed=parsed)
        with self._blob_store.reference_attachment():
            try:
                stored = await self._blob_store.put_stream(
                    expected_sha256,
                    chunks,
                    expected_bytes=content_length,
                    maximum_bytes=1024**3,
                )
            except UnknownOutcomeError:
                raise
            except ArtifactBlobStoreError as error:
                _translate_blob_error(error)
            self._attach_output(job_id, node_id=node_id, parsed=parsed, stored=stored)

    def _validate_output_upload(
        self, job_id: str, *, node_id: str, parsed: RecipeJobFile
    ) -> None:
        with self._sessions() as session:
            job = self._authorized_agent_job(session, job_id, node_id)
            limits = RecipeJobOutputLimits.parse(job.output_limits)
            existing = self._files_in_session(session, job_id, "output")
            projected = tuple(
                self._output_file(item) for item in existing if item.name != parsed.name
            ) + (parsed,)
            contract = self._stored_contract(session, job)
            if isinstance(contract, Residue):
                raise ArtifactJobUnavailableError(
                    "artifact contract evidence is unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            _validate_outputs_against_contract(contract, projected, terminal=False)
            if parsed.media_type not in limits.allowed_media_types:
                raise ArtifactJobInvalid(
                    "artifact output media type is not allowed",
                    reason=InvalidRequestReason.UNSUPPORTED,
                )
            if (
                len(existing)
                + (0 if any(item.name == parsed.name for item in existing) else 1)
                > limits.max_files
            ):
                raise ArtifactJobInvalid(
                    "artifact output file count exceeds the limit",
                    reason=InvalidRequestReason.LIMIT_EXCEEDED,
                )
            if (
                parsed.size_bytes > limits.max_file_bytes
                or sum(item.size_bytes for item in existing if item.name != parsed.name)
                + parsed.size_bytes
                > limits.max_total_bytes
            ):
                raise ArtifactJobInvalid(
                    "artifact output bytes exceed the limit",
                    reason=InvalidRequestReason.LIMIT_EXCEEDED,
                )

    def _attach_output(
        self,
        job_id: str,
        *,
        node_id: str,
        parsed: RecipeJobFile,
        stored: StoredArtifactBlob,
    ) -> None:
        # The blob store has already verified the declared digest and length at
        # ingress. Its typed receipt is the authority for the attached content.
        now = self._clock()
        with self._sessions.begin() as session:
            job = self._authorized_agent_job(session, job_id, node_id, lock=True)
            limits = RecipeJobOutputLimits.parse(job.output_limits)
            if parsed.media_type not in limits.allowed_media_types:
                raise ArtifactJobInvalid(
                    "artifact output media type is not allowed",
                    reason=InvalidRequestReason.UNSUPPORTED,
                )
            existing = self._files_in_session(session, job_id, "output")
            same_name = next(
                (item for item in existing if item.name == parsed.name), None
            )
            if same_name is not None:
                if same_name.blob_sha256 != parsed.sha256:
                    raise ArtifactJobInvalid(
                        "artifact output changed", reason=InvalidRequestReason.CONFLICT
                    )
                return
            projected = tuple(self._output_file(item) for item in existing) + (parsed,)
            contract = self._stored_contract(session, job)
            if isinstance(contract, Residue):
                raise ArtifactJobUnavailableError(
                    "artifact contract evidence is unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            _validate_outputs_against_contract(contract, projected, terminal=False)
            if len(existing) + 1 > limits.max_files:
                raise ArtifactJobInvalid(
                    "artifact output file count exceeds the limit",
                    reason=InvalidRequestReason.LIMIT_EXCEEDED,
                )
            if (
                parsed.size_bytes > limits.max_file_bytes
                or sum(item.size_bytes for item in existing) + parsed.size_bytes
                > limits.max_total_bytes
            ):
                raise ArtifactJobInvalid(
                    "artifact output bytes exceed the limit",
                    reason=InvalidRequestReason.LIMIT_EXCEEDED,
                )
            self._put_blob_in_session(session, stored, now)
            session.add(
                ArtifactJobFile(
                    artifact_job_id=job_id,
                    direction="output",
                    slot=None,
                    name=parsed.name,
                    media_type=parsed.media_type,
                    size_bytes=stored.size_bytes,
                    blob_sha256=stored.sha256,
                    created_at=now,
                )
            )
            job.updated_at = now

    def result_blob(
        self, job_id: str, name: str, sha256: str
    ) -> tuple[Path, str, str, int]:
        with self._sessions() as session:
            job = session.get(ArtifactJob, job_id)
            if job is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            if ajs.state_of(job) != ajs.SUCCEEDED:
                raise ArtifactJobInvalid(
                    "artifact job result is not available",
                    reason=InvalidRequestReason.NOT_READY,
                )
            row = session.scalar(
                select(ArtifactJobFile).where(
                    ArtifactJobFile.artifact_job_id == job_id,
                    ArtifactJobFile.direction == "output",
                    ArtifactJobFile.name == name,
                    ArtifactJobFile.blob_sha256 == sha256,
                )
            )
            blob = session.get(ArtifactJobBlob, sha256) if row is not None else None
            if row is None or blob is None:
                raise MissingRecord(sha256, reason=InvalidRequestReason.NOT_FOUND)
            try:
                path = self._blob_store.resolve(
                    blob.storage_key, sha256, blob.size_bytes
                )
            except UnknownOutcomeError:
                raise
            except ArtifactBlobStoreError as error:
                _translate_blob_error(error)
            if path is None:
                # Bytes the store no longer holds are unknown, not a refusal: the
                # reader is told "not found" and the content is re-uploaded.
                retire_as_unknown(
                    "artifact-job.blob", sha256, note="stored bytes are absent"
                )
                raise MissingRecord(sha256, reason=InvalidRequestReason.NOT_FOUND)
            return path, row.media_type, row.name, row.size_bytes

    @staticmethod
    def _end_unverified_result(
        session: Session,
        adapter: ArtifactJobAdapter,
        operation: AgentOperation,
        parent: Job,
        job: ArtifactJob,
        result: RecipeJobRunResult,
        now: datetime,
    ) -> None:
        # The authenticated terminal report ends execution. Missing local output
        # evidence cannot invalidate it or authorize publishing unverified files.
        # Never replay the irreversible user job to repair its bookkeeping.
        AgentOperationAdapter(session).record_outcome(
            operation,
            None,
            parent,
            Outcome.FAILED,
            now,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
        adapter.settle(
            job,
            Reported(
                Outcome.FAILED,
                retryable=False,
                effect=Effect.ESTABLISHED,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ),
            now,
            evidence=ArtifactJobResultEvidence(
                elapsed_milliseconds=result.elapsed_milliseconds,
                peak_memory_bytes=result.peak_memory_bytes,
            ),
        )

    def consume_agent_result(
        self,
        session: Session,
        operation: AgentOperation,
        _attempt: object,
        message: object,
    ) -> None:
        parent = session.get(Job, operation.parent_job_id)
        if parent is None or parent.kind != WireAgentOperation.RECIPE_JOB_RUN.value:
            return
        artifact_job = session.scalar(
            select(ArtifactJob)
            .where(ArtifactJob.operation_id == parent.id)
            .with_for_update(of=ArtifactJob)
        )
        if artifact_job is None:
            # A result for an order whose artifact job row is gone has nothing to
            # apply: recorded as unknown, and the order's own lifecycle ends it.
            retire_as_unknown(
                "artifact-job.result",
                operation.parent_job_id,
                BookkeepingReason.ROW_INCOMPLETE,
                "no artifact job owns this order",
            )
            return
        state = getattr(message, "state", None)
        raw_result = getattr(message, "result", None)
        now = self._clock()
        adapter = ArtifactJobAdapter(session, clock=self._clock)
        if state == agent_operation_states.WIRE_UNKNOWN:
            try:
                waiting_result = RecipeJobRunResult.parse(raw_result)
                if (
                    waiting_result.job_id != artifact_job.id
                    or waiting_result.run_id != artifact_job.run_id
                    or waiting_result.exit_code != 130
                    or waiting_result.outputs
                ):
                    raise ArtifactResultInvalid(
                        "waiting artifact result identity or output is invalid",
                        reason=InvalidRequestReason.MALFORMED,
                    )
            except (AgentProtocolError, TypeError, ValueError) as error:
                self._reject_result(
                    adapter, operation, parent, artifact_job, error, now
                )
                return
            # The agent could not confirm that the job stopped.  The core has
            # already decided the order (an uncertain report under a cancel is
            # stopped and observed, and ends ``cancelled`` with the effect unknown
            # after its stop budget); the job mirrors that decision and keeps the
            # report as evidence.  It never waits for an operator on its own.
            adapter.project(
                artifact_job,
                now,
                reason=(
                    waiting_result.reason
                    if waiting_result.reason
                    else "artifact cancellation could not safely stop the active scope"
                ),
                evidence=ArtifactJobResultEvidence(
                    failure_kind="cancellation-stop-uncertain",
                    recoverable=True,
                    active_scope_may_remain=True,
                    elapsed_milliseconds=waiting_result.elapsed_milliseconds,
                    peak_memory_bytes=waiting_result.peak_memory_bytes,
                ),
            )
            return
        try:
            result = RecipeJobRunResult.parse(raw_result)
            if result.job_id != artifact_job.id or result.run_id != artifact_job.run_id:
                raise ArtifactResultRefused(
                    "artifact result identity does not match",
                    reason=SecurityRefusalReason.AGENT_IDENTITY_MISMATCH,
                )
            succeeded = state == AgentResultState.SUCCEEDED and result.exit_code == 0
            failed = state == AgentResultState.FAILED and result.exit_code != 0
            cancelled = bool(
                state == AgentResultState.CANCELLED
                and result.exit_code == 130
                and not result.outputs
                and isinstance(parent.result, Mapping)
                and parent.result.get("cancel_requested") is True
            )
            if not (succeeded or failed or cancelled):
                raise ArtifactResultInvalid(
                    "artifact result state and exit code disagree",
                    reason=InvalidRequestReason.MALFORMED,
                )
            uploaded = self._files_in_session(session, artifact_job.id, "output")
            observed = tuple(self._output_file(item) for item in uploaded)
            limits = RecipeJobOutputLimits.parse(artifact_job.output_limits)
            if tuple(result.outputs) != observed:
                self._end_unverified_result(
                    session, adapter, operation, parent, artifact_job, result, now
                )
                return
            if any(
                item.media_type not in limits.allowed_media_types
                for item in result.outputs
            ):
                raise ArtifactResultInvalid(
                    "artifact result media type is not allowed",
                    reason=InvalidRequestReason.MALFORMED,
                )
            if (
                len(result.outputs) > limits.max_files
                or sum(item.size_bytes for item in result.outputs)
                > limits.max_total_bytes
            ):
                raise ArtifactResultInvalid(
                    "artifact result exceeds output limits",
                    reason=InvalidRequestReason.MALFORMED,
                )
            if succeeded:
                result_contract = self._stored_contract(session, artifact_job)
                if isinstance(result_contract, Residue):
                    self._end_unverified_result(
                        session, adapter, operation, parent, artifact_job, result, now
                    )
                    return
                _validate_outputs_against_contract(
                    result_contract, result.outputs, terminal=True
                )
        except (AgentProtocolError, TypeError, ValueError) as error:
            self._reject_result(adapter, operation, parent, artifact_job, error, now)
            return
        adapter.settle(
            artifact_job,
            Reported(
                Outcome.DONE
                if succeeded
                else Outcome.CANCELLED
                if cancelled
                else Outcome.FAILED,
                retryable=False,
            ),
            now,
            reason=(
                None
                if succeeded
                else result.reason
                or ("artifact job cancelled" if cancelled else "recipe job failed")
            ),
            evidence=ArtifactJobResultEvidence(
                elapsed_milliseconds=result.elapsed_milliseconds,
                peak_memory_bytes=result.peak_memory_bytes,
            ),
            output_manifest_sha256=result.output_manifest_sha256,
        )
