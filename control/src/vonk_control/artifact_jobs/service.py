"""Artifact Jobs: service."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InvalidRequestReason,
    RecipeJobInputFile,
    RecipeJobOutputLimits,
    RecipeJobRunRequest,
    RunState,
    WaitReason,
    recipe_job_manifest_sha256,
)
from vonk_agent_protocol.job_inputs import RecipeJobInputManifest

from .. import artifact_job_states as ajs
from ..artifact_job_evidence import ArtifactJobResultEvidence, read_result_evidence
from ..categorized_errors import InvalidValue, MissingRecord
from ..cluster_mappings import mapping_option_choices
from ..compiled_artifact_contract import ParameterScalar
from ..execution_plan_service import compile_job_invocation
from ..lifecycle import CancelRequested
from ..lifecycle.artifact_job import ArtifactJobAdapter
from ..lifecycle.evidence import Damaged, Residue
from ..models import (
    ArtifactJob,
    ArtifactJobBlob,
    ArtifactJobFile,
    ClusterMapping,
    Job,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
)
from ..recipe_operations import RecipeOperationConflict
from .contracts import (
    MAX_INPUT_FILE_BYTES,
    MAX_INPUT_FILES,
    MAX_INPUT_TOTAL_BYTES,
    ArtifactJobCapabilitiesResponse,
    ArtifactJobError,
    ArtifactJobInvalid,
    ArtifactJobStorageCapabilities,
    ArtifactJobTransportCapabilities,
    ArtifactJobUnavailableError,
    ArtifactJobView,
    StorageReconciliation,
    _active_recipe_revision,
    _artifact_submission_in_session,
    _compile_contract,
    _effective_output_limits,
    _effective_parameters,
    _output_mappings,
    _recipe_interface,
    _validate_inputs_against_contract,
)
from .outputs import ArtifactJobService as OutputService


class ArtifactJobService(OutputService):
    def reconcile_storage(self, *, batch_limit: int = 1000) -> StorageReconciliation:
        if not 1 <= batch_limit <= 10_000:
            raise InvalidValue(
                "artifact reconciliation batch limit is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        with self._blob_store.reference_reconciliation():
            return self._reconcile_storage_fenced(batch_limit=batch_limit)

    def _reconcile_storage_fenced(self, *, batch_limit: int) -> StorageReconciliation:
        cutoff = self._clock() - timedelta(seconds=self._retention_seconds)
        with self._sessions.begin() as session:
            expired = tuple(
                session.scalars(
                    select(ArtifactJob)
                    .where(
                        ArtifactJob.state.in_(ajs.ENDED),
                        ArtifactJob.completed_at.is_not(None),
                        ArtifactJob.completed_at < cutoff,
                    )
                    .limit(batch_limit)
                )
            )
            # Bytes may only be reclaimed because the exact job that referenced
            # them expired. An empty global reference scan proves nothing after a
            # restore that lost reference rows, so it never authorizes deletion.
            expired_blobs = (
                set(
                    session.scalars(
                        select(ArtifactJobFile.blob_sha256).where(
                            ArtifactJobFile.artifact_job_id.in_(
                                [job.id for job in expired]
                            )
                        )
                    )
                )
                if expired
                else set()
            )
            expired_ids = [job.id for job in expired]
            referenced = set(
                session.scalars(
                    select(ArtifactJobFile.blob_sha256).where(
                        ArtifactJobFile.artifact_job_id.not_in(expired_ids)
                    )
                )
            )
            reclaimable = expired_blobs - referenced
        # Retain the exact expired references until filesystem work completes.
        # They are the durable deletion authorization on contention, I/O loss,
        # or restart. No SQL transaction spans filesystem locks or deletion.
        result = self._blob_store.reconcile(
            referenced,
            batch_limit=batch_limit,
            reclaimable_sha256=reclaimable,
            _reference_fenced=True,
        )
        removed_blob_records = 0
        expired_jobs = 0
        if not result.remaining_work:
            with self._sessions.begin() as session:
                session.execute(
                    delete(ArtifactJobFile).where(
                        ArtifactJobFile.artifact_job_id.in_(expired_ids)
                    )
                )
                for job_id in expired_ids:
                    job = session.get(ArtifactJob, job_id)
                    if job is not None:
                        session.delete(job)
                        expired_jobs += 1
                orphan_rows = tuple(
                    session.scalars(
                        select(ArtifactJobBlob).where(
                            ArtifactJobBlob.sha256.in_(reclaimable)
                        )
                    )
                )
                for blob in orphan_rows:
                    session.delete(blob)
                removed_blob_records = len(orphan_rows)
        return StorageReconciliation(
            **result.model_dump(exclude={"remaining_work"}),
            expired_jobs=expired_jobs,
            removed_blob_records=removed_blob_records,
            remaining_work=bool(
                len(expired) == batch_limit
                or removed_blob_records == batch_limit
                or result.remaining_work
            ),
        )

    def create(
        self,
        run_id: str,
        *,
        interface: str,
        parameters: Mapping[str, ParameterScalar],
        inputs: Sequence[RecipeJobInputFile],
        output_limits: RecipeJobOutputLimits,
        timeout_seconds: int,
        actor: str,
        request_id: str,
    ) -> ArtifactJobView:
        parsed_inputs = tuple(
            sorted(inputs, key=lambda item: item.name.encode("utf-8"))
        )
        if len(parsed_inputs) > MAX_INPUT_FILES:
            raise ArtifactJobInvalid(
                "artifact job has too many input files",
                reason=InvalidRequestReason.LIMIT_EXCEEDED,
            )
        if len({item.name for item in parsed_inputs}) != len(parsed_inputs):
            raise ArtifactJobInvalid(
                "artifact input names must be unique",
                reason=InvalidRequestReason.DUPLICATE,
            )
        total = sum(item.size_bytes for item in parsed_inputs)
        if total > MAX_INPUT_TOTAL_BYTES:
            raise ArtifactJobInvalid(
                "artifact job input bytes exceed the limit",
                reason=InvalidRequestReason.LIMIT_EXCEEDED,
            )
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
            raise ArtifactJobInvalid(
                "artifact job timeout is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        if not 1 <= timeout_seconds <= 3_600:
            raise ArtifactJobInvalid(
                "artifact job timeout is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        supplied_parameters = dict(parameters)
        manifest = RecipeJobInputManifest(
            schema_version=1, total_bytes=total, files=list(parsed_inputs)
        )
        manifest_digest = recipe_job_manifest_sha256(parsed_inputs)
        now = self._clock()
        try:
            with self._sessions.begin() as session:
                return self._create_in_session(
                    session,
                    run_id=run_id,
                    interface=interface,
                    supplied_parameters=supplied_parameters,
                    parsed_inputs=parsed_inputs,
                    output_limits=output_limits,
                    timeout_seconds=timeout_seconds,
                    actor=actor,
                    request_id=request_id,
                    manifest=manifest,
                    manifest_digest=manifest_digest,
                    total=total,
                    now=now,
                )
        except IntegrityError:
            # A concurrent controller process may win the globally unique
            # request key after our initial lookup. Re-open a transaction and
            # apply the same semantic replay comparison to the committed row.
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(ArtifactJob).where(ArtifactJob.request_id == request_id)
                )
                if existing is None:
                    raise ArtifactJobInvalid(
                        "artifact job request key collision",
                        reason=InvalidRequestReason.CONFLICT,
                    ) from None
                return self._create_in_session(
                    session,
                    run_id=run_id,
                    interface=interface,
                    supplied_parameters=supplied_parameters,
                    parsed_inputs=parsed_inputs,
                    output_limits=output_limits,
                    timeout_seconds=timeout_seconds,
                    actor=actor,
                    request_id=request_id,
                    manifest=manifest,
                    manifest_digest=manifest_digest,
                    total=total,
                    now=now,
                    existing=existing,
                )

    def _create_in_session(
        self,
        session: Session,
        *,
        run_id: str,
        interface: str,
        supplied_parameters: Mapping[str, ParameterScalar],
        parsed_inputs: tuple[RecipeJobInputFile, ...],
        output_limits: RecipeJobOutputLimits,
        timeout_seconds: int,
        actor: str,
        request_id: str,
        manifest: RecipeJobInputManifest,
        manifest_digest: str,
        total: int,
        now: datetime,
        existing: ArtifactJob | None = None,
    ) -> ArtifactJobView:
        existing = existing or session.scalar(
            select(ArtifactJob)
            .where(ArtifactJob.request_id == request_id)
            .with_for_update()
        )
        if existing is not None and (
            existing.run_id != run_id
            or existing.interface != interface
            or existing.input_manifest != manifest.model_dump(mode="json")
            or existing.input_manifest_sha256 != manifest_digest
            or existing.input_total_bytes != total
            or existing.timeout_seconds != timeout_seconds
            or existing.actor != actor
        ):
            raise ArtifactJobInvalid(
                "request key was already used differently",
                reason=InvalidRequestReason.CONFLICT,
            )
        run = session.get(RecipeRun, run_id)
        if run is None or existing is None and run.state != RunState.RUNNING:
            raise ArtifactJobInvalid(
                "recipe run is not accepting jobs",
                reason=InvalidRequestReason.NOT_READY,
            )
        installation = session.get(RecipeInstallation, run.installation_id)
        resolved = (
            _active_recipe_revision(session, installation.recipe_revision_id)
            if installation is not None
            else None
        )
        if resolved is None:
            raise ArtifactJobUnavailableError(
                "recipe revision evidence is unavailable",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        _revision, recipe = resolved
        if _recipe_interface(recipe) != interface or interface == "openai":
            if existing is not None:
                raise ArtifactJobInvalid(
                    "request key was already used differently",
                    reason=InvalidRequestReason.CONFLICT,
                )
            raise ArtifactJobInvalid(
                "artifact job interface does not match the run",
                reason=InvalidRequestReason.CONFLICT,
            )
        try:
            contract = _compile_contract(recipe, interface)
            if isinstance(contract, Damaged):
                raise ArtifactJobUnavailableError(
                    contract.note, reason=WaitReason.OBSERVATION_UNAVAILABLE
                )
            contract_digest = contract.sha256()
            parameters_copy = _effective_parameters(
                contract.parameters, supplied_parameters
            )
            limits = _effective_output_limits(contract, output_limits)
            if timeout_seconds > contract.max_timeout_seconds:
                raise ArtifactJobInvalid(
                    "artifact job timeout exceeds the recipe contract",
                    reason=InvalidRequestReason.LIMIT_EXCEEDED,
                )
            _validate_inputs_against_contract(contract, parsed_inputs)
        except ArtifactJobUnavailableError:
            raise
        except ArtifactJobError:
            if existing is not None:
                raise ArtifactJobInvalid(
                    "request key was already used differently",
                    reason=InvalidRequestReason.CONFLICT,
                ) from None
            raise
        effective_limits = limits.to_mapping()
        contract_mapping = contract.to_mapping()
        if existing is not None:
            existing_contract = self._stored_contract(session, existing)
            if isinstance(existing_contract, Residue):
                raise ArtifactJobUnavailableError(
                    "artifact contract evidence is unavailable",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            if (
                existing.parameters != parameters_copy
                or existing.output_limits != effective_limits
                or existing_contract.sha256() != contract_digest
            ):
                raise ArtifactJobInvalid(
                    "request key was already used differently",
                    reason=InvalidRequestReason.CONFLICT,
                )
            return self._view_in_session(session, existing)
        artifact_job = ArtifactJobAdapter.new_job(
            id=str(uuid.uuid4()),
            run_id=run_id,
            request_id=request_id,
            interface=interface,
            parameters=parameters_copy,
            output_limits=effective_limits,
            compiled_contract=contract_mapping,
            contract_sha256=contract_digest,
            input_manifest=manifest.model_dump(mode="json"),
            input_manifest_sha256=manifest_digest,
            input_total_bytes=total,
            timeout_seconds=timeout_seconds,
            actor=actor,
            created_at=now,
            updated_at=now,
        )
        session.add(artifact_job)
        session.flush()
        return self._view_in_session(session, artifact_job)

    def capabilities(self) -> ArtifactJobCapabilitiesResponse:
        return ArtifactJobCapabilitiesResponse(
            transport=ArtifactJobTransportCapabilities(
                max_input_files=MAX_INPUT_FILES,
                max_input_file_bytes=MAX_INPUT_FILE_BYTES,
                max_input_total_bytes=MAX_INPUT_TOTAL_BYTES,
                max_output_files=32,
                max_output_file_bytes=1024**3,
                max_output_total_bytes=2 * 1024**3,
                max_timeout_seconds=3_600,
                reserved_input_names=["manifest.json"],
            ),
            storage=ArtifactJobStorageCapabilities.model_validate(
                self._blob_store.usage().model_dump()
            ),
        )

    def submit(self, job_id: str, *, actor: str, request_id: str) -> ArtifactJobView:
        now = self._clock()
        with self._sessions.begin() as session:
            artifact_job = session.get(ArtifactJob, job_id, with_for_update=True)
            if artifact_job is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            if artifact_job.operation_id is not None:
                submission = _artifact_submission_in_session(session, artifact_job)
                # A submission whose identity cannot be read is recorded; the job
                # is already submitted, so its view is the answer.
                if isinstance(submission, Job) and submission.request_id != request_id:
                    raise ArtifactJobInvalid(
                        "artifact job was submitted under another request identity",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                return self._view_in_session(session, artifact_job)
            if ajs.preparation_of(artifact_job) != ajs.READY:
                raise ArtifactJobInvalid(
                    "artifact job is not ready", reason=InvalidRequestReason.NOT_READY
                )
            run = session.get(RecipeRun, artifact_job.run_id, with_for_update=True)
            if run is None or run.state != RunState.RUNNING:
                raise ArtifactJobInvalid(
                    "recipe run is not accepting jobs",
                    reason=InvalidRequestReason.NOT_READY,
                )
            # Queue ownership and physical execution slots belong to agent_jobs.
            # A prior request cannot become a second admission mutex here.
            installation = session.get(RecipeInstallation, run.installation_id)
            resolved = (
                _active_recipe_revision(session, installation.recipe_revision_id)
                if installation is not None
                else None
            )
            if installation is None or resolved is None:
                # The run's installation or recipe revision no longer exists:
                # a genuine absence, refused request-led with its own code.
                raise ArtifactJobInvalid(
                    "recipe job workload identity is unavailable",
                    reason=InvalidRequestReason.NOT_FOUND,
                )
            revision, _recipe = resolved
            node = self._job_node_in_session(session, run)
            launch = self._job_launch(session, artifact_job, run, installation, node)
            if launch is None:
                # The run's or the job's stored evidence is damaged and nothing
                # re-derives it (each case is recorded as residue): the run is
                # not accepting this job now, and the submit is asked again.
                raise ArtifactJobInvalid(
                    "recipe run is not accepting jobs",
                    reason=InvalidRequestReason.NOT_READY,
                )
            contract = launch.contract
            floor = launch.memory_floor_bytes
            parameters = launch.parameters
            installed_plan = launch.installed_plan
            mapping = (
                session.get(ClusterMapping, run.mapping_id)
                if run.mapping_id is not None
                else None
            )
            invocation = compile_job_invocation(
                session,
                revision=revision,
                installed=installed_plan,
                build=(
                    session.get(RecipeBuild, installation.recipe_build_id)
                    if installation.recipe_build_id is not None
                    else None
                ),
                parameters=parameters,
                timeout_seconds=artifact_job.timeout_seconds,
                memory_floor_bytes=floor,
                reserved_memory_bytes=node.reserved_memory_bytes,
                option_choices=mapping_option_choices(
                    mapping.parameters if mapping is not None else {}
                ),
            )
            raw_files = [file.model_dump(mode="json") for file in launch.input_files]
            payload = {
                "job_id": artifact_job.id,
                "run_id": run.id,
                "installation_id": installation.id,
                "recipe_revision_id": revision.id,
                "plan_digest": run.plan_digest,
                "mapping_id": run.mapping_id,
                "input_manifest_sha256": artifact_job.input_manifest_sha256,
                "input_total_bytes": artifact_job.input_total_bytes,
                "inputs": raw_files,
                "compiled_execution_plan": invocation.model_dump(mode="json"),
                "run_generation": run.run_generation,
                "output_mappings": [
                    item.model_dump(mode="json") for item in _output_mappings(contract)
                ],
                "output_limits": artifact_job.output_limits,
            }
            RecipeJobRunRequest.parse(payload)
            operation = self._recipe_operations.enqueue_one_shot_job_in_session(
                session,
                artifact_job_id=artifact_job.id,
                run_id=run.id,
                node_id=node.node_id,
                payload=payload,
                actor=actor,
                request_id=request_id,
                authority_digest=revision.content_digest,
                now=now,
            )
            ArtifactJobAdapter.mark_submitted(artifact_job, operation.id, now)
        self._recipe_operations.notify_agents()
        return self.get(job_id)

    def get(self, job_id: str) -> ArtifactJobView:
        with self._sessions() as session:
            job = session.get(ArtifactJob, job_id)
            if job is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            return self._view_in_session(session, job)

    def get_by_request_id(self, request_id: str) -> ArtifactJobView:
        """Resolve the original draft after a create response was lost."""
        with self._sessions() as session:
            job = session.scalar(
                select(ArtifactJob).where(ArtifactJob.request_id == request_id)
            )
            if job is None:
                raise MissingRecord(request_id, reason=InvalidRequestReason.NOT_FOUND)
            return self._view_in_session(session, job)

    def list_for_run(
        self, run_id: str, *, limit: int = 100
    ) -> tuple[ArtifactJobView, ...]:
        if not 1 <= limit <= 100:
            raise ArtifactJobInvalid(
                "artifact job list limit is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        with self._sessions() as session:
            if session.get(RecipeRun, run_id) is None:
                raise MissingRecord(run_id, reason=InvalidRequestReason.NOT_FOUND)
            jobs = tuple(
                session.scalars(
                    select(ArtifactJob)
                    .where(ArtifactJob.run_id == run_id)
                    .order_by(ArtifactJob.created_at.desc(), ArtifactJob.id.desc())
                    .limit(limit)
                )
            )
            return tuple(self._view_in_session(session, job) for job in jobs)

    def cancel(
        self, job_id: str, *, actor: str, request_id: str, reason: str
    ) -> ArtifactJobView:
        cancellation_reason = " ".join(reason.split())[:512]
        with self._sessions() as session:
            job = session.get(ArtifactJob, job_id)
            if job is None:
                raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
            operation_id = job.operation_id
            state = ajs.state_of(job)
            evidence = read_result_evidence(job.result_evidence)
        if state in {ajs.SUCCEEDED, ajs.FAILED}:
            raise ArtifactJobInvalid(
                "artifact job is not cancellable", reason=InvalidRequestReason.CONFLICT
            )
        if state == ajs.CANCELLED and operation_id is None:
            if (
                evidence is not None
                and evidence.cancel_request_id == request_id
                and evidence.cancel_actor == actor
                and evidence.cancel_reason == cancellation_reason
            ):
                return self.get(job_id)
            raise ArtifactJobInvalid(
                "cancellation request key was already used differently",
                reason=InvalidRequestReason.CONFLICT,
            )
        if operation_id is not None:
            try:
                self._recipe_operations.cancel(
                    operation_id, actor=actor, request_id=request_id, reason=reason
                )
            except RecipeOperationConflict as error:
                raise ArtifactJobInvalid(
                    str(error), reason=InvalidRequestReason.CONFLICT
                ) from error
        now = self._clock()
        with self._sessions.begin() as session:
            job = session.get(ArtifactJob, job_id, with_for_update=True)
            assert job is not None
            if not ajs.is_ended(job):
                adapter = ArtifactJobAdapter(session, clock=self._clock)
                evidence = ArtifactJobResultEvidence(
                    cancel_request_id=request_id,
                    cancel_actor=actor,
                    cancel_reason=cancellation_reason,
                )
                if job.operation_id is None:
                    # Nothing was ever issued: the core cancels it at once.
                    adapter.settle(
                        job,
                        CancelRequested(request_id, cancellation_reason),
                        now,
                        reason=cancellation_reason,
                        evidence=evidence,
                    )
                else:
                    # The order carries the cancel; the job mirrors what it decided
                    # (``cancelling`` until the agent's receipt, or until the core
                    # ends the order after its stop budget).
                    adapter.project(
                        job, now, reason=cancellation_reason, evidence=evidence
                    )
            return self._view_in_session(session, job)
