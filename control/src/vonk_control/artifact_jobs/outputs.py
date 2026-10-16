"""Artifact Jobs: outputs."""

from __future__ import annotations

from collections.abc import AsyncIterable, Mapping
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AgentFailureKind,
    AgentFailureResult,
    AgentProtocolError,
    AgentResultState,
    Disposition,
    FailureCode,
    InvalidRequestReason,
    RecipeJobFile,
    RecipeJobOutputLimits,
    RecipeJobRunResult,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_agent_protocol import (
    AgentOperation as WireAgentOperation,
)

from .. import agent_operation_states
from .. import artifact_job_states as ajs
from ..artifact_blob_store import ArtifactBlobStoreError, StoredArtifactBlob
from ..artifact_job_evidence import ArtifactJobResultEvidence
from ..bounded_retry import bounded_attempts
from ..categorized_errors import MissingRecord
from ..compiled_artifact_contract import CompiledArtifactContract
from ..lifecycle import Effect, Outcome, Reported
from ..lifecycle.agent_operation import AgentOperationAdapter
from ..lifecycle.artifact_job import ArtifactJobAdapter
from ..lifecycle.evidence import BookkeepingReason, Residue, retire_as_unknown
from ..models import AgentOperation, ArtifactJob, ArtifactJobFile, Job
from .contracts import (
    ArtifactJobInvalid,
    ArtifactJobUnavailableError,
    ArtifactOutputBreachError,
    ArtifactResultRefused,
    _translate_blob_error,
    _validate_outputs_against_contract,
    _validate_outputs_against_limits,
)
from .inputs import ArtifactJobService as InputService


