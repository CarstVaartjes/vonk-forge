from __future__ import annotations

import logging
from pathlib import Path

import pytest
import vonk_control
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount
from vonk_control.api import production
from vonk_control.api.common import _LOGGER


def _admin_web_mounts(app: FastAPI) -> list[Mount]:
    return [
        route
        for route in app.routes
        if isinstance(route, Mount) and route.name == "admin-web"
    ]


def _fake_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    package = tmp_path / "site-packages" / "vonk_control"
    (package / "api").mkdir(parents=True)
    monkeypatch.setattr(vonk_control, "__file__", str(package / "__init__.py"))
    return package


def test_admin_web_root_is_the_package_root_not_the_api_module_directory() -> None:
    package = Path(vonk_control.__file__).resolve().parent

    assert production.admin_web_root() == package / "web"


def test_admin_web_serves_index_and_deep_links_from_the_package_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = _fake_package(tmp_path, monkeypatch) / "web"
    web.mkdir()
    (web / "index.html").write_text("<h1>Admin</h1>", encoding="utf-8")
    app = FastAPI()

    production.mount_admin_web(app)

    assert _admin_web_mounts(app)
    client = TestClient(app)
    assert client.get("/").text == "<h1>Admin</h1>"
    assert client.get("/profiles/some-profile").text == "<h1>Admin</h1>"


def test_missing_admin_web_assets_are_logged_not_silently_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    web = _fake_package(tmp_path, monkeypatch) / "web"
    app = FastAPI()

    with caplog.at_level(logging.INFO, logger=_LOGGER.name):
        production.mount_admin_web(app)

    assert not _admin_web_mounts(app)
    assert "admin_web.assets_missing" in caplog.text
    assert str(web) in caplog.text
