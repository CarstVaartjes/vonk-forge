"""Durable source-ingress proof bound to an exact managed file copy."""

from vonk_agent_protocol.source_bundles import SourceBundleManifest

from .strict_json import StrictJSONModel


class SourceBundleVerification(StrictJSONModel):
    """Ingress proof bound to one physical copy, never a request-time hash."""

    manifest: SourceBundleManifest
    identity: tuple[int, int, int, int, int]
