"""Portable cache-index recovery without catalog or container prerequisites."""

from vonk_control.library_image_presence import ImagePresenceIndex


def test_unreadable_image_recovers_after_refresh_without_reprobing_healthy_image():
    clock = [0.0]
    faulty = [True]
    calls = []

    def probe(archive: str, size: int) -> bool:
        calls.append(archive)
        if archive == "damaged" and faulty[0]:
            raise PermissionError("denied")
        return True

    index = ImagePresenceIndex(
        probe, present_ttl_seconds=60, absent_ttl_seconds=5, clock=lambda: clock[0]
    )
    wanted = {("damaged", 1), ("fine", 2)}
    answers = index.lookup(wanted, budget_seconds=2)
    assert answers[("damaged", 1)].state == "unreadable"
    assert answers[("fine", 2)].state == "present"
    faulty[0] = False
    clock[0] = 6
    recovered = index.lookup(wanted, budget_seconds=2)
    assert all(answer.state == "present" for answer in recovered.values())
    assert calls.count("fine") == 1
    assert calls.count("damaged") == 2
