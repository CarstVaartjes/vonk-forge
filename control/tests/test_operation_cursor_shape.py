"""An operations cursor whose boundary is unreadable is an invalid cursor."""

from __future__ import annotations

import pytest
from vonk_control.auth import CursorError, TokenCodec
from vonk_control.operation_api.providers import merge_operation_providers


@pytest.mark.parametrize(
    "boundary", [["only-one"], "bare", [1, 2], ["not-a-date", "id"]]
)
def test_unreadable_boundary_is_an_invalid_cursor(boundary: object) -> None:
    codec = TokenCodec(b"s" * 32).cursor_codec()
    context = {"state": None, "node_id": None, "request_id": None}
    cursor = codec.encode(
        resource="operations",
        order="created-at-desc/id-desc/v1",
        context=context,
        boundary=boundary,
    )
    with pytest.raises(CursorError):
        merge_operation_providers(
            [], cursor=cursor, limit=10, state=None, node_id=None, cursors=codec
        )
