"""Durable Run/Switch child execution for Controller artifact distribution."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from vonk_agent_protocol.agent_words import ProfileEffectState
from vonk_agent_protocol.contracts import canonical_message
from vonk_agent_protocol.distribution import DistributionObject
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState, WaitReason
from vonk_agent_protocol.reason_codes.runs import RunSwitchCode

from ..agent_jobs.service import AgentJobService
from ..content_identity import ImageContent, same_image
from ..distribution.service import DistributionService
from ..distribution.types import content_objects, plan_objects
from ..distribution_assignment import NodeDistributionAssignment
from ..models.execution import RecipeInstallation
from ..oci_image_store import StoreUnknown
from ..recipe_operations.execution_contract import (
    RecipeExecutionContractError,
    parse_stored_installation_plan,
)
from ..run_switch_contract.operations import RunSwitchOperationResult
from ..run_switch_contract.phase_results import (
    RunSwitchRuntimeImageResult,
    RunSwitchRuntimePlanResult,
)
from ..run_switch_contract.plans import RunSwitchPlan
from ..run_switch_contract.vocabulary import RunSwitchMemberState
from ..run_switch_operations.constants import _MEMBER_STATE_ADAPTER
from ..run_switch_operations.errors import RunSwitchRetryLater
from ..run_switch_operations.image_receipts import effective_build_receipt
from ..runtime_image_preparation.contracts import RuntimeImageStorage
from .receipts import RuntimeImagePull


class DistributionIdentity:
    """Identity boundary for durable artifact distribution."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        operations: AgentJobService,
        distribution: DistributionService,
        *,
        clock: Any,
    ) -> None:
        self._sessions = sessions
        self._operations = operations
        self._distribution = distribution
        self._clock = clock

    def _model_objects(
        self,
        plan: RunSwitchPlan,
        progress: RunSwitchOperationResult,
    ) -> tuple[tuple[DistributionObject, ...], str, int]:
        """The model objects the persisted compiled plan names, without a NAS copy.

        A node keeps the objects it holds, so a Spark holding the model's files
        is sent only the image even after the NAS evicted the set.
        """
        prepared = next(
            (
                receipt
                for receipt in reversed(progress.phase_results)
                if isinstance(receipt, RunSwitchRuntimePlanResult)
            ),
            None,
        )
        installation_id = (
            plan.installation_id if prepared is None else prepared.installation_id
        )
        with self._sessions() as session:
            installation = (
                None
                if installation_id is None
                else session.get(RecipeInstallation, installation_id)
            )
            if installation is None:
                raise RunSwitchRetryLater(
                    RunSwitchCode.INSTALLATION_PREPARATION_UNAVAILABLE,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            try:
                stored = parse_stored_installation_plan(installation.plan)
            except RecipeExecutionContractError as error:
                raise RunSwitchRetryLater(
                    RunSwitchCode.INSTALLATION_IDENTITY_UNAVAILABLE,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error
        model_set_digest = stored.model_artifact_set_sha256
        preparation = plan.preparation
        if model_set_digest is None or (
            preparation is not None
            and preparation.model.artifact_set_sha256 not in (None, model_set_digest)
        ):
            # A newer installation replaced the one this plan prepared.
            raise RunSwitchRetryLater(
                RunSwitchCode.INSTALLATION_IDENTITY_CHANGED,
                reason=WaitReason.SCOPE_CHANGED,
            )
        # Every node is delivered the whole model: the union of its roles' files.
        objects = content_objects(
            tuple(
                item
                for compiled in stored.compiled_execution_plans.values()
                for item in plan_objects(compiled)
            )
        )
        actual_bytes = sum(item.bytes for item in objects)
        model_set_bytes = (
            preparation.model.artifact_set_bytes if preparation is not None else None
        )
        if model_set_bytes is not None and actual_bytes != model_set_bytes:
            raise RunSwitchRetryLater(
                RunSwitchCode.INSTALLATION_IDENTITY_CHANGED,
                reason=WaitReason.SCOPE_CHANGED,
            )
        return objects, model_set_digest, actual_bytes

    @staticmethod
    def _runtime_identity(
        plan: RunSwitchPlan, progress: RunSwitchOperationResult
    ) -> tuple[str, str, int, str | None]:
        image_digest = plan.image_digest
        layout_digest = plan.build.oci_layout_sha256
        image_bytes = plan.build.image_bytes
        build_id = plan.recipe_build_id
        preparation = plan.preparation
        if preparation is not None:
            runtime = preparation.runtime_image
            image_digest = image_digest or runtime.image_digest
            layout_digest = layout_digest or runtime.oci_layout_sha256
            image_bytes = image_bytes or runtime.image_bytes
            build_id = build_id or runtime.build_id
        build = (
            effective_build_receipt(plan, progress)
            if image_digest is None or layout_digest is None or image_bytes is None
            else None
        )
        if build is not None:
            image_digest = image_digest or build.image_digest
            layout_digest = layout_digest or build.oci_layout_sha256
            image_bytes = image_bytes or build.image_bytes
            build_id = build_id or build.build_id
        for receipt in reversed(progress.phase_results):
            if isinstance(receipt, RunSwitchRuntimeImageResult):
                if image_digest is not None and receipt.image_digest != image_digest:
                    raise RuntimeError(
                        "runtime image receipt differs from the exact build"
                    )
                if (
                    layout_digest is not None
                    and receipt.oci_layout_sha256 != layout_digest
                ):
                    raise RuntimeError(
                        "runtime image layout differs from the exact build"
                    )
                if image_bytes is not None and receipt.image_bytes != image_bytes:
                    raise RuntimeError(
                        "runtime image bytes differ from the exact build"
                    )
                image_digest = image_digest or receipt.image_digest
                layout_digest = layout_digest or receipt.oci_layout_sha256
                image_bytes = image_bytes or receipt.image_bytes
                build_id = build_id or receipt.build_id
                break
        if (
            (build_id is not None and not isinstance(build_id, str))
            or not isinstance(image_digest, str)
            or not isinstance(layout_digest, str)
            or type(image_bytes) is not int
        ):
            raise RuntimeError("verified OCI runtime image identity is unavailable")
        return image_digest, layout_digest, image_bytes, build_id

    @staticmethod
    def _source_runtime_storage(source: object) -> RuntimeImageStorage | None:
        """Return the runtime-image storage behind a distribution source."""

        for candidate in (source, getattr(source, "oci_source", None)):
            storage = getattr(candidate, "_runtime_storage", None)
            if storage is not None:
                return storage
        return None

    def _archive(
        self,
        *,
        build_id: str | None,
        image_digest: str,
        layout_digest: str,
        image_bytes: int,
    ) -> RuntimeImagePull:
        """Resolve verified archive content from its managed-storage authority.

        Managed storage verified the bytes at ingress. Producer history is
        optional evidence and cannot veto reuse of this accepted content.
        """

        if not image_digest or not layout_digest or image_bytes < 1:
            raise RuntimeError("verified OCI runtime image identity is unavailable")
        return RuntimeImagePull(
            image_digest=image_digest,
            config_digest=self._stored_config_digest(layout_digest, image_bytes),
            address=layout_digest,
        )

    def _stored_config_digest(self, address: str, image_bytes: int) -> str:
        storage = self._source_runtime_storage(self._distribution.source)
        layout = getattr(storage, "layout", None)
        image = layout.read(f"sha256:{address}") if layout is not None else None
        if (
            image is None
            or isinstance(image, StoreUnknown)
            or image.stored_bytes != image_bytes
        ):
            raise RuntimeError("verified OCI runtime image identity is unavailable")
        return image.config_digest

    def _assignment(
        self,
        plan: RunSwitchPlan,
        node_id: str,
        model_objects: tuple[DistributionObject, ...],
        image: RuntimeImagePull,
        *,
        model_set_digest: str,
    ) -> NodeDistributionAssignment:
        generation = getattr(getattr(plan, "mapping", None), "mapping_generation", None)
        if type(generation) is not int or generation < 1:
            generation = 1
        # UUID v4 is part of the wire contract, while the digest-derived bytes
        # make replay after a Controller restart yield the same assignment.
        seed = f"{plan.plan_digest}:{generation}:{node_id}:{model_set_digest}:{image.address}"
        assignment_bytes = bytearray(hashlib.sha256(seed.encode("utf-8")).digest()[:16])
        assignment_bytes[6] = (assignment_bytes[6] & 0x0F) | 0x40
        assignment_bytes[8] = (assignment_bytes[8] & 0x3F) | 0x80
        return NodeDistributionAssignment(
            assignment_id=str(uuid.UUID(bytes=bytes(assignment_bytes))),
            plan_digest=plan.plan_digest,
            generation=generation,
            node_id=node_id,
            expires_at=self._clock().astimezone(UTC) + timedelta(hours=1),
            model_artifact_set_sha256=model_set_digest,
            # The wire carries content, not paths: the node stores each object
            # by digest, so identical content at several paths is fetched once.
            objects=content_objects(model_objects),
            oci_image_digest=image.image_digest,
            oci_image_config_digest=image.config_digest,
            oci_archive_sha256=image.address,
        )

    @staticmethod
    def _cached_targets(
        plan: RunSwitchPlan, targets: tuple[str, ...]
    ) -> tuple[str, ...]:
        preparation = plan.preparation
        if preparation is None:
            return ()
        model = {item.node_id: item for item in preparation.model.targets}
        image = {item.node_id: item for item in preparation.runtime_image.targets}
        return tuple(
            node_id
            for node_id in targets
            if (
                model.get(node_id) is not None
                and model[node_id].state == "ready"
                and getattr(model[node_id], "verified_at", None) is not None
                and model[node_id].verified_sha256
                == preparation.model.artifact_set_sha256
                and image.get(node_id) is not None
                and image[node_id].state == "ready"
                and getattr(image[node_id], "verified_at", None) is not None
                and same_image(
                    ImageContent(
                        image_digest=image[node_id].imported_image_digest,
                        archive_sha256=image[node_id].verified_sha256,
                    ),
                    preparation.runtime_image,
                )
            )
        )

    @staticmethod
    def _target_bytes(plan: RunSwitchPlan, node_id: str) -> int:
        preparation = plan.preparation
        if preparation is None:
            return 0
        model_bytes = getattr(preparation.model, "artifact_set_bytes", 0)
        image_bytes = getattr(preparation.runtime_image, "image_bytes", 0)
        return model_bytes + image_bytes

    @staticmethod
    def _member_state(value: str) -> RunSwitchMemberState:
        return _MEMBER_STATE_ADAPTER.validate_python(
            {
                LifecycleState.QUEUED.value: ProfileEffectState.PENDING.value,
                LifecycleState.RUNNING.value: LifecycleState.RUNNING.value,
                LifecycleState.SUCCEEDED.value: LifecycleState.SUCCEEDED.value,
                LifecycleState.FAILED.value: LifecycleState.FAILED.value,
            }.get(value, ProfileEffectState.UNKNOWN.value)
        )

    @staticmethod
    def _int(value: object) -> int | None:
        return value if type(value) is int and value >= 0 else None

    @staticmethod
    def _digest(value: BaseModel) -> str:
        return hashlib.sha256(
            canonical_message(value.model_dump(mode="json"))
        ).hexdigest()
