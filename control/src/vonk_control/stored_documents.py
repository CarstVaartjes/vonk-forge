"""Contract models for stored JSON documents that have no other home.

Most JSON columns store a document owned by an existing contract module (a plan,
a progress tree, a wire payload); :mod:`vonk_control.stored_columns` binds those
where they are defined.  A document that exists only as bookkeeping in a column
is defined here, with its columns named in its docstring.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field

from .strict_json import StrictJSONModel


class _StoredDocument(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class RouteClaimMarker(_StoredDocument):
    """The marker of the one route publication claim row.

    ``route_publications.activation_marker`` holds an activation marker for a
    published or maintenance generation, and this ordinal for the claim row that
    orders concurrent publication attempts.
    """

    claim_ordinal: int = Field(ge=1)


__all__ = ["RouteClaimMarker"]
