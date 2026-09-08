"""Generated admin upload -> actual service/store -> emitted client response."""

import io
from datetime import UTC, datetime

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.source_bundles import SourceBundleManifest
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.catalog_api import install_catalog_routes
from vonk_control.catalog_service import CatalogService
from vonk_control.models import Base, RecipeSourceBundle
from vonk_control.source_bundles import SourceBundleStore, generate_source_bundle
from vonk_control.strict_json import ControllerAPIRoute

from cluster_profiles.generated_control.api.default import upload_recipe_source_bundle
from cluster_profiles.generated_control.client import AuthenticatedClient
from cluster_profiles.generated_control.models.source_bundle_response import (
    SourceBundleResponse as ClientSourceBundleResponse,
)
from cluster_profiles.generated_control.types import File


def test_generated_admin_upload_roundtrips_actual_canonical_bundle(tmp_path):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    catalog = CatalogService(
        sessions,
        clock=lambda: datetime(2026, 9, 8, tzinfo=UTC),
        cursors=TokenCodec(b"c" * 32).cursor_codec(),
        source_bundles=SourceBundleStore(tmp_path / "bundles"),
    )
    app = FastAPI()
    app.router.route_class = ControllerAPIRoute

    @app.middleware("http")
    async def request_identity(request, call_next):
        request.state.request_id = "00000000-0000-4000-8000-000000000001"
        return await call_next(request)

    install_catalog_routes(
        app,
        actor_dependency=Depends(lambda: Actor("test", "administrator")),
        audits=MemoryAuditStore(),
        service=catalog,
    )
    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\n", "empty": b""})
    with TestClient(app) as transport:
        client = AuthenticatedClient(
            base_url="http://testserver", token="test"
        ).set_httpx_client(transport)
        uploaded = upload_recipe_source_bundle.sync_detailed(
            bundle.sha256,
            client=client,
            body=File(payload=io.BytesIO(bundle.archive)),
        )
        assert uploaded.status_code == 200
        assert isinstance(uploaded.parsed, ClientSourceBundleResponse)
        assert uploaded.parsed.sha256 == bundle.sha256
        assert uploaded.parsed.archive_bytes == len(bundle.archive)
        assert uploaded.parsed.files == ["Dockerfile", "empty"]
        downloaded = transport.get(
            f"/api/v1/catalog/source-bundles/{uploaded.parsed.sha256}"
        )
        assert downloaded.status_code == 200
        assert downloaded.content == bundle.archive
        assert (
            downloaded.headers["content-type"]
            == "application/vnd.vonk-forge.source-bundle.v1+tar"
        )
    with sessions() as session:
        persisted = session.get(RecipeSourceBundle, bundle.sha256)
        assert (
            SourceBundleManifest.model_validate_json(
                canonical_message(persisted.manifest)
            )
            == bundle.manifest
        )
    engine.dispose()
