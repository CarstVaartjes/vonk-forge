from __future__ import annotations

import threading
import time

from vonk_control.availability_production import RecipeImageAvailabilityScheduler


class _Claim:
    pass


class _Service:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.claimed = 0

    def claim_pending(self, *, limit: int, owner_id: str):
        del owner_id
        if self.claimed:
            return ()
        self.claimed += 1
        return (_Claim(),)[:limit]

    def run_claim(self, claim: _Claim) -> None:
        del claim
        self.started.set()
        self.release.wait(5)


def test_scheduler_submits_durable_claim_without_waiting_for_image_io() -> None:
    service = _Service()
    scheduler = RecipeImageAvailabilityScheduler(service, max_workers=1)
    started = time.monotonic()
    assert scheduler.tick() == 1
    elapsed = time.monotonic() - started
    assert elapsed < 0.5
    assert service.started.wait(1)
    service.release.set()
    scheduler.close()
    assert scheduler.executor._shutdown is True


def test_scheduler_close_is_idempotent_and_stops_new_claims() -> None:
    service = _Service()
    scheduler = RecipeImageAvailabilityScheduler(service, max_workers=1)
    scheduler.close()
    scheduler.close()
    assert scheduler.tick() == 0
