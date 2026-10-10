"""Independent agent enrollment, presence, distribution and grant capabilities."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from .agent_api import AgentApiServices
from .auth import AgentSource
from .capabilities import CapabilityRegistry
from .capability_contract import ControllerCapability
from .enrollment import EnrollmentService
from .settings import (
    AGENT_CA_PROVISIONER_NAME,
    Settings,
)
from .source_bundles import DatabaseSourceBundleStore


def build_enrollment_service(
    settings: Settings,
    sessions: sessionmaker[Session],
    clock: Callable[[], datetime],
) -> EnrollmentService:
    """Factory evaluated only by the shared certificate capability owner."""
    from .local_ca import LocalCertificateAuthority

    authority = LocalCertificateAuthority(
        sessions=sessions,
        root_certificate_path=settings.agent_ca_root_path,
        intermediate_certificate_path=settings.agent_intermediate_certificate_path,
        provisioner_name=AGENT_CA_PROVISIONER_NAME,
        provisioner_kid=settings.agent_ca_provisioner_kid,
        intermediate_key_path=settings.secrets_root / "step-ca" / "intermediate-key",
        password_path=settings.secrets_root / "step-ca" / "password",
    )
    # Verify the existing PKI and carry durable revocation intent before use.
    # Exact issuance runs outside the enrollment admission transaction.
    return EnrollmentService(sessions, authority, clock=clock)


def build_agent_services(
    settings: Any,
    sessions: Any,
    clock: Callable[[], Any],
    *,
    distribution: Any | None = None,
    model_cache: Any | None = None,
    capabilities: CapabilityRegistry | None = None,
) -> AgentApiServices:
    """Construct the fail-closed production agent runtime from one provider."""
    from .agent_jobs import AgentJobService
    from .enrollment import EnrollmentService
    from .enrollment_bootstrap import EnrollmentBootstrapConfig
    from .host_helper_authority import (
        HostHelperGrantIssuer,
        HostRuntimeAuthorityService,
    )
    from .presence import AgentPresenceService, ManagementAddressPolicy

    registry = capabilities or CapabilityRegistry()
    if distribution is None and model_cache is not None:
        from .distribution import (
            DistributionService,
            build_distribution_service_from_components,
        )

        distribution = registry.guard(
            ControllerCapability.DISTRIBUTION,
            DistributionService,
            lambda: build_distribution_service_from_components(
                model_cache,
                sessions,
                settings.agent_artifact_root,
                clock=clock,
            ),
        )
    if distribution is not None and capabilities is None:
        attach_sessions = getattr(distribution, "attach_sessions", None)
        if callable(attach_sessions):
            attach_sessions(sessions)

    if not settings.agent_runtime_enabled:
        # Local development still needs the durable operation queue and fleet
        # presence service, but deliberately has no enrollment or certificate
        # authority.  Agent HTTP routes stay disabled by production_app.
        operations = AgentJobService(
            sessions,
            clock=clock,
        )
        presence = registry.guard(
            ControllerCapability.AGENT_PRESENCE,
            AgentPresenceService,
            lambda: AgentPresenceService(
                sessions,
                ManagementAddressPolicy.parse(
                    settings.management_cidrs or "127.0.0.1/32",
                    forbidden_cidrs=settings.direct_fabric_cidrs,
                ),
                clock=clock,
            ),
        )
        return AgentApiServices(
            enrollment=None,
            operations=operations,
            sessions=sessions,
            clock=clock,
            presence=presence,
            artifact_root=settings.agent_artifact_root,
            source_bundles=DatabaseSourceBundleStore(sessions),
            distribution=distribution,
        )

    bootstrap = registry.guard(
        ControllerCapability.ENROLLMENT_BOOTSTRAP,
        EnrollmentBootstrapConfig,
        lambda: EnrollmentBootstrapConfig.from_paths(
            controller_endpoint=settings.agent_controller_origin,
            enrollment_endpoint=settings.agent_enrollment_origin,
            controller_ca_path=settings.controller_ca_path,
            controller_address=settings.nas_lan_ip,
            service_hostnames=(
                settings.agent_service_hostnames if settings.nas_lan_ip else ()
            ),
            installer_url=(
                "https://install.vonkforge.ai/dev/spark"
                if settings.install_channel == "dev"
                else "https://install.vonkforge.ai/spark"
            ),
        ),
    )

    presence = registry.guard(
        ControllerCapability.AGENT_PRESENCE,
        AgentPresenceService,
        lambda: AgentPresenceService(
            sessions,
            ManagementAddressPolicy.parse(
                settings.management_cidrs,
                forbidden_cidrs=settings.direct_fabric_cidrs,
            ),
            clock=clock,
        ),
    )
    operations = AgentJobService(
        sessions,
        clock=clock,
    )

    def observe_contact(session: Session, source: AgentSource) -> None:
        presence.observe_in_session(session, source)

    operations.set_contact_consumer(observe_contact)

    def build_host_authority() -> HostRuntimeAuthorityService:
        host_runtime_key_path = settings.host_runtime_grant_private_key_path
        if host_runtime_key_path is None:
            raise FileNotFoundError
        return HostRuntimeAuthorityService(
            sessions,
            HostHelperGrantIssuer.from_private_key_file(
                host_runtime_key_path, clock=clock
            ),
            clock=clock,
        )

    host_runtime_authority = registry.guard(
        ControllerCapability.HOST_RUNTIME_AUTHORITY,
        HostRuntimeAuthorityService,
        build_host_authority,
    )

    return AgentApiServices(
        enrollment=registry.guard(
            ControllerCapability.CERTIFICATE_AUTHORITY,
            EnrollmentService,
            lambda: build_enrollment_service(settings, sessions, clock),
        ),
        operations=operations,
        sessions=sessions,
        clock=clock,
        presence=presence,
        artifact_root=settings.agent_artifact_root,
        source_bundles=DatabaseSourceBundleStore(sessions),
        distribution=distribution,
        host_runtime_authority=host_runtime_authority,
        fabric_policy=(
            registry.guard(
                ControllerCapability.FABRIC_POLICY,
                ManagementAddressPolicy,
                lambda: ManagementAddressPolicy.parse(
                    settings.direct_fabric_cidrs,
                    forbidden_cidrs=settings.management_cidrs,
                ),
            )
            if settings.direct_fabric_cidrs
            else None
        ),
        bootstrap=bootstrap,
    )
