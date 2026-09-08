"""OpenAPI declarations for exact-byte downloads, not JSON reconstruction."""

from typing import Any


def download_responses(media_type: str, *, partial: bool = False) -> dict[int, Any]:
    """Describe both complete and range responses without a phantom JSON body."""
    content = {media_type: {"schema": {"type": "string", "format": "binary"}}}
    responses = {200: {"description": "Exact file bytes", "content": content}}
    if partial:
        responses[206] = {"description": "Selected byte range", "content": content}
    return responses


def upload_request_body(media_type: str) -> dict[str, Any]:
    """Declare a bounded raw upload without introducing a JSON parser."""
    return {
        "requestBody": {
            "required": True,
            "content": {
                media_type: {"schema": {"type": "string", "format": "binary"}},
            },
        }
    }
