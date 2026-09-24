"""A new installed process can recover cancellation and explain its actual effects."""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

from sqlalchemy import select
from vonk_control.auth import TokenCodec
from vonk_control.fleet_profiles import build_production_fleet_profile_service
from vonk_control.models import Job, ResourceReservation
from vonk_control.operation_api import durable_operation_services

from .test_fleet_profile_api import _client, _headers
from .test_fleet_profile_cancel import _application
from .test_profile_load_installed_cli import _https_api_peer, _process_environment

pytest_plugins = ("tests.test_profile_load_installed_cli",)


def test_installed_profile_cancel_recovery_is_visible_in_activity(
    installed_vonkctl: Path, postgres_engine, tmp_path: Path
) -> None:
    sessions, _switch, service, profile, application, _adapter, _nodes = _application(
        tmp_path, engine=postgres_engine
    )
    operations = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=service._clock,
        cursors=TokenCodec(b"a" * 32).cursor_codec(),
        operation_providers=(service.operation_provider(),),
    )
    api, tokens = _client(sessions, profiles=service, operations=operations)
    headers = _headers(tokens, "administrator")
    cancel_key = str(uuid4())
    path = f"/api/profile/applications/{application.id}/cancel"
    with _https_api_peer(tmp_path, api, headers) as (url, certificate, peer):
        environment = _process_environment(tmp_path, url, certificate, headers)
        peer.drop_responses.add(("POST", path))

        def run(*args: str):
            return subprocess.run(
                [str(installed_vonkctl), *args],
                env=environment,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )

        cancelled = run(
            "--profile",
            str(profile.number),
            "profile",
            "cancel",
            application.id,
            "--yes",
            "--request-key",
            cancel_key,
            "--detach",
            "--json",
        )
        assert cancelled.returncode == 0, cancelled.stdout + cancelled.stderr
        receipt = json.loads(cancelled.stdout)
        assert receipt["cancellation"]["request_key"] == cancel_key
        assert receipt["cancellation"]["state"] == "cancelling"
        assert peer.dropped_responses == [("POST", path)]
        assert [(method, endpoint) for method, endpoint, _ in peer.calls] == [
            ("POST", path),
            (
                "GET",
                f"/api/profile/applications/{application.id}/cancellations/{cancel_key}",
            ),
        ]

        # This crosses the generated response-model boundary before the human
        # renderer. Dropping the canonical cancellation field loses these details.
        activity = run(
            "fleet",
            "activity",
            "--state",
            "cancelling",
            "--request-id",
            application.request_key,
        )
        assert activity.returncode == 0, activity.stdout + activity.stderr
        assert "cancelling" in activity.stdout
        assert application.id in activity.stdout
        assert cancel_key in activity.stdout
        assert "Cancelled or unissued effect" in activity.stdout
        assert "administrator" in activity.stdout

        assert service.tick()
        terminal = run(
            "fleet",
            "activity",
            "--state",
            "cancelled",
            "--request-id",
            application.request_key,
            "--json",
        )
        assert terminal.returncode == 0, terminal.stdout + terminal.stderr
        result = json.loads(terminal.stdout)
        assert len(result["operations"]) == 1
        operation = result["operations"][0]
        assert operation["id"] == application.id
        assert operation["cancellation"]["state"] == "cancelled"
        assert operation["cancellation"]["request_key"] == cancel_key
        assert (
            sum(
                method == "POST" and endpoint == path
                for method, endpoint, _ in peer.calls
            )
            == 1
        )


def test_installed_cancel_reports_issued_child_until_late_receipt_is_reconciled(
    installed_vonkctl: Path, postgres_engine, tmp_path: Path, monkeypatch
) -> None:
    sessions, switch, service, profile, application, adapter, _nodes = _application(
        tmp_path, engine=postgres_engine
    )
    api, tokens = _client(sessions, profiles=service)
    headers = _headers(tokens, "administrator")
    started, release = Event(), Event()
    original_start = adapter.start

    def delayed_start(**kwargs):
        child = original_start(**kwargs)
        started.set()
        if not release.wait(timeout=45):
            raise TimeoutError("test did not release the committed child response")
        return child

    monkeypatch.setattr(adapter, "start", delayed_start)
    cancel_key = str(uuid4())
    with _https_api_peer(tmp_path, api, headers) as (url, certificate, _peer):
        environment = _process_environment(tmp_path, url, certificate, headers)

        def run(*args: str):
            return subprocess.run(
                [str(installed_vonkctl), *args],
                env=environment,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(service.tick)
            try:
                assert started.wait(timeout=10)
                progress = service.application(application.id).progress.switch_adapter
                assert progress is not None and progress.active_operation_id is not None
                child_id = progress.active_operation_id
                result = run(
                    "--profile",
                    str(profile.number),
                    "profile",
                    "cancel",
                    application.id,
                    "--yes",
                    "--request-key",
                    cancel_key,
                    "--detach",
                    "--json",
                )
                assert result.returncode == 0, result.stdout + result.stderr
                cancellation = json.loads(result.stdout)["cancellation"]
                assert cancellation["state"] == "cancelling"
                assert cancellation["dependency"] == child_id
                assert any(
                    effect["operation_id"] == child_id
                    and effect["outcome"] == "pending"
                    for effect in cancellation["pending_effects"]
                )
                observed = run(
                    "profile", "progress", "--application", application.id, "--json"
                )
                assert observed.returncode == 0, observed.stdout + observed.stderr
                pending = json.loads(observed.stdout)
                assert pending["id"] == application.id
                assert pending["cancellation"]["state"] == "cancelling"
                assert pending["cancellation"]["request_key"] == cancel_key
                with sessions() as session:
                    claims = tuple(
                        session.scalars(
                            select(ResourceReservation).where(
                                ResourceReservation.owner_kind == "fleet-profile",
                                ResourceReservation.owner_id == application.id,
                            )
                        )
                    )
                    assert claims
                    assert all(
                        claim.state in {"active", "promised"} for claim in claims
                    )
            finally:
                release.set()
            assert worker.result(timeout=10)

        # Reconstruct the actual durable owner after the old worker returns its
        # stale running view. No cancellation or child-state method is stubbed.
        restarted = build_production_fleet_profile_service(
            sessions, clock=service._clock, run_switch_operations=switch
        )
        assert restarted.tick()
        settled = run("profile", "progress", "--application", application.id, "--json")
        assert settled.returncode == 0, settled.stdout + settled.stderr
        terminal = json.loads(settled.stdout)
        assert terminal["state"] == "cancelled"
        cancellation = terminal["cancellation"]
        assert cancellation["request_key"] == cancel_key
        assert not cancellation["pending_effects"]
        assert any(
            effect["operation_id"] == child_id and effect["outcome"] == "cancelled"
            for effect in cancellation["cancelled_effects"]
        )
        with sessions() as session:
            children = tuple(
                session.scalars(select(Job).where(Job.kind == "recipe.run-switch.v2"))
            )
            assert len(children) == 1 and children[0].id == child_id
            assert children[0].state == "cancelled"
