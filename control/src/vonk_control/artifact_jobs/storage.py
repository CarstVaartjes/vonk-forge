"""Artifact Jobs: storage."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    InvalidRequestReason,
    RecipeJobFile,
    RecipeJobInputFile,
    SecurityRefusalReason,
    recipe_job_manifest_sha256,
)
from vonk_agent_protocol.job_inputs import RecipeJobInputManifest

from .. import artifact_job_states as ajs
from ..artifact_blob_store import ArtifactBlobStore, StoredArtifactBlob
from ..artifact_job_evidence import read_result_evidence
from ..categorized_errors import InvalidValue, MissingRecord
from ..compiled_artifact_contract import CompiledArtifactContract
from ..lifecycle import Outcome, Reported
from ..lifecycle.agent_operation import AgentOperationAdapter
from ..lifecycle.artifact_job import ArtifactJobAdapter
from ..lifecycle.evidence import (
    BookkeepingReason,
    Damaged,
    Residue,
    read_or_rebuild,
    retire_as_unknown,
)
from ..models import (
    AgentOperation,
    ArtifactJob,
    ArtifactJobBlob,
    ArtifactJobFile,
    ClusterMappingNode,
    Job,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_installation_plan,
    parse_stored_run_plan,
)
from ..recipe_operations import RecipeOperationService
from .contracts import (
    ArtifactFileDeclaration,
    ArtifactJobError,
    ArtifactJobInvalid,
    ArtifactJobRefused,
    ArtifactJobResponse,
    ArtifactJobTransferClosedError,
    ArtifactJobView,
    ArtifactOutputFile,
    OutputLimits,
    _active_recipe_revision,
    _artifact_submission_in_session,
    _canonical_declared_parameters,
    _compile_contract,
    _JobLaunch,
    _read_input_manifest,
    _record_unservable_run,
)


class ArtifactJobService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        recipe_operations: RecipeOperationService,
        blob_store: ArtifactBlobStore,
        clock: Callable[[], datetime],
        retention_seconds: int = 7 * 24 * 60 * 60,
    ) -> None:
        if not 3600 <= retention_seconds <= 365 * 24 * 60 * 60:
            raise InvalidValue(
                "artifact job retention is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        self._sessions = sessions
        self._recipe_operations = recipe_operations
        self._blob_store = blob_store
        self._clock = clock
        self._retention_seconds = retention_seconds

    @staticmethod
    def _reject_result(
        adapter: ArtifactJobAdapter,
        operation: AgentOperation,
        parent: Job,
        artifact_job: ArtifactJob,
        error: Exception,
        now: datetime,
    ) -> None:
        """A result that breaks the contract ends the order and the job, failed."""

        AgentOperationAdapter(adapter.session).record_outcome(
            operation, None, parent, Outcome.FAILED, now
        )
        adapter.settle(
            artifact_job,
            Reported(Outcome.FAILED, retryable=False),
            now,
            reason=str(error)[:512],
        )

    def _authorized_agent_job(
        self, session: Session, job_id: str, node_id: str, *, lock: bool = False
    ) -> ArtifactJob:
        statement = select(ArtifactJob).where(ArtifactJob.id == job_id)
        if lock:
            statement = statement.with_for_update(of=ArtifactJob)
        job = session.scalar(statement)
        if job is None or job.operation_id is None:
            raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
        parent = session.get(Job, job.operation_id)
        if parent is None or node_id not in parent.targets:
            raise ArtifactJobRefused(
                "agent is not authorized for this artifact job",
                reason=SecurityRefusalReason.FORBIDDEN,
            )
        order = session.scalar(
            select(AgentOperation.state).where(
                AgentOperation.parent_job_id == job.operation_id
            )
        )
        if ajs.state_of(job) not in ajs.QUEUED_OR_RUNNING or order not in {
            "queued",
            "running",
        }:
            # A job that ended, or whose attempt lapsed (its order is only being
            # observed), is fenced: its bytes are no longer accepted.
            raise ArtifactJobTransferClosedError("artifact job transfer is closed")
        return job

    def _job_launch(
        self,
        session: Session,
        artifact_job: ArtifactJob,
        run: RecipeRun,
        installation: RecipeInstallation,
        node: RunNode,
    ) -> _JobLaunch | None:
        """The stored evidence a submission needs, or ``None`` when it is damaged.

        Rebuilt from evidence, not from the stored run plan: the accepted capacity
        promise is the run node's own reservation, the memory floor is carried by
        the installed compiled plan, the contract is recompiled from its recipe
        and the declared inputs from their uploads.  What nothing re-derives is
        recorded as residue and read as ``None``.
        """

        try:
            installation_plan = parse_stored_installation_plan(installation.plan)
        except RecipeExecutionContractError as error:
            _record_unservable_run(run.id, f"stored installation plan: {error}")
            return None
        compiled = installation_plan.compiled_execution_plans.get(node.node_id)
        if compiled is None:
            _record_unservable_run(run.id, "no compiled plan for the job node")
            return None
        floor = compiled.runtime.placement.memory_floor_bytes
        contract = self._stored_contract(session, artifact_job)
        manifest = self._stored_input_manifest(session, artifact_job)
        if isinstance(contract, Residue) or isinstance(manifest, Residue):
            return None
        try:
            parameters = _canonical_declared_parameters(
                contract, artifact_job.parameters
            )
        except (ArtifactJobError, TypeError, ValueError) as error:
            _record_unservable_run(run.id, f"stored job parameters: {error}")
            return None
        return _JobLaunch(
            installed_plan=compiled,
            memory_floor_bytes=floor,
            contract=contract,
            parameters=parameters,
            input_files=list(manifest.files),
        )

    @staticmethod
    def _job_node_in_session(session: Session, run: RecipeRun) -> RunNode:
        nodes = tuple(
            session.scalars(
                select(RunNode).where(RunNode.run_id == run.id).order_by(RunNode.rank)
            )
        )

        def planned_endpoints() -> set[str]:
            return {
                node.node_id
                for node in parse_stored_run_plan(run.plan).nodes
                if node.endpoint_owner
            }

        def mapped_endpoints() -> set[str] | None:
            # Rebuild from evidence: the cluster mapping names each node's role
            # in the run, independent of the stored run plan.
            if run.mapping_id is None:
                return None
            return set(
                session.scalars(
                    select(ClusterMappingNode.node_id).where(
                        ClusterMappingNode.mapping_id == run.mapping_id,
                        ClusterMappingNode.endpoint_owner.is_(True),
                    )
                )
            )

        loaded = read_or_rebuild(
            kind="artifact-job.run-plan",
            subject=run.id,
            read=planned_endpoints,
            rebuild=mapped_endpoints,
        )
        endpoint_ids = set() if isinstance(loaded, Residue) else loaded
        candidates = [node for node in nodes if node.node_id in endpoint_ids]
        if not candidates and len(nodes) == 1:
            candidates = list(nodes)
        if len(candidates) != 1 or candidates[0].state != "running":
            # The run's endpoint owner is not running (yet, or any more): the
            # run is not accepting jobs, which is the requester's to retry.
            raise ArtifactJobInvalid(
                "artifact job endpoint owner is not running",
                reason=InvalidRequestReason.NOT_READY,
            )
        return candidates[0]

    @staticmethod
    def _put_blob_in_session(
        session: Session, stored: StoredArtifactBlob, now: datetime
    ) -> None:
        blob = session.get(ArtifactJobBlob, stored.sha256)
        if blob is not None:
            if (
                blob.size_bytes != stored.size_bytes
                or blob.storage_key != stored.storage_key
            ):
                raise ArtifactJobRefused(
                    "content-addressed artifact collision",
                    reason=SecurityRefusalReason.DIGEST_MISMATCH,
                )
            return
        session.add(
            ArtifactJobBlob(
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                storage_key=stored.storage_key,
                created_at=now,
            )
        )

    @staticmethod
    def _file_in_session(
        session: Session, job_id: str, direction: str, name: str
    ) -> ArtifactJobFile | None:
        return session.scalar(
            select(ArtifactJobFile).where(
                ArtifactJobFile.artifact_job_id == job_id,
                ArtifactJobFile.direction == direction,
                ArtifactJobFile.name == name,
            )
        )

    def _stored_input_manifest(
        self, session: Session, job: ArtifactJob
    ) -> RecipeJobInputManifest | Residue:
        """The job's declared inputs: stored, else rebuilt from its uploaded rows.

        The rebuilt manifest is evidence only when it has the digest the job was
        created under; otherwise the damaged manifest is retired as unknown.
        """

        def rebuild() -> RecipeJobInputManifest | None:
            rows = self._files_in_session(session, job.id, "input")
            if any(row.slot is None for row in rows):
                return None
            files = sorted(
                (
                    RecipeJobInputFile(
                        slot=row.slot or "",
                        name=row.name,
                        media_type=row.media_type,
                        size_bytes=row.size_bytes,
                        sha256=row.blob_sha256,
                    )
                    for row in rows
                ),
                key=lambda item: item.name.encode("utf-8"),
            )
            rebuilt = RecipeJobInputManifest(
                schema_version=1,
                total_bytes=sum(item.size_bytes for item in files),
                files=files,
            )
            return (
                rebuilt
                if rebuilt.total_bytes == job.input_total_bytes
                and recipe_job_manifest_sha256(tuple(files))
                == job.input_manifest_sha256
                else None
            )

        return read_or_rebuild(
            kind="artifact-job.input-manifest",
            subject=job.id,
            read=lambda: _read_input_manifest(job),
            rebuild=rebuild,
        )

    def _input_declaration(
        self, session: Session, job: ArtifactJob, name: str
    ) -> RecipeJobInputFile | None:
        manifest = self._stored_input_manifest(session, job)
        if isinstance(manifest, Residue):
            return None
        return next((item for item in manifest.files if item.name == name), None)

    @staticmethod
    def _files_in_session(
        session: Session, job_id: str, direction: str
    ) -> tuple[ArtifactJobFile, ...]:
        return tuple(
            session.scalars(
                select(ArtifactJobFile)
                .where(
                    ArtifactJobFile.artifact_job_id == job_id,
                    ArtifactJobFile.direction == direction,
                )
                .order_by(ArtifactJobFile.name)
            )
        )

    @staticmethod
    def _input_files(
        rows: Sequence[ArtifactJobFile],
        manifest: RecipeJobInputManifest | Residue,
    ) -> list[RecipeJobInputFile]:
        """The uploaded inputs with their slots.

        A row that lost its slot takes it from the declared manifest (the slot
        is declared by name); one the manifest cannot place is left out and
        recorded, so it reads as not uploaded and is uploaded again.
        """

        declared = (
            {}
            if isinstance(manifest, Residue)
            else {item.name: item.slot for item in manifest.files}
        )
        files: list[RecipeJobInputFile] = []
        for row in rows:
            slot = row.slot if row.slot is not None else declared.get(row.name)
            if slot is None:
                retire_as_unknown(
                    "artifact-job.input-slot",
                    row.artifact_job_id,
                    BookkeepingReason.ROW_INCOMPLETE,
                    f"uploaded input {row.name} has no slot",
                )
                continue
            files.append(
                RecipeJobInputFile(
                    slot=slot,
                    name=row.name,
                    media_type=row.media_type,
                    size_bytes=row.size_bytes,
                    sha256=row.blob_sha256,
                )
            )
        return files

    @staticmethod
    def _output_file(item: ArtifactJobFile) -> RecipeJobFile:
        return RecipeJobFile(
            name=item.name,
            media_type=item.media_type,
            size_bytes=item.size_bytes,
            sha256=item.blob_sha256,
        )

    def _stored_contract(
        self, session: Session, job: ArtifactJob
    ) -> CompiledArtifactContract | Residue:
        """The job's compiled contract: stored, else recompiled from its recipe.

        The recompiled contract is evidence only when it has the digest the job
        was created under; otherwise the damaged contract is retired as unknown.
        """

        def rebuild() -> CompiledArtifactContract | None:
            run = session.get(RecipeRun, job.run_id)
            installation = (
                session.get(RecipeInstallation, run.installation_id)
                if run is not None
                else None
            )
            resolved = (
                _active_recipe_revision(session, installation.recipe_revision_id)
                if installation is not None
                else None
            )
            if resolved is None:
                return None
            compiled = _compile_contract(resolved[1], job.interface)
            return (
                compiled
                if not isinstance(compiled, Damaged)
                and compiled.sha256() == job.contract_sha256
                else None
            )

        return read_or_rebuild(
            kind="artifact-job.contract",
            subject=job.id,
            read=lambda: CompiledArtifactContract.parse(job.compiled_contract),
            rebuild=rebuild,
        )

    def _view_in_session(self, session: Session, job: ArtifactJob) -> ArtifactJobView:
        submission = _artifact_submission_in_session(session, job)
        adapter = ArtifactJobAdapter(session, clock=self._clock)
        state, actions, cancel_requested_at = adapter.view(job)
        manifest = self._stored_input_manifest(session, job)
        inputs = tuple(
            ArtifactFileDeclaration.model_validate(item.model_dump(mode="json"))
            for item in self._input_files(
                self._files_in_session(session, job.id, "input"), manifest
            )
        )
        outputs = tuple(
            ArtifactOutputFile.model_validate(
                self._output_file(item).model_dump(mode="json")
            )
            for item in self._files_in_session(session, job.id, "output")
        )
        contract = self._stored_contract(session, job)
        view = ArtifactJobView(
            id=job.id,
            run_id=job.run_id,
            # A submission that cannot be read leaves the pair unset together.
            operation_id=job.operation_id if isinstance(submission, Job) else None,
            submit_request_id=(
                submission.request_id if isinstance(submission, Job) else None
            ),
            interface=job.interface,
            state=state,
            preparation=ajs.preparation_of(job),
            cancel_requested_at=cancel_requested_at,
            contract_sha256=job.contract_sha256,
            compiled_contract=None if isinstance(contract, Residue) else contract,
            input_manifest_sha256=job.input_manifest_sha256,
            input_total_bytes=job.input_total_bytes,
            input_declarations=(
                None
                if isinstance(manifest, Residue)
                else tuple(
                    ArtifactFileDeclaration.model_validate(item.model_dump(mode="json"))
                    for item in manifest.files
                )
            ),
            input_files=inputs,
            output_limits=OutputLimits.model_validate(job.output_limits),
            output_manifest_sha256=job.output_manifest_sha256,
            output_files=outputs,
            result_evidence=read_result_evidence(job.result_evidence),
            status_reason=job.status_reason,
            timeout_seconds=job.timeout_seconds,
            created_at=job.created_at,
            updated_at=job.updated_at,
            supported_actions=actions,
        )
        ArtifactJobResponse.model_validate(view, from_attributes=True)
        return view
