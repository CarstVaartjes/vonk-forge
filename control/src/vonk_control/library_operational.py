"""Pure Library operational truth and bounded set-wise evidence loading."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session

from .library_contract import (
    _MAX_CANDIDATE_NODES,
    _MAX_OPERATIONAL_MEMBERS,
    _MAX_OPERATIONAL_ROWS,
    PlacementEvidenceCounts,
    _utc,
)
from .models import (
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)

_ACTIVE_RUN_STATES = frozenset({"planned", "starting", "running", "stopping"})
_RUN_RANK_FRESH_SECONDS = 300


@dataclass(frozen=True, slots=True)
class _MemberEvidence:
    node_id: str
    rank: int
    role: str
    state: str | None = None
    updated_at: datetime | None = None

    @property
    def identity(self) -> tuple[str, int, str]:
        return (self.node_id, self.rank, self.role)


@dataclass(frozen=True, slots=True)
class _InstallationCoverage:
    expected_rank_count: int
    installed_rank_count: int
    complete: bool


@dataclass(frozen=True, slots=True)
class _RunHealth:
    expected_rank_count: int
    healthy_rank_count: int
    healthy: bool
    evidence_code: str | None = None
    evidence_detail: str | None = None


def _members_are_exact(
    expected: Sequence[_MemberEvidence], actual: Sequence[_MemberEvidence]
) -> bool:
    expected_identities = [item.identity for item in expected]
    actual_identities = [item.identity for item in actual]
    return (
        bool(expected_identities)
        and len(expected_identities) == len(set(expected_identities))
        and len(actual_identities) == len(set(actual_identities))
        and len(expected_identities) == len(actual_identities)
        and set(expected_identities) == set(actual_identities)
    )


def _installation_coverage(
    installation_state: str,
    mapping_state: str | None,
    mapping_generation: int | None,
    installation_mapping_generation: int,
    expected: Sequence[_MemberEvidence],
    actual: Sequence[_MemberEvidence],
    *,
    declared_expected_count: int,
) -> _InstallationCoverage:
    """Match RunAdmission's exact installed-membership gate; bytes are informational."""

    expected_identities = {item.identity for item in expected}
    installed_identities = {
        item.identity
        for item in actual
        if item.state == "installed" and item.identity in expected_identities
    }
    complete = (
        installation_state == "installed"
        and mapping_state == "ready"
        and mapping_generation == installation_mapping_generation
        and len(expected) == declared_expected_count
        and _members_are_exact(expected, actual)
        and all(item.state == "installed" for item in actual)
    )
    return _InstallationCoverage(
        expected_rank_count=declared_expected_count,
        installed_rank_count=len(installed_identities),
        complete=complete,
    )


def _run_health(
    plan: object,
    actual: Sequence[_MemberEvidence],
    *,
    current: datetime,
) -> _RunHealth:
    """Match run_status using bounded immutable plan membership evidence."""

    raw_expected = plan.get("nodes") if isinstance(plan, Mapping) else None
    if not isinstance(raw_expected, list):
        return _RunHealth(
            expected_rank_count=0,
            healthy_rank_count=0,
            healthy=False,
            evidence_code="run.plan_invalid",
            evidence_detail="The persisted run plan does not contain a valid nodes list; rank health fails closed.",
        )
    expected_count = len(raw_expected)
    if expected_count > _MAX_CANDIDATE_NODES:
        return _RunHealth(
            expected_rank_count=expected_count,
            healthy_rank_count=0,
            healthy=False,
            evidence_code="projection.evidence_truncated",
            evidence_detail=f"The persisted run plan has {expected_count} members, above the active {_MAX_CANDIDATE_NODES}-member evidence limit; rank health fails closed.",
        )
    expected: list[_MemberEvidence] = []
    for item in raw_expected:
        if not isinstance(item, Mapping):
            expected = []
            break
        node_id = item.get("node_id")
        rank = item.get("rank")
        role = item.get("role")
        if (
            not isinstance(node_id, str)
            or len(node_id) > 36
            or type(rank) is not int
            or not 0 <= rank < _MAX_CANDIDATE_NODES
            or not isinstance(role, str)
            or not role
            or len(role) > 64
        ):
            expected = []
            break
        expected.append(_MemberEvidence(node_id=node_id, rank=rank, role=role))
    if not expected or len({item.identity for item in expected}) != len(expected):
        return _RunHealth(
            expected_rank_count=expected_count,
            healthy_rank_count=0,
            healthy=False,
            evidence_code="run.plan_invalid",
            evidence_detail="The persisted run plan has malformed or duplicate member evidence; rank health fails closed.",
        )

    expected_identities = {item.identity for item in expected}
    healthy_identities = {
        item.identity
        for item in actual
        if item.identity in expected_identities
        and item.state == "running"
        and item.updated_at is not None
        and timedelta(0)
        <= current - _utc(item.updated_at)
        < timedelta(seconds=_RUN_RANK_FRESH_SECONDS)
    }
    healthy = (
        _members_are_exact(expected, actual)
        and len(healthy_identities) == expected_count
    )
    return _RunHealth(
        expected_rank_count=expected_count,
        healthy_rank_count=len(healthy_identities),
        healthy=healthy,
    )


