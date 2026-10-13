"""The closed set of security edges a refusal may name.

A :class:`~vonk_agent_protocol.SecurityRefusalError` is terminal and never
retried, so it is only for a real security boundary.  Each closed
:class:`~vonk_agent_protocol.SecurityRefusalReason` names exactly one of the
edges below; :mod:`failure_classification` treats a code as a security refusal
only through this map.

Bookkeeping drift is never a security edge.  An owner that is unavailable is a
wait (:class:`~vonk_agent_protocol.UnknownOutcomeError`); stored state that
changed, was superseded or no longer agrees is a conflict the owner re-plans
(an :class:`~vonk_agent_protocol.InvalidRequestError` with
``InvalidRequestReason.SUPERSEDED``); stored evidence that does not parse is an
invalid request with its reason (``MALFORMED``).
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType

from vonk_agent_protocol import SecurityRefusalReason as Reason


class SecurityEdge(StrEnum):
    """Where failing closed is right: the only causes a refusal may carry."""

    AUTHENTICATION = "authentication"
    AUTHORISATION = "authorisation"
    IDENTITY = "identity"
    REVOCATION = "revocation"
    SIGNED_METADATA = "signed-metadata"
    INGRESS_DIGEST = "ingress-digest"
    FENCE = "fence"


_AUTHN = SecurityEdge.AUTHENTICATION
_AUTHZ = SecurityEdge.AUTHORISATION
_IDENTITY = SecurityEdge.IDENTITY
_REVOKED = SecurityEdge.REVOCATION
_SIGNED = SecurityEdge.SIGNED_METADATA
_DIGEST = SecurityEdge.INGRESS_DIGEST
_FENCE = SecurityEdge.FENCE

#: Every closed security reason and the one edge it guards.  A new reason must
#: name its edge here, or ``test_security_classes`` fails.
SECURITY_REFUSAL_EDGES = MappingProxyType(
    {
        Reason.HTTP_401: _AUTHN,
        Reason.HTTP_403: _AUTHZ,
        Reason.AGENT_CERTIFICATE_ROTATION_CONFLICT: _IDENTITY,
        Reason.AGENT_ENROLLMENT_SUBMIT_REJECTED: _AUTHN,
        Reason.AGENT_EXPIRED_RENEWAL_REFUSED: _IDENTITY,
        Reason.AGENT_EXPIRED_RENEWAL_GRACE_EXHAUSTED: _IDENTITY,
        Reason.AGENT_IDENTITY_MISMATCH: _IDENTITY,
        Reason.AGENT_TOMBSTONE_FENCED: _FENCE,
        Reason.CATALOG_AUTHENTICATION_REQUIRED: _AUTHN,
        Reason.CONTROLLER_AUTHENTICATION_REQUIRED: _AUTHN,
        Reason.CONTROLLER_FLEET_ENROLLMENT_DENIED: _AUTHZ,
        Reason.CONTROLLER_REQUEST_REJECTED: _AUTHZ,
        Reason.DIGEST_MISMATCH: _DIGEST,
        Reason.DISTRIBUTION_REVOKED: _REVOKED,
        Reason.FORBIDDEN: _AUTHZ,
        Reason.GRANT_INVALID: _SIGNED,
        Reason.GRANT_NODE_MISMATCH: _AUTHZ,
        Reason.GRANT_UNAUTHORIZED: _AUTHZ,
        Reason.HELPER_AUTHORIZATION_INVALID: _AUTHZ,
        Reason.HELPER_GRANT_INVALID: _SIGNED,
        Reason.HELPER_GRANT_NODE_MISMATCH: _AUTHZ,
        Reason.HELPER_GRANT_UNAUTHORIZED: _AUTHZ,
        Reason.HELPER_OPERATION_INVALID_ARTIFACT: _DIGEST,
        Reason.HELPER_PEER_IDENTITY_INVALID: _IDENTITY,
        Reason.HELPER_REQUEST_INSTALLATION_IDENTITY_INVALID: _SIGNED,
        Reason.HELPER_REQUEST_PLAN_BINDING_INVALID: _SIGNED,
        Reason.HELPER_REQUEST_REPLAYED: _FENCE,
        Reason.HELPER_RUNTIME_IMAGE_IDENTITY_INVALID: _DIGEST,
        Reason.HOST_HELPER_AUTHORITY_DENIED: _AUTHZ,
        Reason.LOCAL_IDENTITY_EXPIRED: _IDENTITY,
        Reason.LOCAL_IDENTITY_FAILED: _IDENTITY,
        Reason.MODEL_CACHE_CREDENTIALS_DENIED: _AUTHN,
        Reason.MODEL_CACHE_CREDENTIALS_INVALID: _AUTHN,
        Reason.MODEL_CACHE_SOURCE_ACCESS_DENIED: _AUTHZ,
        Reason.OPERATION_INVALID_ARTIFACT: _DIGEST,
        Reason.PEER_IDENTITY_INVALID: _IDENTITY,
        Reason.PERMISSION_DENIED: _AUTHZ,
        Reason.RECIPE_UPDATE_AUTHORITY_DENIED: _AUTHZ,
        Reason.REQUEST_REPLAYED: _FENCE,
        Reason.RUN_SWITCH_ARTIFACT_DIGEST_VERIFICATION_FAILED: _DIGEST,
        Reason.RUN_SWITCH_CLEANUP_NAS_EVICTION_FORBIDDEN: _AUTHZ,
        Reason.RUN_SWITCH_CLEANUP_RECLAIMED_DIGEST_NOT_PLANNED: _AUTHZ,
        Reason.RUN_SWITCH_RUNTIME_IMAGE_PREPARATION_DIGEST_MISMATCH: _DIGEST,
        Reason.RUNTIME_IMAGE_AUTHORIZATION_INVALID: _SIGNED,
        Reason.RUNTIME_IMAGE_AUTHORIZATION_REVOKED: _REVOKED,
        Reason.RUNTIME_IMAGE_IDENTITY_INVALID: _DIGEST,
        Reason.STALE_FENCE: _FENCE,
        Reason.TUF_METADATA_INVALID: _SIGNED,
        Reason.TUF_SIGNATURE_INVALID: _SIGNED,
        Reason.UNAUTHORIZED: _AUTHN,
        Reason.UNSAFE_PATH: _AUTHZ,
    }
)


__all__ = ["SECURITY_REFUSAL_EDGES", "SecurityEdge"]
