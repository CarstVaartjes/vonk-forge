"""Bounded management-address policy for authenticated agent evidence."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .auth import AgentIdentity, AgentSource, AuthError
from .models import AgentCertificate, AgentNode, AgentPresence

type _Network = ipaddress.IPv4Network | ipaddress.IPv6Network
type _Address = ipaddress.IPv4Address | ipaddress.IPv6Address

_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")


class PresenceError(ValueError):
    """A management address or its configured policy is invalid."""


def _utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PresenceError(f"{label} must include a timezone")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    """Normalize PostgreSQL timestamps and SQLite's timezone-less test values."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_networks(value: str, *, label: str, required: bool) -> tuple[_Network, ...]:
    if not value.strip():
        if required:
            raise PresenceError(f"{label} cannot be empty")
        return ()
    comma_groups = value.split(",")
    if any(not item.strip() for item in comma_groups):
        raise PresenceError(f"{label} cannot contain an empty network")
    raw_networks = [network for group in comma_groups for network in group.split()]
    networks: list[_Network] = []
    for raw in raw_networks:
        try:
            network = ipaddress.ip_network(raw, strict=True)
        except ValueError as error:
            raise PresenceError(
                f"{label} must contain canonical CIDR networks"
            ) from error
        if str(network) != raw:
            raise PresenceError(f"{label} must contain canonical CIDR networks")
        if network in networks:
            raise PresenceError(f"{label} cannot contain duplicate networks")
        networks.append(network)
    return tuple(networks)


@dataclass(frozen=True)
class ManagementAddressPolicy:
    """Allow canonical IP literals from explicit management networks only."""

    allowed_networks: tuple[_Network, ...]
    forbidden_networks: tuple[_Network, ...] = ()

    @classmethod
    def parse(
        cls,
        value: str,
        *,
        forbidden_cidrs: str = "",
    ) -> ManagementAddressPolicy:
        allowed = _parse_networks(value, label="management CIDRs", required=True)
        forbidden = _parse_networks(
            forbidden_cidrs,
            label="forbidden CIDRs",
            required=False,
        )
        for network in allowed:
            if any(
                network.version == blocked.version and network.subnet_of(blocked)
                for blocked in forbidden
            ):
                raise PresenceError(f"management CIDR {network} is fully forbidden")
        return cls(allowed, forbidden)

    def validate(self, value: str) -> str:
        try:
            address: _Address = ipaddress.ip_address(value)
        except ValueError as error:
            raise PresenceError(
                "management address must be a canonical IP literal"
            ) from error
        if str(address) != value:
            raise PresenceError("management address must be a canonical IP literal")
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
        ):
            raise PresenceError(
                "management address belongs to a prohibited address class"
            )
        matching = tuple(
            network
            for network in self.allowed_networks
            if address.version == network.version and address in network
        )
        if not matching:
            raise PresenceError(
                "management address is outside configured management CIDRs"
            )
        if any(
            address.version == network.version and address in network
            for network in self.forbidden_networks
        ):
            raise PresenceError("management address belongs to a forbidden CIDR")
        if any(
            network.prefixlen < network.max_prefixlen
            and (
                address == network.network_address
                or (
                    isinstance(network, ipaddress.IPv4Network)
                    and address == network.broadcast_address
                )
            )
            for network in matching
        ):
            raise PresenceError(
                "management address cannot be a network or broadcast address"
            )
        return address.compressed


@dataclass(frozen=True)
class ManagementAddressObservation:
    """Address-only view of one authenticated durable presence record."""

    node_id: str
    certificate_serial: str
    address: str
    observed_at: datetime


