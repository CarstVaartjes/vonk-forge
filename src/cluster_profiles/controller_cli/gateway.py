"""``gateway model`` and ``gateway credential``: external models and their bindings.

The Controller owns every decision.  A change is reviewed, then accepted exactly as
reviewed: the accept carries the review's ``effect_digest``, the revision the
operator read and the request key, so a refused, changed or repeated request never
applies something else.  A credential is only ever a binding name; the secret is
provisioned in NAS custody and never passes through this command.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass

from cluster_profiles.cli_files import read_json_document
from cluster_profiles.cli_json_contract import Word
from cluster_profiles.cli_render.dispatch import render_payload
from cluster_profiles.cli_states import PARTIAL_FIELD
from cluster_profiles.cli_states_generated import (
    ExternalBindingAction,
    ExternalModelAction,
)
from cluster_profiles.control_client.errors import ControlNotFound, LocalInputError
from cluster_profiles.controller_cli.common import (
    ControllerClient,
    _quoted,
    _request_key,
)
from cluster_profiles.controller_cli.confirmation import _can_prompt, _confirm_action
from cluster_profiles.generated_control.models.approved_destination import (
    ApprovedDestination,
)
from cluster_profiles.generated_control.models.external_binding_accept_request import (
    ExternalBindingAcceptRequest,
)
from cluster_profiles.generated_control.models.external_binding_accepted import (
    ExternalBindingAccepted,
)
from cluster_profiles.generated_control.models.external_binding_action import (
    check_external_binding_action,
)
from cluster_profiles.generated_control.models.external_binding_list import (
    ExternalBindingList,
)
from cluster_profiles.generated_control.models.external_binding_preview_request import (
    ExternalBindingPreviewRequest,
)
from cluster_profiles.generated_control.models.external_binding_review import (
    ExternalBindingReview,
)
from cluster_profiles.generated_control.models.external_model_accept_request import (
    ExternalModelAcceptRequest,
)
from cluster_profiles.generated_control.models.external_model_accepted import (
    ExternalModelAccepted,
)
from cluster_profiles.generated_control.models.external_model_action import (
    check_external_model_action,
)
from cluster_profiles.generated_control.models.external_model_input import (
    ExternalModelInput,
)
from cluster_profiles.generated_control.models.external_model_list import (
    ExternalModelList,
)
from cluster_profiles.generated_control.models.external_model_preview_request import (
    ExternalModelPreviewRequest,
)
from cluster_profiles.generated_control.models.external_model_review import (
    ExternalModelReview,
)
from cluster_profiles.generated_control.models.external_model_view import (
    ExternalModelView,
)
from cluster_profiles.generated_control.models.external_validate_request import (
    ExternalValidateRequest,
)
from cluster_profiles.generated_control.models.external_validation import (
    ExternalValidation,
)
from cluster_profiles.generated_control.types import UNSET

_MODELS = "/api/gateway/models"
_CREDENTIALS = "/api/gateway/credentials"
#: The marker of the one document a change returns (unpublished, like the removal review).
CHANGE_FIELD = "gateway_change"

_MODEL_CONSEQUENCE = {
    ExternalModelAction.PUT: "Publish {name}? The gateway restarts to load it.",
    ExternalModelAction.ENABLE: "Publish {name} again? The gateway restarts to load it.",
    ExternalModelAction.DISABLE: "Withdraw {name}? Calls to it stop; the entry is kept.",
    ExternalModelAction.REMOVE: "Remove {name}? Calls to it stop and the entry is deleted.",
}
_BINDING_CONSEQUENCE = {
    ExternalBindingAction.PUT: (
        "Change where credential {name} may be sent? "
        "A model whose destination it no longer approves is withdrawn."
    ),
    ExternalBindingAction.REMOVE: (
        "Remove credential {name}? Every model that uses it is withdrawn."
    ),
}


@dataclass(frozen=True)
class GatewayChange:
    """The one document of a change: what was reviewed and, when consented, accepted."""

    target: str
    name: str
    action: str
    review: ExternalModelReview | ExternalBindingReview | ExternalValidation
    accepted: ExternalModelAccepted | ExternalBindingAccepted | None = None
    refused: bool = False

    def to_dict(self):
        document = {
            CHANGE_FIELD: True,
            "target": self.target,
            "name": self.name,
            "action": self.action,
            "review": self.review.to_dict(),
        }
        if self.accepted is not None:
            document["accepted"] = self.accepted.to_dict()
        if self.refused:
            document[PARTIAL_FIELD] = True
        return document


def _gateway(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
):
    if args.gateway_action == Word.CREDENTIAL:
        return _credential(args, client, factory)
    return _model(args, client, factory)


def _model(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
):
    action = args.gateway_model_action
    if action in (None, Word.LIST):
        return ExternalModelList.from_dict(client.request("GET", _MODELS))
    if action == Word.SHOW:
        return ExternalModelView.from_dict(
            client.request("GET", f"{_MODELS}/{_quoted(args.alias)}")
        )
    if action == Word.VALIDATE:
        return _validation(client, _entry(args.file))
    if action == ExternalModelAction.PUT:
        entry = _entry(args.file)
        alias = entry.model_name
        # Every problem at once, with no traffic sent; the preview repeats the check.
        validation = _validation(client, entry)
        if validation.problems:
            return GatewayChange(Word.MODEL, alias, action, validation, refused=True)
    else:
        entry = None
        alias = args.alias
    path = f"{_MODELS}/{_quoted(alias)}"
    revision = _model_revision(client, path)
    request = ExternalModelPreviewRequest(
        action=check_external_model_action(action),
        entry=UNSET if entry is None else entry,
        expected_revision=UNSET if revision is None else revision,
    )
    review = ExternalModelReview.from_dict(
        client.request("POST", f"{path}/preview", request.to_dict())
    )
    change = GatewayChange(
        Word.MODEL, alias, action, review, refused=bool(review.problems)
    )
    if change.refused or args.review:
        return _reviewed_only(args, change)
    _consent(args, change, _MODEL_CONSEQUENCE[ExternalModelAction(action)])
    accept = ExternalModelAcceptRequest(
        action=request.action,
        effect_digest=review.effect_digest,
        request_id=_request_key(args, factory),
        entry=request.entry,
        expected_revision=request.expected_revision,
    )
    accepted = ExternalModelAccepted.from_dict(
        client.request("POST", f"{path}/accept", accept.to_dict())
    )
    return GatewayChange(Word.MODEL, alias, action, review, accepted)


def _credential(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
):
    action = args.gateway_credential_action
    bindings = ExternalBindingList.from_dict(client.request("GET", _CREDENTIALS))
    if action in (None, Word.LIST):
        return bindings
    held = next(
        (
            binding.revision
            for binding in bindings.bindings
            if binding.name == args.name
        ),
        None,
    )
    destinations = UNSET
    if action == ExternalBindingAction.PUT:
        destinations = [
            ApprovedDestination(
                origin=origin,
                private=args.private,
                region=UNSET if args.region is None else args.region,
            )
            for origin in args.origins
        ]
    request = ExternalBindingPreviewRequest(
        action=check_external_binding_action(action),
        destinations=destinations,
        expected_revision=UNSET if held is None else held,
    )
    path = f"{_CREDENTIALS}/{_quoted(args.name)}"
    review = ExternalBindingReview.from_dict(
        client.request("POST", f"{path}/preview", request.to_dict())
    )
    change = GatewayChange(Word.CREDENTIAL, args.name, action, review)
    if args.review:
        return _reviewed_only(args, change)
    _consent(args, change, _BINDING_CONSEQUENCE[ExternalBindingAction(action)])
    accept = ExternalBindingAcceptRequest(
        action=request.action,
        effect_digest=review.effect_digest,
        request_id=_request_key(args, factory),
        destinations=request.destinations,
        expected_revision=request.expected_revision,
    )
    accepted = ExternalBindingAccepted.from_dict(
        client.request("POST", f"{path}/accept", accept.to_dict())
    )
    return GatewayChange(Word.CREDENTIAL, args.name, action, review, accepted)


def _reviewed_only(args: argparse.Namespace, change: GatewayChange) -> GatewayChange:
    """A review is shown and nothing is accepted: a read, never a mutation."""

    args.outcome_context = "read"
    return change


def _consent(args: argparse.Namespace, change: GatewayChange, message: str) -> None:
    if not args.yes and not _can_prompt(args):
        raise ValueError(
            "gateway changes require --yes in noninteractive mode; "
            "--review shows the change without applying it"
        )
    if not args.yes:
        with redirect_stdout(sys.stderr):
            render_payload(change.to_dict(), Word.GATEWAY, action=change.target)
    _confirm_action(args, message.format(name=change.name))


def _validation(
    client: ControllerClient, entry: ExternalModelInput
) -> ExternalValidation:
    return ExternalValidation.from_dict(
        client.request(
            "POST",
            f"{_MODELS}/validate",
            ExternalValidateRequest(entry=entry).to_dict(),
        )
    )


def _entry(source: str) -> ExternalModelInput:
    document = read_json_document(source)
    if not isinstance(document, dict) or not isinstance(
        document.get("model_name"), str
    ):
        raise LocalInputError(
            "the entry file must hold one JSON object with a model_name: "
            "the alias clients call"
        )
    unknown = sorted(set(document) - {"model_name", "litellm_params", "model_info"})
    if unknown:
        # The generated model would drop them silently before the server could refuse.
        raise LocalInputError(
            f"the entry has unknown top-level keys: {', '.join(unknown)}"
        )
    return ExternalModelInput.from_dict(document)


def _model_revision(client: ControllerClient, path: str) -> int | None:
    """The revision the operator reads before changing: ``None`` for a new model."""

    try:
        view = ExternalModelView.from_dict(client.request("GET", path))
    except ControlNotFound:
        return None
    return view.desired_revision
