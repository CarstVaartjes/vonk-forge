"""The public removal review selects fleet scope and returns a durable owner."""

from pathlib import Path
from uuid import uuid4

from vonk_agent_protocol import LifecycleState

from .test_model_cache import cache as cache_fixture
from .test_model_cache_cancel_recovery import _api
from .test_model_removal_worker_postgres import _prepare_removal_target

cache = cache_fixture


def test_fleet_review_and_receipt_are_owned_by_the_same_durable_removal(
    cache, tmp_path: Path
) -> None:
    """Fails on NAS-only removal: the scope flag must reach Controller effects,
    and acceptance must return the same owner after a lost response.
    """
    service, sessions = cache
    try:
        selector, _, path = _prepare_removal_target(
            service, sessions, tmp_path, slug="fleet-api", data=b"finished"
        )
        with _api(service) as client:
            review = client.get(
                f"/api/model/{selector}/remove-review", params={"fleet": True}
            )
            assert review.status_code == 200
            effects = review.json()["fleet"]
            assert effects is not None and effects["nas_remove"] is True
            assert path.read_bytes() == b"finished"
            key = str(uuid4())
            body = {
                "request_key": key,
                "fleet": True,
                "review_digest": review.json()["review_digest"],
            }
            accepted = client.post(f"/api/model/{selector}/remove", json=body)
            assert accepted.status_code == 202, accepted.text
            receipt = accepted.json()
            assert receipt["fleet"]["effects"] == effects
            assert receipt["review_digest"] == review.json()["review_digest"]
            assert receipt["state"] == LifecycleState.QUEUED
            lookup = client.get(f"/api/model/requests/{key}")
            assert lookup.status_code == 200
            assert lookup.json()["operation_id"] == receipt["operation_id"]
            replay = client.post(f"/api/model/{selector}/remove", json=body)
            assert replay.status_code == 202
            assert replay.json()["operation_id"] == receipt["operation_id"]
    finally:
        service.close()
