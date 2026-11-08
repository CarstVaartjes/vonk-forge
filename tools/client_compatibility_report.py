"""Report what a published vonkctl would refuse from this Controller.

vonkctl validates every Controller response strictly against the OpenAPI it
was built with. A response shape that this Controller may send and that a
published vonkctl's contract rejects is a break that the installed CLI meets
after the deploy (incident 2026-11-08). This tool compares the two contracts
operation by operation and lists every such shape. It is a report, never a
gate: a deliberate break is legitimate. A released vonkctl answers such a
break with ``controller.protocol_invalid``; a vonkctl that carries the
contract-mismatch check answers ``client.contract_mismatch`` with
``vonkctl update --apply``.

    python tools/client_compatibility_report.py \\
        --candidate src/cluster_profiles/schemas/control-openapi.json \\
        --channel stable --channel dev

``--prior PATH`` compares against a local contract instead of a published
channel. The exit status is 0 whatever the report says.

Limits: the comparison reads a hand-written subset of JSON Schema: ``$ref``
to ``#/components/schemas``, ``type``, ``const``, ``enum``, ``properties``,
``additionalProperties: false``, ``required``, ``items``, ``anyOf``/``oneOf``,
``maxLength``/``minLength``, ``maximum``/``minimum``, ``maxItems`` and
``pattern``. Union alternatives are paired by component name, then by a
``kind`` constant, then by JSON type. Other keywords (``allOf``,
``prefixItems``, ``unevaluatedProperties``, ``patternProperties``, a schema
for ``additionalProperties``, ``exclusiveMinimum``/``exclusiveMaximum``,
``format``, ``if``/``then``/``else``) are ignored, so a break expressed only
through them is not reported, and a pairing by type can name a nearby
alternative. A clean report is evidence, not proof.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue

_REFERENCE = "#/components/schemas/"
_OPENAPI_MEMBER = "cluster_profiles/schemas/control-openapi.json"


class _Read(BaseModel):
    """The part of an OpenAPI document a response validator reads."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class OpenApiSchemaNode(_Read):
    ref: str | None = Field(default=None, alias="$ref")
    type: str | list[str] | None = None
    const: JsonValue = None
    enum: list[JsonValue] | None = None
    properties: dict[str, OpenApiSchemaNode] = Field(default_factory=dict)
    additional_properties: bool | OpenApiSchemaNode | None = Field(
        default=None, alias="additionalProperties"
    )
    required: list[str] = Field(default_factory=list)
    items: OpenApiSchemaNode | None = None
    any_of: list[OpenApiSchemaNode] | None = Field(default=None, alias="anyOf")
    one_of: list[OpenApiSchemaNode] | None = Field(default=None, alias="oneOf")
    max_length: int | None = Field(default=None, alias="maxLength")
    min_length: int | None = Field(default=None, alias="minLength")
    max_items: int | None = Field(default=None, alias="maxItems")
    maximum: float | None = None
    minimum: float | None = None
    pattern: str | None = None

    def values(self) -> list[JsonValue] | None:
        """The finite values this node admits, when it names them."""
        if "const" in self.model_fields_set:
            return [self.const]
        return self.enum

    def types(self) -> set[str] | None:
        """The JSON types this node admits; None when it does not restrict them."""
        values = self.values()
        if values is not None:
            return {_json_type(value) for value in values}
        if isinstance(self.type, str):
            return {"number", "integer"} if self.type == "number" else {self.type}
        if isinstance(self.type, list):
            return set(self.type)
        return None

    def kind(self) -> JsonValue:
        tag = self.properties.get("kind")
        values = tag.values() if tag is not None else None
        return values[0] if values is not None and len(values) == 1 else None


class OpenApiMediaType(_Read):
    schema_: OpenApiSchemaNode | None = Field(default=None, alias="schema")


class OpenApiResponse(_Read):
    content: dict[str, OpenApiMediaType] = Field(default_factory=dict)


class OpenApiOperation(_Read):
    responses: dict[str, OpenApiResponse] = Field(default_factory=dict)
    observation_payload: OpenApiSchemaNode | None = Field(
        default=None, alias="x-vonk-observation-payload"
    )


class OpenApiPathItem(_Read):
    get: OpenApiOperation | None = None
    put: OpenApiOperation | None = None
    post: OpenApiOperation | None = None
    delete: OpenApiOperation | None = None
    patch: OpenApiOperation | None = None

    def operations(self) -> dict[str, OpenApiOperation]:
        found = {
            "get": self.get,
            "put": self.put,
            "post": self.post,
            "delete": self.delete,
            "patch": self.patch,
        }
        return {method: item for method, item in found.items() if item is not None}


class OpenApiComponents(_Read):
    schemas: dict[str, OpenApiSchemaNode] = Field(default_factory=dict)


