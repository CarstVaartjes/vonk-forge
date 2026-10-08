"""Faults in stored audit inputs cannot publish success or poison fresh reads."""

from vonk_agent_protocol import UnknownOutcomeError

from .source_observation import observe


def test_observation_exhausts_then_fresh_request_recovers():
    attempts = []
    repaired = False

    def read():
        attempts.append(None)
        if not repaired:
            raise FileNotFoundError("fixture")
        return (1, 2)

    published = None
    try:
        published = observe(read)
    except UnknownOutcomeError:
        pass
    assert published is None and len(attempts) == 3
    repaired = True
    assert observe(read) == (1, 2)
    assert len(attempts) == 4


def test_registry_read_recovers_without_empty_inventory(tmp_path):
    from .blocker_boundaries import ALLOWLIST_PATH, load_allowlist

    stored = tmp_path / "registry.json"
    stored.write_text("{")
    published = None
    try:
        published = load_allowlist(stored)
    except UnknownOutcomeError:
        pass
    assert published is None
    stored.write_bytes(ALLOWLIST_PATH.read_bytes())
    assert load_allowlist(stored)["fail_closed"]
    assert load_allowlist(stored)["fail_closed"]
