"""One receipt algorithm with explicit source-owned schema validation.

Normal CLI callers use their bundled canonical contract. Historical acceptance
callers must select the contract from the verified release, never by trying a
different decoder after a response fails. Record allocation is not a total RAM
or observation-size promise.
"""

import base64
import hashlib
import io
import json
import math
import tempfile
import time
from collections.abc import Callable
from typing import Protocol


class ObservationStream(Protocol):
    def read(self, amount: int, /) -> bytes: ...


class ObservationTransferInvalid(ValueError):
    """A safe, observed transfer-phase failure with no payload values."""


class ObservationTransferUnavailable(ValueError):
    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}")


def _unique_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ObservationTransferInvalid("duplicate observation JSON member")
        result[key] = value
    return result


def check_observation_deadline(deadline: float) -> None:
    if not math.isfinite(deadline):
        raise ObservationTransferInvalid("observation attempt deadline is invalid")
    if time.monotonic() >= deadline:
        raise TimeoutError("observation attempt deadline elapsed")


def receive_observation[Receipt](
    stream: ObservationStream,
    *,
    resource: str,
    record_max_bytes: int,
    deadline: float,
    validate_record: Callable[[object], None],
    validate_payload: Callable[[object], Receipt],
) -> Receipt:
    """Return the original full payload only after verified receipt and EOF.

    Callers validate status/media against their explicit source contract before
    this function. Every record is validated through that contract, including
    canonical UUID/base64/numeric/presence rules. No parallel field schema lives
    here: checks below own transfer ordering and observed completeness only.
    The caller supplies one finite monotonic deadline captured before opening
    the response. Its native transport must interrupt blocking I/O at that same
    deadline; these checkpoints additionally refuse late buffered records and
    late validation results. They do not preempt synchronous parsing or claim
    a process memory bound.
    """
    check_observation_deadline(deadline)
    if type(record_max_bytes) is not int or record_max_bytes < 1:
        raise ObservationTransferInvalid("observation reader allocation is invalid")
    transfer_id: str | None = None
    ordinal = 0
    byte_count = 0
    digest = hashlib.sha256()
    complete = False
    pending = bytearray()
    with tempfile.TemporaryFile(mode="w+b") as spool:
        while True:
            check_observation_deadline(deadline)
            if len(pending) >= record_max_bytes:
                raise ObservationTransferInvalid(
                    "observation record exceeds reader allocation"
                )
            incoming = stream.read(min(65536, record_max_bytes - len(pending)))
            check_observation_deadline(deadline)
            if not incoming:
                break
            if len(incoming) > record_max_bytes - len(pending):
                raise ObservationTransferInvalid(
                    "observation read exceeds reader allocation"
                )
            pending.extend(incoming)
            while b"\n" in pending:
                check_observation_deadline(deadline)
                line, remainder = pending.split(b"\n", 1)
                pending = bytearray(remainder)
                if complete:
                    raise ObservationTransferInvalid(
                        "observation data follows final receipt"
                    )
                record = json.loads(
                    line.decode("utf-8", errors="strict"),
                    object_pairs_hook=_unique_members,
                )
                validate_record(record)
                check_observation_deadline(deadline)
                if not isinstance(record, dict):
                    raise ObservationTransferInvalid(
                        "observation record is not an object"
                    )
                kind = record.get("type")
                identifier = record.get("transfer_id")
                if transfer_id is None:
                    if (
                        kind != "start"
                        or record.get("resource") != resource
                        or not isinstance(identifier, str)
                    ):
                        raise ObservationTransferInvalid(
                            "observation start differs from requested resource"
                        )
                    transfer_id = identifier
                    continue
                if identifier != transfer_id:
                    raise ObservationTransferInvalid(
                        "observation identity changed during transfer"
                    )
                if kind == "chunk":
                    if record.get("ordinal") != ordinal:
                        raise ObservationTransferInvalid(
                            "observation fragments are not contiguous"
                        )
                    data = record.get("data")
                    if not isinstance(data, str):
                        raise ObservationTransferInvalid(
                            "observation fragment data is unreadable"
                        )
                    raw = base64.b64decode(data, validate=True)
                    spool.write(raw)
                    digest.update(raw)
                    byte_count += len(raw)
                    ordinal += 1
                elif kind == "complete":
                    if (
                        record.get("chunks") != ordinal
                        or ordinal == 0
                        or record.get("bytes") != byte_count
                        or record.get("sha256") != digest.hexdigest()
                    ):
                        raise ObservationTransferInvalid(
                            "observation completeness receipt differs"
                        )
                    complete = True
                elif kind == "error":
                    detail = record.get("detail")
                    reason = record.get("reason_code")
                    if not isinstance(detail, str) or not isinstance(reason, str):
                        raise ObservationTransferInvalid(
                            "observation error explanation is unreadable"
                        )
                    raise ObservationTransferUnavailable(reason, detail)
                else:
                    raise ObservationTransferInvalid(
                        "unexpected observation transfer phase"
                    )
        if pending or not complete:
            raise ObservationTransferInvalid(
                "observation ended without a complete final receipt"
            )
        check_observation_deadline(deadline)
        spool.seek(0)
        with io.TextIOWrapper(spool, encoding="utf-8", errors="strict") as document:
            decoded = json.load(document, object_pairs_hook=_unique_members)
            check_observation_deadline(deadline)
            result = validate_payload(decoded)
            check_observation_deadline(deadline)
            return result