class OpenApiDocument(_Read):
    """One OpenAPI document, read into typed nodes once."""

    paths: dict[str, OpenApiPathItem] = Field(default_factory=dict)
    components: OpenApiComponents = Field(default_factory=OpenApiComponents)

    def resolve(self, node: OpenApiSchemaNode) -> tuple[OpenApiSchemaNode, str | None]:
        name = None
        seen: set[str] = set()
        while node.ref is not None and node.ref.startswith(_REFERENCE):
            if node.ref in seen:
                break
            seen.add(node.ref)
            name = node.ref.removeprefix(_REFERENCE)
            node = self.components.schemas.get(name, OpenApiSchemaNode())
        return node, name

    def alternatives(
        self, node: OpenApiSchemaNode
    ) -> list[tuple[OpenApiSchemaNode, str | None]]:
        resolved, name = self.resolve(node)
        branches = resolved.any_of or resolved.one_of
        if branches is None:
            return [(resolved, name)]
        return [
            alternative
            for branch in branches
            for alternative in self.alternatives(branch)
        ]


def _json_type(value: JsonValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def _describe(node: OpenApiSchemaNode, name: str | None) -> str:
    if name is not None:
        return name
    types = node.types()
    return "|".join(sorted(types)) if types else "any value"


def _fits(prior: OpenApiSchemaNode, value: JsonValue) -> bool:
    if isinstance(value, str):
        if prior.max_length is not None and len(value) > prior.max_length:
            return False
        if prior.min_length is not None and len(value) < prior.min_length:
            return False
        return prior.pattern is None or re.search(prior.pattern, value) is not None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (prior.minimum is None or prior.minimum <= value) and (
            prior.maximum is None or value <= prior.maximum
        )
    return True


def _wider_bounds(
    prior: OpenApiSchemaNode, candidate: OpenApiSchemaNode, where: str
) -> Iterator[str]:
    uppers = (
        ("maxLength", prior.max_length, candidate.max_length),
        ("maximum", prior.maximum, candidate.maximum),
        ("maxItems", prior.max_items, candidate.max_items),
    )
    lowers = (
        ("minLength", prior.min_length, candidate.min_length),
        ("minimum", prior.minimum, candidate.minimum),
    )
    for bound, before, after in uppers:
        if before is not None and (after is None or after > before):
            yield f"{where}: {bound} {before} may now be {after}"
    for bound, before, after in lowers:
        if before is not None and (after is None or after < before):
            yield f"{where}: {bound} {before} may now be {after}"
    if prior.pattern is not None and candidate.pattern != prior.pattern:
        yield f"{where}: pattern changed"


def _match(
    known: list[tuple[OpenApiSchemaNode, str | None]],
    node: OpenApiSchemaNode,
    name: str | None,
) -> OpenApiSchemaNode | None:
    if name is not None:
        for prior, prior_name in known:
            if prior_name == name:
                return prior
    tag = node.kind()
    if tag is not None:
        for prior, _ in known:
            if prior.kind() == tag:
                return prior
    types = node.types()
    for prior, _ in known:
        accepted = prior.types()
        if accepted is None or (types is not None and types <= accepted):
            return prior
    return None


class _Comparison:
    def __init__(self, prior: OpenApiDocument, candidate: OpenApiDocument) -> None:
        self.prior = prior
        self.candidate = candidate
        #: Schema node pairs already compared in this operation: each is
        #: reported once per operation, and recursive schemas end.
        self.seen: set[tuple[int, int]] = set()

    def schema(
        self, prior: OpenApiSchemaNode, candidate: OpenApiSchemaNode, where: str
    ) -> Iterator[str]:
        """Shapes ``candidate`` admits that ``prior`` refuses."""
        known = self.prior.alternatives(prior)
        for node, name in self.candidate.alternatives(candidate):
            match = _match(known, node, name)
            if match is None:
                yield f"{where}: may now be {_describe(node, name)}"
                continue
            pair = (id(match), id(node))
            if pair in self.seen:
                continue
            self.seen.add(pair)
            yield from self._single(match, node, where)

    def _single(
        self, prior: OpenApiSchemaNode, candidate: OpenApiSchemaNode, where: str
    ) -> Iterator[str]:
        accepted, sent = prior.types(), candidate.types()
        if accepted is not None and (sent is None or not sent <= accepted):
            yield f"{where}: type {sorted(accepted)} may now be {sorted(sent or {'any'})}"
            return
        allowed, finite = prior.values(), candidate.values()
        if allowed is not None:
            if finite is None:
                yield f"{where}: was one of {allowed}, may now be any value"
            elif extra := [value for value in finite if value not in allowed]:
                yield f"{where}: may now be {extra}"
        if finite is not None:
            # Known values: each must fit the prior bounds, whatever the
            # candidate declares about them.
            if refused := [value for value in finite if not _fits(prior, value)]:
                yield f"{where}: may now be {refused}"
        else:
            yield from _wider_bounds(prior, candidate, where)
        closed = prior.additional_properties is False
        for name, schema in candidate.properties.items():
            if name in prior.properties:
                yield from self.schema(
                    prior.properties[name], schema, f"{where}.{name}"
                )
            elif closed:
                yield f"{where}: new property {name!r}"
        for name in prior.required:
            if name not in candidate.required:
                yield f"{where}: {name!r} may now be absent"
        if prior.items is not None and candidate.items is not None:
            yield from self.schema(prior.items, candidate.items, f"{where}[]")

    def operation(
        self, prior: OpenApiOperation, candidate: OpenApiOperation, where: str
    ) -> Iterator[str]:
        self.seen.clear()
        if candidate.observation_payload is not None:
            yield from self.schema(
                prior.observation_payload or OpenApiSchemaNode(),
                candidate.observation_payload,
                f"{where} payload",
            )
        for status, response in candidate.responses.items():
            known = prior.responses.get(status, prior.responses.get("default"))
            if known is None:
                yield f"{where} {status}: status undocumented in the prior contract"
                continue
            for media_type, content in response.content.items():
                media = known.content.get(media_type)
                if media is None:
                    yield f"{where} {status}: media type {media_type} undocumented"
                elif content.schema_ is not None:
                    yield from self.schema(
                        media.schema_ or OpenApiSchemaNode(),
                        content.schema_,
                        f"{where} {status}",
                    )


def breaks(prior: JsonValue, candidate: JsonValue) -> list[str]:
    """Every response shape ``candidate`` may send that ``prior`` refuses.

    Only operations the prior contract knows matter: a client never calls a
    route it does not have.
    """
    from cluster_profiles.control_client.schema import _open_shown_only_words

    # The prior client opens the words it only shows when it loads its schema.
    opened = json.loads(json.dumps(prior))
    _open_shown_only_words(opened)
    comparison = _Comparison(
        OpenApiDocument.model_validate(opened),
        OpenApiDocument.model_validate(candidate),
    )
    found: list[str] = []
    for template, item in sorted(comparison.prior.paths.items()):
        sent_item = comparison.candidate.paths.get(template)
        sent = sent_item.operations() if sent_item is not None else {}
        for method, operation in item.operations().items():
            where = f"{method.upper()} {template}"
            if method in sent:
                found.extend(comparison.operation(operation, sent[method], where))
            else:
                found.append(f"{where}: route removed")
    return list(dict.fromkeys(found))


def _published_contract(channel: str) -> tuple[str, JsonValue]:
    """The OpenAPI inside the accepted signed CLI wheel of ``channel``."""
    from cluster_profiles.cli_update import (
        _NOTICE_ORIGIN,
        INSTALLER_PUBLIC_KEY,
        CliUpdateIngressError,
        _download,
        _signed_release,
    )

    release, base = _signed_release(
        channel=channel,
        public_key=INSTALLER_PUBLIC_KEY,
        origin=_NOTICE_ORIGIN,
        download=_download,
    )
    artifact = release["artifacts"]["cli-wheel"]  # type: ignore[index]
    wheel = _download(f"{base}/{artifact['path']}", artifact["size"])
    if hashlib.sha256(wheel).hexdigest() != artifact["sha256"]:
        raise CliUpdateIngressError("CLI wheel digest is invalid")
    with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
        document = json.loads(archive.read(_OPENAPI_MEMBER))
    return f"{channel} {release['version']} ({release['source_sha']})", document


def _report(label: str, found: list[str]) -> str:
    if not found:
        return f"### {label}\n\nNo response shape it would refuse.\n"
    lines = "\n".join(f"- {item}" for item in found)
    return (
        f"### {label}\n\n{len(found)} response shape(s) it would refuse. A "
        "released vonkctl answers them with `controller.protocol_invalid`; one "
        "with the contract-mismatch check answers `client.contract_mismatch` "
        "and `vonkctl update --apply`.\n\n"
        f"{lines}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report what a published vonkctl would refuse."
    )
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--channel", action="append", default=[])
    parser.add_argument("--prior", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    candidate = json.loads(args.candidate.read_text())
    print("## Published vonkctl against this Controller's responses\n")
    priors: list[tuple[str, JsonValue]] = [
        (str(path), json.loads(path.read_text())) for path in args.prior
    ]
    for channel in args.channel:
        try:
            priors.append(_published_contract(channel))
        except Exception as error:  # noqa: BLE001 - a report never fails its job
            print(f"### {channel}\n\nPublished contract unavailable: {error}\n")
    for label, prior in priors:
        print(_report(label, breaks(prior, candidate)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
