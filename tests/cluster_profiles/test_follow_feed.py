"""The follow view shows the Controller's own meter, per phase, at the right cadence."""

from cluster_profiles.cli_render.follow import PLAIN_INTERVAL_SECONDS, FollowFeed
from cluster_profiles.cli_render.progress import live_lines, progress_line

GIB = 1 << 30


def _application(completed: int, phase: str = "transfer") -> dict[str, object]:
    return {
        "state": "running",
        "blockers": [
            {"code": "observation-unavailable", "detail": "x", "node_ids": []}
        ],
        "progress": {
            "current_label": "Switch profile",
            "child_progress": {
                "operation": {
                    "phase": phase,
                    "completed_bytes": completed,
                    "total_bytes": 8 * GIB,
                    "total_bytes_known": True,
                    "smoothed_bytes_per_second": GIB,
                    "eta_seconds": 360,
                    "members": [
                        {
                            "member_id": "spk_a",
                            "phase": phase,
                            "completed_bytes": completed // 2,
                            "total_bytes": 4 * GIB,
                            "smoothed_bytes_per_second": GIB // 2,
                            "eta_seconds": 120,
                        },
                        {
                            "member_id": "spk_b",
                            "phase": phase,
                            "completed_bytes": completed // 2,
                            "total_bytes": 4 * GIB,
                        },
                    ],
                }
            },
        },
    }


class _Out:
    """A stream that remembers each write; a plain line is one non-newline write."""

    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    @property
    def lines(self) -> list[str]:
        return [item for item in self.writes if item != "\n"]


def _feed(tty: bool, clock) -> tuple[FollowFeed, _Out]:
    out = _Out()
    return FollowFeed(out, tty=tty, clean=str, clock=clock), out  # type: ignore[arg-type]


def test_view_carries_the_servers_numbers_and_a_known_reason():
    application = _application(4 * GIB)
    line = progress_line(application)
    assert all(part in line for part in ("4.0 GiB", "8.0 GiB", "50%", "1.0 GiB/s"))
    assert "observation-unavailable" not in line
    bars = live_lines(application)
    assert len(bars) == 3
    assert "512.0 MiB/s" in bars[1] and "/s" not in bars[2]


def test_plain_cadence_is_phase_change_or_thirty_seconds():
    now = [0.0]
    feed, out = _feed(False, lambda: now[0])
    feed.show(_application(GIB))
    now[0] = PLAIN_INTERVAL_SECONDS - 1
    feed.show(_application(2 * GIB))
    assert len(out.lines) == 1
    feed.show(_application(2 * GIB, phase="start"))
    assert len(out.lines) == 2
    now[0] = 2 * PLAIN_INTERVAL_SECONDS
    feed.show(_application(3 * GIB, phase="start"))
    assert len(out.lines) == 3


def test_terminal_redraws_in_place_and_starts_a_block_per_phase():
    feed, out = _feed(True, lambda: 0.0)
    feed.show(_application(2 * GIB))
    feed.show(_application(4 * GIB))
    feed.show(_application(4 * GIB, phase="start"))
    assert out.writes.count("\x1b[3F") == 1