class ArtifactJobService(InputService):
    def input_blob(
        self, job_id: str, sha256: str, *, node_id: str
    ) -> tuple[Path, str, int]:
        for _attempt in bounded_attempts():
            with self._sessions.begin() as session:
                job = self._authorized_agent_job(session, job_id, node_id)
                manifest = self._stored_input_manifest(session, job)
                declaration = (
                    None
                    if isinstance(manifest, Residue)
                    else next(
                        (item for item in manifest.files if item.sha256 == sha256),
                        None,
                    )
                )
                if declaration is not None:
                    path = self._blob_store.resolve(
                        f"{sha256[:2]}/{sha256}",
                        sha256,
                        declaration.size_bytes,
                    )
                    if path is not None:
                        self._put_blob_in_session(
                            session,
                            StoredArtifactBlob(
                                sha256=sha256,
                                size_bytes=declaration.size_bytes,
                                storage_key=f"{sha256[:2]}/{sha256}",
                                path=path,
                            ),
                            self._clock(),
                        )
                        return path, declaration.media_type, declaration.size_bytes
        with self._sessions.begin() as session:
            job = session.get(ArtifactJob, job_id, with_for_update=True)
            if job is not None:
                adapter = ArtifactJobAdapter(session, clock=self._clock)
                parent, operation, _attempt = adapter.order_of(session, job)
                # An order that already ended keeps its outcome.
                if (
                    parent is not None
                    and operation is not None
                    and operation.state in agent_operation_states.LIVE
                ):
                    self._observe_result(adapter, operation, parent, job, self._clock())
        raise ArtifactJobUnavailableError(
            "artifact input observation is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )

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
        unavailable: UnknownOutcomeError | None = None
        for _attempt in bounded_attempts():
            try:
                self._validate_output_upload(job_id, node_id=node_id, parsed=parsed)
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
            limits = self._stored_output_limits(session, job, repair=True)
            if isinstance(limits, Residue):
                raise ArtifactJobUnavailableError(
                    "artifact output limit observation is unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            existing = self._files_in_session(session, job_id, "output")
            if any(
                item.name == parsed.name and item.blob_sha256 != parsed.sha256
                for item in existing
            ):
                raise ArtifactJobInvalid(
                    "artifact output changed", reason=InvalidRequestReason.CONFLICT
                )
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
            _validate_outputs_against_limits(limits, projected)

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
            limits = self._stored_output_limits(session, job, repair=True)
            if isinstance(limits, Residue):
                raise ArtifactJobUnavailableError(
                    "artifact output limit observation is unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            existing = self._files_in_session(session, job_id, "output")
            same_name = next(
                (item for item in existing if item.name == parsed.name), None
            )
            if same_name is not None:
                if same_name.blob_sha256 != parsed.sha256:
                    raise ArtifactJobUnavailableError(
                        "artifact output changed",
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    )
                return
            projected = tuple(self._output_file(item) for item in existing) + (parsed,)
            contract = self._stored_contract(session, job)
            if isinstance(contract, Residue):
                raise ArtifactJobUnavailableError(
                    "artifact contract evidence is unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            try:
                _validate_outputs_against_contract(contract, projected, terminal=False)
                _validate_outputs_against_limits(limits, projected)
            except ArtifactJobInvalid as error:
                # Validated before the bytes were stored: the next PUT
                # validates again and refuses a breach with its rule.
                raise ArtifactJobUnavailableError(
                    "artifact output projection changed during attachment",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error
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
        unavailable: ArtifactJobUnavailableError | None = None
        for _attempt in bounded_attempts():
            try:
                return self._result_blob_once(job_id, name, sha256)
            except ArtifactJobUnavailableError as error:
                unavailable = error
        assert unavailable is not None
        raise unavailable

    def _result_blob_once(
        self, job_id: str, name: str, sha256: str
    ) -> tuple[Path, str, str, int]:
        with self._sessions() as session:
            job = session.get(ArtifactJob, job_id)
            if job is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            if ajs.state_of(job) != ajs.SUCCEEDED:
                raise ArtifactJobUnavailableError(
                    "artifact result observation is unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            row = self._file_in_session(session, job.id, "output", name)
            file = None
            if row is not None and row.blob_sha256 == sha256:
                try:
                    file = self._output_file(row)
                except (TypeError, ValueError):
                    pass
            receipt = self._stored_result(session, job)
            if receipt is not None:
                file = next(
                    (
                        item
                        for item in receipt.outputs
                        if item.name == name and item.sha256 == sha256
                    ),
                    None,
                )
            if file is None:
                raise MissingRecord(sha256, reason=InvalidRequestReason.NOT_FOUND)
            path = self._blob_store.resolve(
                f"{sha256[:2]}/{sha256}",
                sha256,
                file.size_bytes,
            )
            if path is None:
                raise ArtifactJobUnavailableError(
                    "artifact result bytes observation is unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            return path, file.media_type, file.name, file.size_bytes

    @staticmethod
    def _end_result_failed(
        session: Session,
        adapter: ArtifactJobAdapter,
        operation: AgentOperation,
        parent: Job,
        job: ArtifactJob,
        result: RecipeJobRunResult,
        now: datetime,
        breach: ArtifactOutputBreachError | None = None,
    ) -> None:
        # The authenticated terminal report ends execution. Missing local output
        # evidence cannot invalidate it or authorize publishing unverified files,
        # and a manifest that breaks the output contract breaks it on every look.
        # Never replay the irreversible user job to repair its bookkeeping.
        reason = (
            WaitReason.OBSERVATION_UNAVAILABLE if breach is None else str(breach)[:512]
        )
        AgentOperationAdapter(session).record_outcome(
            operation,
            None,
            parent,
            Outcome.FAILED,
            now,
            reason=reason,
            replaces_report=True,
        )
        adapter.settle(
            job,
            Reported.failed(
                Disposition.FAIL,
                # A receipted exit proves the job's process ceased.
                effect=Effect.ESTABLISHED if breach is None else Effect.STOPPED,
                reason=reason,
            ),
            now,
            evidence=ArtifactJobResultEvidence(
                elapsed_milliseconds=result.elapsed_milliseconds,
                peak_memory_bytes=result.peak_memory_bytes,
                exit_code=None if breach is None else result.exit_code,
                output_breach=None if breach is None else breach.breach,
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
        if (
            state == AgentResultState.FAILED
            and isinstance(raw_result, AgentFailureResult)
            and raw_result.failure_kind
            in {AgentFailureKind.INVALID_CONTRACT, AgentFailureKind.INVALID_AUTHORITY}
        ):
            # A definite failure without a process receipt: refused before the
            # job's container started (an input or the Start grant), or the
            # exited job's outputs broke their contract on the agent. The job
            # ends failed with that cause, nothing to observe.
            adapter.settle(
                artifact_job,
                Reported.failed(Disposition.FAIL, effect=Effect.NONE),
                now,
                reason=": ".join(
                    part
                    for part in (raw_result.reason, raw_result.diagnostic)
                    if part is not None
                )[:512],
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
            limits = self._stored_output_limits(session, artifact_job)
            contract = (
                self._stored_contract(session, artifact_job)
                if succeeded or result.outputs
                else None
            )
            # The receipt's manifest is final: the outputs the exited job left.
            # One that breaks the contract does so in every state the agent
            # reports it (an upload the Controller refused leaves it unknown).
            breach = _output_breach(
                limits, contract, result.outputs, terminal=succeeded
            )
            if breach is not None:
                self._end_result_failed(
                    session,
                    adapter,
                    operation,
                    parent,
                    artifact_job,
                    result,
                    now,
                    breach,
                )
                return
            if state == agent_operation_states.WIRE_UNKNOWN:
                if result.exit_code != 130 or result.outputs:
                    self._observe_result(adapter, operation, parent, artifact_job, now)
                    return
                adapter.project(
                    artifact_job,
                    now,
                    reason=(
                        result.reason or "artifact cancellation stop is unconfirmed"
                    ),
                    evidence=ArtifactJobResultEvidence(
                        recoverable=True,
                        active_scope_may_remain=True,
                        elapsed_milliseconds=result.elapsed_milliseconds,
                        peak_memory_bytes=result.peak_memory_bytes,
                    ),
                )
                return
            failed = state == AgentResultState.FAILED and result.exit_code != 0
            cancelled = bool(
                state == AgentResultState.CANCELLED
                and result.exit_code == 130
                and not result.outputs
                and isinstance(parent.result, Mapping)
                and parent.result.get("cancel_requested") is True
            )
            if not (succeeded or failed or cancelled):
                self._observe_result(adapter, operation, parent, artifact_job, now)
                return
            uploaded = self._files_in_session(session, artifact_job.id, "output")
            observed = tuple(self._output_file(item) for item in uploaded)
            if isinstance(limits, Residue):
                self._observe_result(adapter, operation, parent, artifact_job, now)
                return
            if tuple(result.outputs) != observed:
                self._end_result_failed(
                    session, adapter, operation, parent, artifact_job, result, now
                )
                return
            if succeeded and isinstance(contract, Residue):
                self._end_result_failed(
                    session, adapter, operation, parent, artifact_job, result, now
                )
                return
        except ArtifactResultRefused as error:
            self._reject_result(adapter, operation, parent, artifact_job, error, now)
            return
        except (AgentProtocolError, TypeError, ValueError):
            self._observe_result(adapter, operation, parent, artifact_job, now)
            return
        adapter.settle(
            artifact_job,
            Reported(Outcome.DONE)
            if succeeded
            else Reported(Outcome.CANCELLED)
            if cancelled
            # A measured process exit proves the job's process ceased: a known
            # end, never a lapsed lease or a scope left in doubt.
            else Reported.failed(Disposition.FAIL, effect=Effect.STOPPED),
            now,
            reason=(
                None
                if succeeded
                else result.reason
                or (
                    FailureCode.OPERATION_CANCELLED
                    if cancelled
                    else FailureCode.RECIPE_JOB_RUN_FAILED
                )
            ),
            evidence=ArtifactJobResultEvidence(
                elapsed_milliseconds=result.elapsed_milliseconds,
                peak_memory_bytes=result.peak_memory_bytes,
                exit_code=result.exit_code,
            ),
            output_manifest_sha256=result.output_manifest_sha256,
        )


def _output_breach(
    limits: RecipeJobOutputLimits | Residue,
    contract: CompiledArtifactContract | Residue | None,
    outputs: tuple[RecipeJobFile, ...],
    *,
    terminal: bool,
) -> ArtifactOutputBreachError | None:
    """The rule a reported manifest breaks, checked against what is readable;
    unreadable limits or contract evidence decide nothing here."""

    try:
        if isinstance(contract, CompiledArtifactContract):
            _validate_outputs_against_contract(contract, outputs, terminal=terminal)
        if not isinstance(limits, Residue):
            _validate_outputs_against_limits(limits, outputs)
    except ArtifactOutputBreachError as error:
        return error
    return None
