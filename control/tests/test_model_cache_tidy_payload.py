from __future__ import annotations

from types import SimpleNamespace

from vonk_control.lifecycle.model_cache import ModelCacheAdapter
from vonk_control.lifecycle.types import State
from vonk_control.model_cache.contract import (
    ModelCacheRemovalPayload,
    ModelCacheRetry,
)
from vonk_control.strict_json import serialize_json_value


def test_an_unchanged_payload_is_not_rewritten() -> None:
    payload = ModelCacheRemovalPayload(
        schema_version=2,
        source_policy="nas-first",
        operator_action="remove-model",
        selector="model",
        review_digest=None,
        removal_fence="00000000-0000-4000-8000-000000000001",
        selected=[],
        selected_objects=[],
        delete_objects=[],
        object_index=0,
        reclaimed_bytes=0,
        set_index=0,
        retry=ModelCacheRetry(automatic_attempts=1, operator_retries=0),
        result=None,
    )
    stored: list[object] = []
    adapter = ModelCacheAdapter(
        None,  # type: ignore[arg-type]
        effects=None,  # type: ignore[arg-type]
        read_payload=lambda _operation: payload,
        store_payload=lambda _operation, value: stored.append(value),
    )
    operation = SimpleNamespace(payload=serialize_json_value(payload))
    after = SimpleNamespace(state=State.RUNNING, retry_count=0)

    assert adapter._tidy_payload(operation, after, spent=False) is False  # type: ignore[arg-type]
    assert stored == []
