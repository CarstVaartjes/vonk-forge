"""``vonkctl auth login`` against the real app: password in, token files out."""

from __future__ import annotations

import json
import stat
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from vonk_control.api.application import create_app
from vonk_control.auth import TokenCodec
from vonk_control.fleet_projection.service import FleetProjection
from vonk_control.jobs.queue import JobService
from vonk_control.model_primitives import Base
from vonk_control.models.fleet import User
from vonk_control.security.browser_auth import BrowserAuthService
from vonk_control.security.passwords import hash_password

from .test_profile_load_installed_cli import _https_api_peer, _process_environment

pytest_plugins = ("tests.test_profile_load_installed_cli",)

_PASSWORD = "correct horse battery staple"
_KEY = b"auth-login-installed-cli-signing-key"


def _run(
    executable: Path,
    arguments: tuple[str, ...],
    environment: dict[str, str],
    stdin: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(executable), *arguments],
        env=environment,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )


@pytest.mark.lane
def test_installed_login_keeps_only_a_private_token_that_later_commands_use(
    installed_vonkctl: Path, postgres_engine, tmp_path: Path
) -> None:
    """A wrong password leaves nothing; the right one connects with the files alone."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as db:
        db.add(
            User(
                subject="admin",
                role="administrator",
                disabled_at=None,
                password_verifier=hash_password(_PASSWORD),
            )
        )

    def now():
        return datetime.now(UTC)

    api = TestClient(
        create_app(
            jobs=JobService(sessions, clock=now),
            tokens=TokenCodec(_KEY),
            fleet_projection=FleetProjection(sessions, clock=now),
            browser_auth=BrowserAuthService(
                sessions,
                token_signing_key=_KEY,
                clock=now,
            ),
            now=lambda: int(time.time()),
        )
    )
    minted: list[str] = []
    forward = api.request

    def spy(method, url, *args, **kwargs):
        response = forward(method, url, *args, **kwargs)
        if str(url).endswith("/api/auth/cli-token"):
            minted.append(response.headers["X-Vonk-Token-Expires-At"])
        return response

    api.request = spy
    with _https_api_peer(tmp_path, api, {}) as (url, certificate, _peer):
        environment = _process_environment(
            tmp_path, url, certificate, {"Authorization": "Bearer unused"}
        )
        environment.pop("VONK_CONTROL_URL")
        environment.pop("VONK_CONTROL_TOKEN_FILE")
        config = Path(environment["XDG_CONFIG_HOME"]) / "vonkctl"

        wrong = _run(
            installed_vonkctl,
            ("auth", "login", url, "--password-stdin"),
            environment,
            "not the password\n",
        )
        assert wrong.returncode == 2
        assert not config.exists()
        assert "not the password" not in wrong.stdout + wrong.stderr

        # A corrupt stored connection never blocks the login that rewrites it.
        config.mkdir(mode=0o700, parents=True)
        (config / "connection.json").write_text("{not json")
        login = _run(
            installed_vonkctl,
            ("auth", "login", url, "--password-stdin"),
            environment,
            _PASSWORD + "\n",
        )
        connected = _run(installed_vonkctl, ("--check-connection",), environment, "")
        status = _run(installed_vonkctl, ("auth", "status"), environment, "")
        status_json = _run(
            installed_vonkctl, ("auth", "status", "--json"), environment, ""
        )

    assert login.returncode == 0, login.stdout + login.stderr
    token = (config / "token").read_text().strip()
    assert stat.S_IMODE(config.stat().st_mode) == 0o700
    for name in ("token", "connection.json"):
        assert stat.S_IMODE((config / name).stat().st_mode) == 0o600
    stored = json.loads((config / "connection.json").read_text())
    assert stored["controller_url"] == url
    assert connected.returncode == 0, connected.stdout + connected.stderr
    assert status.returncode == 0, status.stdout + status.stderr
    assert len(minted) == 1
    assert stored["token_expires_at"] == minted[0]
    assert minted[0] in status.stdout
    assert json.loads(status_json.stdout)["token_expires_at"] == minted[0]
    assert "in 30 days" in login.stdout
    for output in (login, connected, status):
        assert token not in output.stdout + output.stderr
        assert _PASSWORD not in output.stdout + output.stderr
