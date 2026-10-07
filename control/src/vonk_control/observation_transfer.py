"""Complete immutable observations delivered in bounded canonical records.

The allocation belongs to a single transport record, not the whole observation
or the process heap. No domain fact is truncated to make a record fit.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from collections.abc import Iterator
from typing import Annotated, Literal

from pydantic import Field, RootModel, field_validator
from starlette.responses import StreamingResponse

from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES

from .strict_json import StrictModel, serialize_json_value

OBSERVATION_MEDIA_TYPE = "application/x-vonk-observation+ndjson"
TransferId = Annotated[
    str,
    Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        json_schema_extra={"not": {"pattern": r"[^0-9a-f-]"}},
    ),
]
# The last alphabet character encodes only real bits, never nonzero padding
# bits. The extra schema rule also excludes the newline accepted by some `$`
# regex implementations. Both rules belong to this canonical field.
_BASE64_PATTERN = (
    r"^(?:[A-Za-z0-9+/]{4})*"
    r"(?:[A-Za-z0-9+/]{4}|[A-Za-z0-9+/][AQgw]==|"
    r"[A-Za-z0-9+/]{2}[AEIMQUYcgkosw048]=)$"
)


class ObservationTransferStart(StrictModel):
    type: Literal["start"]
    transfer_id: TransferId
    resource: Literal["fleet", "platform"]
    encoding: Literal["base64-canonical-json-utf8-v1"]


class ObservationTransferChunk(StrictModel):
    type: Literal["chunk"]
    transfer_id: TransferId
    ordinal: Annotated[int, Field(ge=0)]
    data: Annotated[
        str,
        Field(
            min_length=4,
            max_length=MAX_CONTROL_DOCUMENT_BYTES,
            pattern=_BASE64_PATTERN,
            json_schema_extra={"not": {"pattern": r"[^A-Za-z0-9+/=]"}},
        ),
    ]

    @field_validator("data")
    @classmethod
    def canonical_base64(cls, value: str) -> str:
        if re.fullmatch(_BASE64_PATTERN, value) is None:
            raise ValueError("observation fragment must be canonical base64")
        return value


class ObservationTransferComplete(StrictModel):
    type: Literal["complete"]
    transfer_id: TransferId
    chunks: Annotated[int, Field(ge=1)]
    bytes: Annotated[int, Field(ge=1)]
    sha256: Annotated[
        str,
        Field(
            pattern=r"^[0-9a-f]{64}$",
            json_schema_extra={"not": {"pattern": r"[^0-9a-f]"}},
        ),
    ]


class ObservationTransferError(StrictModel):
    type: Literal["error"]
    transfer_id: TransferId
    reason_code: Literal["observation.transfer_unavailable"]
    detail: Annotated[str, Field(min_length=1, max_length=256)]


class ObservationTransferRecord(
    RootModel[
        Annotated[
            ObservationTransferStart
            | ObservationTransferChunk
            | ObservationTransferComplete
            | ObservationTransferError,
            Field(discriminator="type"),
        ]
    ]
):
    """One NDJSON record; EOF after a verified complete record is success."""


def _record(record: StrictModel) -> bytes:
    encoded = (
        json.dumps(
            serialize_json_value(record),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    if len(encoded) > MAX_CONTROL_DOCUMENT_BYTES:
        raise ValueError("observation record exceeds its reader allocation")
    return encoded


class ObservationTransferResponse(StreamingResponse):
    media_type = OBSERVATION_MEDIA_TYPE


def observation_response(
    value: StrictModel, *, resource: Literal["fleet", "platform"]
) -> ObservationTransferResponse:
    # Freeze the complete projection before any network suspension. Domain
    # services have already closed their SQL sessions. This dictionary owns no
    # references to mutable ORM rows or the producer's model containers.
    frozen = serialize_json_value(value)
    transfer_id = str(uuid.uuid4())

    def records() -> Iterator[bytes]:
        yield _record(
            ObservationTransferStart(
                type="start",
                transfer_id=transfer_id,
                resource=resource,
                encoding="base64-canonical-json-utf8-v1",
            )
        )
        digest = hashlib.sha256()
        ordinal = 0
        byte_count = 0
        pending = bytearray()

        def fragment(raw: bytes) -> bytes:
            nonlocal ordinal, byte_count
            encoded = _record(
                ObservationTransferChunk(
                    type="chunk",
                    transfer_id=transfer_id,
                    ordinal=ordinal,
                    data=base64.b64encode(raw).decode("ascii"),
                )
            )
            digest.update(raw)
            ordinal += 1
            byte_count += len(raw)
            return encoded

        try:
            # Derive the available base64 space from the actual envelope. The
            # final serialization is measured as well; max_length(data) alone
            # is never a wire-size proof.
            def allocation() -> int:
                envelope = (
                    len(
                        _record(
                            ObservationTransferChunk(
                                type="chunk",
                                transfer_id=transfer_id,
                                ordinal=ordinal,
                                data="AAAA",
                            )
                        )
                    )
                    - 4
                )
                return (MAX_CONTROL_DOCUMENT_BYTES - envelope) // 4 * 3

            available = allocation()
            encoder = json.JSONEncoder(
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for token in encoder.iterencode(frozen):
                raw = token.encode("utf-8")
                offset = 0
                while offset < len(raw):
                    if available <= 0:
                        raise ValueError("observation record envelope cannot fit")
                    take = min(available - len(pending), len(raw) - offset)
                    pending.extend(raw[offset : offset + take])
                    offset += take
                    if len(pending) == available:
                        yield fragment(bytes(pending))
                        pending.clear()
                        available = allocation()
            if pending:
                yield fragment(bytes(pending))
            yield _record(
                ObservationTransferComplete(
                    type="complete",
                    transfer_id=transfer_id,
                    chunks=ordinal,
                    bytes=byte_count,
                    sha256=digest.hexdigest(),
                )
            )
        except (TypeError, ValueError, OverflowError):
            yield _record(
                ObservationTransferError(
                    type="error",
                    transfer_id=transfer_id,
                    reason_code="observation.transfer_unavailable",
                    detail="The frozen observation could not be encoded completely; retry observation.",
                )
            )

    return ObservationTransferResponse(
        records(), headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    )


def observation_openapi(payload: str) -> dict[str, object]:
    """Canonical route metadata consumed by generated readers."""
    return {
        "x-vonk-streaming-transport": True,
        "x-vonk-response-record-max-bytes": MAX_CONTROL_DOCUMENT_BYTES,
        "x-vonk-observation-payload": {"$ref": f"#/components/schemas/{payload}"},
    }