def _group_rows[Row](rows: Sequence[Row], field: str) -> dict[str, list[Row]]:
    grouped: dict[str, list[Row]] = {}
    for row in rows:
        grouped.setdefault(str(getattr(row, field)), []).append(row)
    return grouped


def _member_evidence(rows: Sequence[object]) -> list[_MemberEvidence]:
    return [
        _MemberEvidence(
            node_id=str(row.node_id),  # type: ignore[attr-defined]
            rank=int(row.rank),  # type: ignore[attr-defined]
            role=str(row.role),  # type: ignore[attr-defined]
            state=getattr(row, "state", None),
            updated_at=getattr(row, "updated_at", None),
        )
        for row in rows
    ]


@dataclass(frozen=True)
class _OperationalRows:
    builds: Sequence[RecipeBuild]
    mappings: Sequence[ClusterMapping]
    mapping_nodes: Sequence[ClusterMappingNode]
    installations: Sequence[RecipeInstallation]
    installation_nodes: Sequence[InstallationNode]
    runs: Sequence[RecipeRun]
    run_nodes: Sequence[RunNode]
    mapping_members: Mapping[str, Sequence[ClusterMappingNode]]
    installation_members: Mapping[str, Sequence[InstallationNode]]
    run_members: Mapping[str, Sequence[RunNode]]

    @classmethod
    def collect(
        cls,
        *,
        builds: Sequence[RecipeBuild],
        mappings: Sequence[ClusterMapping],
        mapping_nodes: Sequence[ClusterMappingNode],
        installations: Sequence[RecipeInstallation],
        installation_nodes: Sequence[InstallationNode],
        runs: Sequence[RecipeRun],
        run_nodes: Sequence[RunNode],
    ) -> _OperationalRows:
        return cls(
            builds=builds,
            mappings=mappings,
            mapping_nodes=mapping_nodes,
            installations=installations,
            installation_nodes=installation_nodes,
            runs=runs,
            run_nodes=run_nodes,
            mapping_members=_group_rows(mapping_nodes, "mapping_id"),
            installation_members=_group_rows(installation_nodes, "installation_id"),
            run_members=_group_rows(run_nodes, "run_id"),
        )


@dataclass(frozen=True)
class _PlacementOperationalEvidence:
    operational: _OperationalRows
    counts: PlacementEvidenceCounts

    @property
    def truncated(self) -> bool:
        return bool(self.counts.truncated_collections)


