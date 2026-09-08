from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.agent_deployment_evidence_connectivity import AgentDeploymentEvidenceConnectivity
from ..models.agent_deployment_evidence_connectivity import check_agent_deployment_evidence_connectivity
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.evidence_age import EvidenceAge





T = TypeVar("T", bound="AgentDeploymentEvidence")



@_attrs_define
class AgentDeploymentEvidence:
    """
        Attributes:
            connectivity (AgentDeploymentEvidenceConnectivity):
            display_name (str):
            evidence (EvidenceAge):
            node_id (str):
            package_evidence (EvidenceAge):
            state (str):
            binary_sha256 (Union[None, Unset, str]):
            boundary (Union[Literal['agent_deployment'], Unset]):  Default: 'agent_deployment'.
            build_digest (Union[None, Unset, str]):
            package_sha256 (Union[None, Unset, str]):
            semantic_version (Union[None, Unset, str]):
     """

    connectivity: AgentDeploymentEvidenceConnectivity
    display_name: str
    evidence: 'EvidenceAge'
    node_id: str
    package_evidence: 'EvidenceAge'
    state: str
    binary_sha256: Union[None, Unset, str] = UNSET
    boundary: Union[Literal['agent_deployment'], Unset] = 'agent_deployment'
    build_digest: Union[None, Unset, str] = UNSET
    package_sha256: Union[None, Unset, str] = UNSET
    semantic_version: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.evidence_age import EvidenceAge
        connectivity: str = self.connectivity

        display_name = self.display_name

        evidence = self.evidence.to_dict()

        node_id = self.node_id

        package_evidence = self.package_evidence.to_dict()

        state = self.state

        binary_sha256: Union[None, Unset, str]
        if isinstance(self.binary_sha256, Unset):
            binary_sha256 = UNSET
        else:
            binary_sha256 = self.binary_sha256

        boundary = self.boundary

        build_digest: Union[None, Unset, str]
        if isinstance(self.build_digest, Unset):
            build_digest = UNSET
        else:
            build_digest = self.build_digest

        package_sha256: Union[None, Unset, str]
        if isinstance(self.package_sha256, Unset):
            package_sha256 = UNSET
        else:
            package_sha256 = self.package_sha256

        semantic_version: Union[None, Unset, str]
        if isinstance(self.semantic_version, Unset):
            semantic_version = UNSET
        else:
            semantic_version = self.semantic_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "connectivity": connectivity,
            "display_name": display_name,
            "evidence": evidence,
            "node_id": node_id,
            "package_evidence": package_evidence,
            "state": state,
        })
        if binary_sha256 is not UNSET:
            field_dict["binary_sha256"] = binary_sha256
        if boundary is not UNSET:
            field_dict["boundary"] = boundary
        if build_digest is not UNSET:
            field_dict["build_digest"] = build_digest
        if package_sha256 is not UNSET:
            field_dict["package_sha256"] = package_sha256
        if semantic_version is not UNSET:
            field_dict["semantic_version"] = semantic_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.evidence_age import EvidenceAge
        d = dict(src_dict)
        connectivity = check_agent_deployment_evidence_connectivity(d.pop("connectivity"))




        display_name = d.pop("display_name")

        evidence = EvidenceAge.from_dict(d.pop("evidence"))




        node_id = d.pop("node_id")

        package_evidence = EvidenceAge.from_dict(d.pop("package_evidence"))




        state = d.pop("state")

        def _parse_binary_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        binary_sha256 = _parse_binary_sha256(d.pop("binary_sha256", UNSET))


        boundary = cast(Union[Literal['agent_deployment'], Unset] , d.pop("boundary", UNSET))
        if boundary != 'agent_deployment' and not isinstance(boundary, Unset):
            raise ValueError(f"boundary must match const 'agent_deployment', got '{boundary}'")

        def _parse_build_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        build_digest = _parse_build_digest(d.pop("build_digest", UNSET))


        def _parse_package_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        package_sha256 = _parse_package_sha256(d.pop("package_sha256", UNSET))


        def _parse_semantic_version(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        semantic_version = _parse_semantic_version(d.pop("semantic_version", UNSET))


        agent_deployment_evidence = cls(
            connectivity=connectivity,
            display_name=display_name,
            evidence=evidence,
            node_id=node_id,
            package_evidence=package_evidence,
            state=state,
            binary_sha256=binary_sha256,
            boundary=boundary,
            build_digest=build_digest,
            package_sha256=package_sha256,
            semantic_version=semantic_version,
        )

        return agent_deployment_evidence
