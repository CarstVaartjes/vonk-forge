"""Cross-reference checks for topology documents."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


class TopologyValidationError(ValueError):
    """A topology is structurally valid but its references are inconsistent."""


def validate_topology_references(document: Mapping[str, object]) -> None:
    """Reject endpoint references and link identities that are not self-consistent."""

    raw_nodes = document.get("nodes")
    raw_links = document.get("links")
    if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
        raise TopologyValidationError("topology nodes must be a sequence")
    if not isinstance(raw_links, Sequence) or isinstance(raw_links, (str, bytes)):
        raise TopologyValidationError("topology links must be a sequence")

    nodes = set(raw_nodes)
    seen_links: set[object] = set()
    for raw_link in raw_links:
        if not isinstance(raw_link, Mapping):
            raise TopologyValidationError("topology link must be an object")
        link_id = raw_link.get("id")
        if link_id in seen_links:
            raise TopologyValidationError(f"duplicate link id: {link_id}")
        seen_links.add(link_id)
        endpoints = raw_link.get("endpoints")
        if not isinstance(endpoints, Sequence) or isinstance(
            endpoints, (str, bytes)
        ):
            raise TopologyValidationError(f"link {link_id} endpoints must be a sequence")
        for endpoint in endpoints:
            if not isinstance(endpoint, Mapping):
                raise TopologyValidationError(
                    f"link {link_id} endpoint must be an object"
                )
            node_id = endpoint.get("node_id")
            if node_id not in nodes:
                raise TopologyValidationError(
                    f"link {link_id} references unknown node {node_id}"
                )