def load_placement_operational_evidence(
    session: Session,
    recipe_revision_id: str,
) -> _PlacementOperationalEvidence:
    """Load fail-closed current placement evidence apart from display history."""

    run_rows = list(
        session.scalars(
            select(RecipeRun)
            .join(
                RecipeInstallation,
                RecipeInstallation.id == RecipeRun.installation_id,
            )
            .where(
                RecipeInstallation.recipe_revision_id == recipe_revision_id,
                RecipeRun.state.in_(_ACTIVE_RUN_STATES),
            )
            .order_by(RecipeRun.updated_at.desc(), RecipeRun.id.desc())
            .limit(_MAX_OPERATIONAL_ROWS + 1)
        )
    )
    runs = run_rows[:_MAX_OPERATIONAL_ROWS]
    referenced_installation_ids = {item.installation_id for item in runs}
    installation_rows = list(
        session.scalars(
            select(RecipeInstallation)
            .where(
                RecipeInstallation.recipe_revision_id == recipe_revision_id,
                RecipeInstallation.state != "uninstalled",
            )
            .order_by(
                case(
                    (
                        RecipeInstallation.id.in_(referenced_installation_ids),
                        0,
                    ),
                    (RecipeInstallation.state == "installed", 1),
                    else_=2,
                ),
                RecipeInstallation.updated_at.desc(),
                RecipeInstallation.id.desc(),
            )
            .limit(_MAX_OPERATIONAL_ROWS + 1)
        )
    )
    installations = installation_rows[:_MAX_OPERATIONAL_ROWS]
    referenced_mapping_ids = {
        *(item.mapping_id for item in runs),
        *(item.mapping_id for item in installations),
    }
    mapping_rows = list(
        session.scalars(
            select(ClusterMapping)
            .where(
                ClusterMapping.recipe_revision_id == recipe_revision_id,
                ClusterMapping.state == "ready",
            )
            .order_by(
                case(
                    (ClusterMapping.id.in_(referenced_mapping_ids), 0),
                    else_=1,
                ),
                ClusterMapping.updated_at.desc(),
                ClusterMapping.id.desc(),
            )
            .limit(_MAX_OPERATIONAL_ROWS + 1)
        )
    )
    mappings = mapping_rows[:_MAX_OPERATIONAL_ROWS]
    referenced_build_ids = {item.recipe_build_id for item in installations}
    build_rows = list(
        session.scalars(
            select(RecipeBuild)
            .where(
                RecipeBuild.recipe_revision_id == recipe_revision_id,
                or_(
                    RecipeBuild.id.in_(referenced_build_ids),
                    RecipeBuild.state == "succeeded",
                ),
            )
            .order_by(
                case(
                    (RecipeBuild.id.in_(referenced_build_ids), 0),
                    else_=1,
                ),
                RecipeBuild.updated_at.desc(),
                RecipeBuild.id.desc(),
            )
            .limit(_MAX_OPERATIONAL_ROWS + 1)
        )
    )
    builds = build_rows[:_MAX_OPERATIONAL_ROWS]
    mapping_node_rows = list(
        session.scalars(
            select(ClusterMappingNode)
            .where(ClusterMappingNode.mapping_id.in_([item.id for item in mappings]))
            .order_by(ClusterMappingNode.mapping_id, ClusterMappingNode.rank)
            .limit(_MAX_OPERATIONAL_MEMBERS + 1)
        )
    )
    installation_node_rows = list(
        session.scalars(
            select(InstallationNode)
            .where(
                InstallationNode.installation_id.in_(
                    [item.id for item in installations]
                )
            )
            .order_by(InstallationNode.installation_id, InstallationNode.rank)
            .limit(_MAX_OPERATIONAL_MEMBERS + 1)
        )
    )
    run_node_rows = list(
        session.scalars(
            select(RunNode)
            .where(RunNode.run_id.in_([item.id for item in runs]))
            .order_by(RunNode.run_id, RunNode.rank)
            .limit(_MAX_OPERATIONAL_MEMBERS + 1)
        )
    )
    observed_counts = {
        "builds": len(build_rows),
        "mappings": len(mapping_rows),
        "mapping_members": len(mapping_node_rows),
        "installations": len(installation_rows),
        "installation_members": len(installation_node_rows),
        "runs": len(run_rows),
        "run_members": len(run_node_rows),
    }
    limits = {
        "builds": _MAX_OPERATIONAL_ROWS,
        "mappings": _MAX_OPERATIONAL_ROWS,
        "mapping_members": _MAX_OPERATIONAL_MEMBERS,
        "installations": _MAX_OPERATIONAL_ROWS,
        "installation_members": _MAX_OPERATIONAL_MEMBERS,
        "runs": _MAX_OPERATIONAL_ROWS,
        "run_members": _MAX_OPERATIONAL_MEMBERS,
    }
    truncated_collections = [
        name
        for name in (
            "builds",
            "mappings",
            "mapping_members",
            "installations",
            "installation_members",
            "runs",
            "run_members",
        )
        if observed_counts[name] > limits[name]
    ]
    counts = PlacementEvidenceCounts(
        **observed_counts,
        truncated_collections=truncated_collections,
    )
    return _PlacementOperationalEvidence(
        operational=_OperationalRows.collect(
            builds=builds,
            mappings=mappings,
            mapping_nodes=mapping_node_rows[:_MAX_OPERATIONAL_MEMBERS],
            installations=installations,
            installation_nodes=installation_node_rows[:_MAX_OPERATIONAL_MEMBERS],
            runs=runs,
            run_nodes=run_node_rows[:_MAX_OPERATIONAL_MEMBERS],
        ),
        counts=counts,
    )
