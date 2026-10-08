"""Shared ORM metadata and database constraint expressions."""

from __future__ import annotations

from sqlalchemy import (
    Integer,
)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql.functions import FunctionElement
from vonk_agent_protocol import (
    LifecycleState,
    LifecycleSubject,
    check_words,
)


class Base(DeclarativeBase):
    pass


class _Utf8ByteLength(FunctionElement[int]):
    type = Integer()
    inherit_cache = True


@compiles(_Utf8ByteLength, "sqlite")
def _compile_sqlite_utf8_byte_length(element, compiler, **kwargs) -> str:
    value = compiler.process(next(iter(element.clauses)), **kwargs)
    return f"length(CAST({value} AS BLOB))"


@compiles(_Utf8ByteLength)
def _compile_utf8_byte_length(element, compiler, **kwargs) -> str:
    value = compiler.process(next(iter(element.clauses)), **kwargs)
    return f"octet_length(CAST({value} AS TEXT))"


def _state_in(
    subject: LifecycleSubject,
    *states: LifecycleState,
    extra: tuple[str, ...] = (),
    nullable: bool = False,
) -> str:
    """The CHECK of a lifecycle subject's ``state``: its words, and the old spellings.

    Generated from the contract, so the words the column admits cannot drift from
    the ones the rest of the Controller speaks.  An old spelling stays admitted for
    one release so a row written before the rename is still valid; ``extra`` names
    words the kind itself still carries (an artifact job's old ``draft`` and
    ``ready``) and ``nullable`` admits a row that has no lifecycle state yet.
    """

    words = ",".join(f"'{word}'" for word in (*check_words(subject, states), *extra))
    check = f"state IN ({words})"
    return f"state IS NULL OR {check}" if nullable else check


def _lower_hex(column: str, length: int) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column}) = {length} AND {column} = lower({column}) AND "
        f"length({remainder}) = 0"
    )


def _prefixed_digest(column: str) -> str:
    """Require a lowercase sha256 digest with its explicit algorithm prefix."""

    return (
        f"length({column}) = 71 AND substr({column}, 1, 7) = 'sha256:' "
        f"AND ({_lower_hex(f'substr({column}, 8)', 64)})"
    )


def _nullable_lower_hex(column: str, length: int) -> str:
    return f"{column} IS NULL OR ({_lower_hex(column, length)})"


def _uuid_shape(column: str) -> str:
    compact = f"replace({column}, '-', '')"
    return (
        f"length({column}) = 36 AND substr({column}, 9, 1) = '-' AND "
        f"substr({column}, 14, 1) = '-' AND substr({column}, 19, 1) = '-' AND "
        f"substr({column}, 24, 1) = '-' AND ({_lower_hex(compact, 32)})"
    )
