"""Declarations a route attaches for the contract graph to read."""

from __future__ import annotations

from pydantic import BaseModel


def raw_json_body(model: type[BaseModel]):
    """Bind a bounded raw reader to its existing canonical request model."""

    def decorate(endpoint):
        endpoint.__vonk_raw_json_model__ = model
        return endpoint

    return decorate
