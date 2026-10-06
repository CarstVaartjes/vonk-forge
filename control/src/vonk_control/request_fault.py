"""The caller's request is invalid, as opposed to the server's state being bad.

The projection surfaces used to answer any ``ValueError`` with 422, which also
caught failures that are the Controller's own fault -- a stored catalog
revision that no longer validates, for instance. Marking request faults
explicitly lets those routes keep 422 for a rejected request and answer 503 for
unreadable stored state.
"""

from __future__ import annotations

from vonk_agent_protocol import InvalidRequestError


class RequestFault(InvalidRequestError, ValueError):
    """A request the caller can correct: a bad selector, limit, sort or filter."""
