"""Exact relational projections of canonical nonnegative JSON integers.

These are decimal tokens, not SQL arithmetic operands. SQLite numeric affinity
and PostgreSQL BIGINT cannot represent the canonical metadata domain. Python
callers continue to consume integers; only the relational encoding changes.
"""

from __future__ import annotations

import re

from sqlalchemy import Boolean, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.types import TypeDecorator

_TOKEN = re.compile(r"(?:0|[1-9][0-9]*)", re.ASCII)


def decode_decimal_integer(value: object) -> int:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise ValueError("stored integer must be a canonical nonnegative decimal token")
    return int(value)


class ExactNonnegativeInteger(TypeDecorator[int]):
    """One current TEXT encoding; malformed stored values never become zero."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: int | None, dialect: Dialect) -> str | None:
        if value is None:
            return None  # The owning column alone decides nullability.
        if type(value) is not int or value < 0:
            raise ValueError("integer projection requires a nonnegative integer")
        return str(value)

    def process_result_value(self, value: object, dialect: Dialect) -> int | None:
        if value is None:
            return None
        return decode_decimal_integer(value)


class DecimalIntegerToken(FunctionElement[bool]):
    type = Boolean()
    inherit_cache = True


class DecimalIntegerAtMost(FunctionElement[bool]):
    type = Boolean()
    inherit_cache = True


class DecimalIntegerOrderKey(FunctionElement[str]):
    type = Text()
    inherit_cache = True


def _ordered(value: str, dialect: str) -> str:
    collation = '"C"' if dialect == "postgresql" else "BINARY"
    return f"(CAST({value} AS TEXT) COLLATE {collation})"


@compiles(DecimalIntegerOrderKey)
def _compile_order_key(element, compiler, **kwargs) -> str:
    value = compiler.process(next(iter(element.clauses)), **kwargs)
    return _ordered(value, compiler.dialect.name)


@compiles(DecimalIntegerToken)
def _compile_token(element, compiler, **kwargs) -> str:
    value = compiler.process(next(iter(element.clauses)), **kwargs)
    ordered = _ordered(value, compiler.dialect.name)
    remaining = ordered
    for digit in "0123456789":
        remaining = f"replace({remaining}, '{digit}', '')"
    return (
        f"({ordered} = '0' OR "
        f"(substr({ordered}, 1, 1) BETWEEN '1' AND '9' AND {remaining} = ''))"
    )


@compiles(DecimalIntegerAtMost)
def _compile_at_most(element, compiler, **kwargs) -> str:
    left, right = (
        _ordered(compiler.process(value, **kwargs), compiler.dialect.name)
        for value in element.clauses
    )
    return (
        f"(length({left}) < length({right}) OR "
        f"(length({left}) = length({right}) AND {left} <= {right}))"
    )
