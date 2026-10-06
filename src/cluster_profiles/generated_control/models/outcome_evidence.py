from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.failure_diagnostics import FailureDiagnostics
  from ..models.package_activation_receipt import PackageActivationReceipt





T = TypeVar("T", bound="OutcomeEvidence")



@_attrs_define
class OutcomeEvidence:
    """ Typed facts an executor attaches to a failed or unknown outcome.

    Every field is bounded and already sanitized by the agent; the Controller
    sanitizes again at ingress.  ``diagnostics`` holds the bounded diagnostic
    logs, ``helper_error_code``/``helper_exit_code`` the privileged helper's own
    verdict, ``stage``/``diagnostic`` the phase and a one-line cause.

        Attributes:
            diagnostic (None | str | Unset):
            diagnostics (FailureDiagnostics | None | Unset):
            helper_error_code (None | str | Unset):
            helper_exit_code (int | None | Unset):
            package_activation (None | PackageActivationReceipt | Unset):
            stage (None | str | Unset):
     """

    diagnostic: None | str | Unset = UNSET
    diagnostics: FailureDiagnostics | None | Unset = UNSET
    helper_error_code: None | str | Unset = UNSET
    helper_exit_code: int | None | Unset = UNSET
    package_activation: None | PackageActivationReceipt | Unset = UNSET
    stage: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.failure_diagnostics import FailureDiagnostics # noqa: PLC0415
        from ..models.package_activation_receipt import PackageActivationReceipt # noqa: PLC0415
        diagnostic: None | str | Unset
        if isinstance(self.diagnostic, Unset):
            diagnostic = UNSET
        else:
            diagnostic = self.diagnostic

        diagnostics: dict[str, Any] | None | Unset
        if isinstance(self.diagnostics, Unset):
            diagnostics = UNSET
        elif isinstance(self.diagnostics, FailureDiagnostics):
            diagnostics = self.diagnostics.to_dict()
        else:
            diagnostics = self.diagnostics

        helper_error_code: None | str | Unset
        if isinstance(self.helper_error_code, Unset):
            helper_error_code = UNSET
        else:
            helper_error_code = self.helper_error_code

        helper_exit_code: int | None | Unset
        if isinstance(self.helper_exit_code, Unset):
            helper_exit_code = UNSET
        else:
            helper_exit_code = self.helper_exit_code

        package_activation: dict[str, Any] | None | Unset
        if isinstance(self.package_activation, Unset):
            package_activation = UNSET
        elif isinstance(self.package_activation, PackageActivationReceipt):
            package_activation = self.package_activation.to_dict()
        else:
            package_activation = self.package_activation

        stage: None | str | Unset
        if isinstance(self.stage, Unset):
            stage = UNSET
        else:
            stage = self.stage


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if diagnostic is not UNSET:
            field_dict["diagnostic"] = diagnostic
        if diagnostics is not UNSET:
            field_dict["diagnostics"] = diagnostics
        if helper_error_code is not UNSET:
            field_dict["helper_error_code"] = helper_error_code
        if helper_exit_code is not UNSET:
            field_dict["helper_exit_code"] = helper_exit_code
        if package_activation is not UNSET:
            field_dict["package_activation"] = package_activation
        if stage is not UNSET:
            field_dict["stage"] = stage

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.failure_diagnostics import FailureDiagnostics # noqa: PLC0415
        from ..models.package_activation_receipt import PackageActivationReceipt # noqa: PLC0415
        d = dict(src_dict)
        def _parse_diagnostic(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        diagnostic = _parse_diagnostic(d.pop("diagnostic", UNSET))


        def _parse_diagnostics(data: object) -> FailureDiagnostics | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                diagnostics_type_0 = FailureDiagnostics.from_dict(data)



                return diagnostics_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FailureDiagnostics | None | Unset, data)

        diagnostics = _parse_diagnostics(d.pop("diagnostics", UNSET))


        def _parse_helper_error_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        helper_error_code = _parse_helper_error_code(d.pop("helper_error_code", UNSET))


        def _parse_helper_exit_code(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        helper_exit_code = _parse_helper_exit_code(d.pop("helper_exit_code", UNSET))


        def _parse_package_activation(data: object) -> None | PackageActivationReceipt | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                package_activation_type_0 = PackageActivationReceipt.from_dict(data)



                return package_activation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PackageActivationReceipt | Unset, data)

        package_activation = _parse_package_activation(d.pop("package_activation", UNSET))


        def _parse_stage(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        stage = _parse_stage(d.pop("stage", UNSET))


        outcome_evidence = cls(
            diagnostic=diagnostic,
            diagnostics=diagnostics,
            helper_error_code=helper_error_code,
            helper_exit_code=helper_exit_code,
            package_activation=package_activation,
            stage=stage,
        )

        return outcome_evidence
