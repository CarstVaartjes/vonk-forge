"""Availability of independently recoverable Controller capabilities."""

from datetime import datetime
from enum import StrEnum

from .strict_json import StrictModel


class ControllerCapability(StrEnum):
    CERTIFICATE_AUTHORITY = "certificate-authority"
    ENROLLMENT_BOOTSTRAP = "enrollment-bootstrap"
    HOST_RUNTIME_AUTHORITY = "host-runtime-authority"
    MODEL_CACHE = "model-cache"
    RUNTIME_IMAGE_STORAGE = "runtime-image-storage"
    ARTIFACT_STORAGE = "artifact-storage"
    MANAGEMENT_POLICY = "management-policy"
    FABRIC_POLICY = "fabric-policy"
    AGENT_PRESENCE = "agent-presence"
    IMAGE_COLLECTION = "image-collection"
    ROUTE_PUBLISHER = "route-publisher"
    RECIPE_ROUTES = "recipe-routes"
    DISTRIBUTION = "distribution"
    RECIPE_LIBRARY = "recipe-library"
    TOKEN_AUTH = "token-auth"
    CURSOR_AUTH = "cursor-auth"
    BROWSER_AUTH = "browser-auth"
    METRICS_AUTH = "metrics-auth"
    AGENT_PROXY_AUTH = "agent-proxy-auth"
    GATEWAY_KEYS = "gateway-keys"


class CapabilityAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class CapabilityReason(StrEnum):
    CA_KEY_ENCODING_UNSUPPORTED = (
        "capability.ca_key_encoding_unsupported_rerun_nas_installer"
    )
    INITIALIZING = "capability.initializing"
    CONFIGURATION_INVALID = "capability.configuration_invalid"
    STORAGE_UNAVAILABLE = "capability.storage_unavailable"
    DEPENDENCY_UNAVAILABLE = "capability.dependency_unavailable"


class CapabilityStatus(StrictModel):
    capability: ControllerCapability
    availability: CapabilityAvailability
    reason: CapabilityReason | None = None
    next_attempt_at: datetime | None = None


class CapabilityUnavailableReply(StrictModel):
    capability: ControllerCapability
    reason: CapabilityReason
    retryable: bool = True