class AgentPresenceService:
    """Persist and retrieve certificate-bound management addresses."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        policy: ManagementAddressPolicy,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = sessions
        self._policy = policy
        self._clock = clock

    @staticmethod
    def _lock_active_node(session: Session, node_id: str) -> AgentNode | None:
        return session.scalar(
            select(AgentNode)
            .where(
                AgentNode.node_id == node_id,
                AgentNode.state == "active",
                AgentNode.revoked_at.is_(None),
            )
            .with_for_update(of=AgentNode)
        )

    @staticmethod
    def _lock_active_certificate(
        session: Session,
        identity: AgentIdentity,
        now: datetime,
    ) -> AgentCertificate | None:
        return session.scalar(
            select(AgentCertificate)
            .where(
                AgentCertificate.serial == identity.certificate_serial,
                AgentCertificate.node_id == identity.node_id,
                AgentCertificate.fingerprint == identity.certificate_fingerprint,
                AgentCertificate.state == "active",
                AgentCertificate.revoked_at.is_(None),
                AgentCertificate.ca_revoked_at.is_(None),
                AgentCertificate.not_before <= now,
                AgentCertificate.not_after > now,
            )
            .with_for_update(of=AgentCertificate)
        )

    def observe(self, source: AgentSource) -> ManagementAddressObservation:
        with self._sessions.begin() as session:
            return self.observe_in_session(session, source)

    def observe_in_session(
        self,
        session: Session,
        source: AgentSource,
    ) -> ManagementAddressObservation:
        """Persist presence using a caller-owned, Node-first transaction."""
        now = _utc(self._clock(), label="presence clock")
        address = self.validate(source).management_address
        node = self._lock_active_node(session, source.identity.node_id)
        if node is None:
            raise PresenceError("agent node is not active")
        certificate = self._lock_active_certificate(session, source.identity, now)
        if certificate is None:
            raise PresenceError("agent certificate is not active")
        row = session.scalar(
            select(AgentPresence)
            .where(AgentPresence.node_id == source.identity.node_id)
            .with_for_update(of=AgentPresence)
        )
        if row is None:
            row = AgentPresence(node_id=source.identity.node_id)
            session.add(row)
        row.certificate_serial = certificate.serial
        row.certificate_fingerprint = certificate.fingerprint
        row.management_address = address
        row.observed_at = now
        certificate_serial = certificate.serial
        return ManagementAddressObservation(
            node_id=source.identity.node_id,
            certificate_serial=certificate_serial,
            address=address,
            observed_at=now,
        )

    def validate(self, source: AgentSource) -> AgentSource:
        """Apply address policy without creating durable contact evidence."""
        address = self._policy.validate(source.management_address)
        if address == source.management_address:
            return source
        return AgentSource(identity=source.identity, management_address=address)

    def latest(
        self,
        node_id: str,
        *,
        maximum_age_seconds: int,
    ) -> ManagementAddressObservation:
        with self._sessions.begin() as session:
            return self.latest_in_session(
                session,
                node_id,
                maximum_age_seconds=maximum_age_seconds,
            )

    def latest_in_session(
        self,
        session: Session,
        node_id: str,
        *,
        maximum_age_seconds: int,
    ) -> ManagementAddressObservation:
        """Resolve presence using a caller-owned, Node-first transaction."""
        if _NODE_ID.fullmatch(node_id) is None:
            raise PresenceError("node ID is invalid")
        if maximum_age_seconds <= 0:
            raise PresenceError("maximum age must be positive")
        now = _utc(self._clock(), label="presence clock")
        row = session.get(AgentPresence, node_id)
        if row is None:
            raise PresenceError("management address presence is unavailable")
        try:
            identity = AgentIdentity(
                node_id=row.node_id,
                certificate_serial=row.certificate_serial,
                certificate_fingerprint=row.certificate_fingerprint,
                verified=True,
            )
        except AuthError as error:
            raise PresenceError("presence certificate binding is invalid") from error
        if self._lock_active_node(session, node_id) is None:
            raise PresenceError("agent node is not active")
        if self._lock_active_certificate(session, identity, now) is None:
            raise PresenceError("presence certificate is not active")
        original_binding = (
            row.certificate_serial,
            row.certificate_fingerprint,
        )
        session.refresh(row, with_for_update=True)
        if original_binding != (
            row.certificate_serial,
            row.certificate_fingerprint,
        ):
            raise PresenceError("presence certificate changed during read")
        observed_at = _stored_utc(row.observed_at)
        address = self._policy.validate(row.management_address)
        certificate_serial = row.certificate_serial
        if observed_at > now:
            raise PresenceError("management address presence is in the future")
        if now - observed_at > timedelta(seconds=maximum_age_seconds):
            raise PresenceError("management address presence is stale")
        return ManagementAddressObservation(
            node_id=node_id,
            certificate_serial=certificate_serial,
            address=address,
            observed_at=observed_at,
        )
