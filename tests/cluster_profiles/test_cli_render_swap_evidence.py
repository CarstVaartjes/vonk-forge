"""vonkctl prints the Controller's swap numbers on a wait and on a loading rank."""

from __future__ import annotations

from cluster_profiles.cli_render import render_payload
from cluster_profiles.cli_render.common import _reasons
from cluster_profiles.cli_render.fleet import _node

GIB = 1 << 30

SWAP = {
    "active_bytes": 16 * GIB,
    "desired_bytes": 64 * GIB,
    "used_bytes": 2 * GIB,
    "held_bytes": 0,
    "usable_bytes": 14 * GIB,
    "rise_bytes": 35 * GIB,
    "needed_setting_bytes": 40 * GIB,
}

BLOCKER = {
    "code": "run-switch.resource.insufficient_swap",
    "detail": "The load waits for swap.",
    "severity": "error",
    "node_ids": ["spark-1"],
}

LINE = (
    "Swap: 16.0 GiB active (setting 64.0 GiB), 2.0 GiB in use, 0 B held by loading "
    "runs; usable 14.0 GiB; this load rises 35.0 GiB; "
    "needs a setting of at least 40.0 GiB"
)


def _application(blocker):
    return {
        "id": "11111111-1111-4111-8111-111111111111",
        "state": "running",
        "blockers": [blocker],
        "progress": {"completed_steps": 0, "total_steps": 1},
    }


def test_a_wait_prints_its_swap_numbers_and_nothing_extra_without_them(capsys) -> None:
    render_payload(
        _application({**BLOCKER, "swap": SWAP}), "profile", action="progress"
    )
    assert LINE in capsys.readouterr().out.splitlines()

    render_payload(_application(BLOCKER), "profile", action="progress")
    assert "Swap:" not in capsys.readouterr().out


def test_an_unknown_setting_prints_setting_unknown(capsys) -> None:
    swap = {**SWAP, "desired_bytes": None}
    render_payload(
        _application({**BLOCKER, "swap": swap}), "profile", action="progress"
    )
    assert "(setting unknown)" in capsys.readouterr().out


def test_a_review_reason_prints_its_swap_numbers(capsys) -> None:
    reasons = [
        {
            "code": "run-switch.resource.insufficient_swap",
            "detail": "The load waits for swap.",
            "severity": "blocker",
            "scope": "node",
            "swap": SWAP,
        }
    ]
    _reasons(reasons)
    assert LINE in capsys.readouterr().err.splitlines()

    _reasons([{k: v for k, v in reasons[0].items() if k != "swap"}])
    assert "Swap:" not in capsys.readouterr().err


def _loaded(**extra):
    return {
        "run_id": "run-1",
        "installation_id": "inst-1",
        "recipe_id": "r",
        "recipe_revision_id": "rr",
        "title": "GLM",
        "alias": "glm",
        "expected_rank_count": 1,
        "present_ranks": [0],
        "member_node_ids": ["spark-1"],
        "rank": 0,
        "role": "worker",
        "run_state": "starting",
        "route_state": "pending",
        "rank_state": "starting",
        "rank_age_seconds": 1.0,
        "rank_fresh": True,
        "group_state": "healthy",
        "healthy": True,
        **extra,
    }


def _fleet_node(run):
    return {
        "id": "n1",
        "display_name": "spark-1",
        "connection": {"online_state": "online", "last_seen_at": None},
        "loaded": [run],
    }


def test_a_loading_rank_prints_the_swap_it_holds(capsys) -> None:
    _node(_fleet_node(_loaded(load_swap_held_bytes=35 * GIB)), detail=False)
    assert "Swap held while loading: 35.0 GiB" in capsys.readouterr().out

    _node(_fleet_node(_loaded()), detail=False)
    assert "Swap held while loading" not in capsys.readouterr().out
