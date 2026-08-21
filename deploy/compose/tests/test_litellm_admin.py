import json
from pathlib import Path

from deploy.compose.tests.test_agent_ingress import (
    _adapted_caddy,
    _adapted_development_caddy,
    _environment,
    _rendered,
    _routes_with_handlers,
    _server_on_port,
)

ROOT = Path(__file__).resolve().parents[3]


def _upstream(route: dict) -> str | None:
    encoded = json.dumps(route, sort_keys=True)
    for candidate in ("litellm:4000", "control-api:8000"):
        if candidate in encoded:
            return candidate
    return None


def _path_patterns(route: dict) -> list[str]:
    return [
        path for matcher in route.get("match", []) for path in matcher.get("path", [])
    ]


def _reverse_proxy_dials(value: object) -> list[str]:
    dials: list[str] = []
    if isinstance(value, dict):
        if value.get("handler") == "reverse_proxy":
            dials.extend(upstream["dial"] for upstream in value["upstreams"])
        for child in value.values():
            dials.extend(_reverse_proxy_dials(child))
    elif isinstance(value, list):
        for child in value:
            dials.extend(_reverse_proxy_dials(child))
    return dials


def _route_for_path(adapted: dict, *, port: int, path: str) -> dict:
    routes = _routes_with_handlers(_server_on_port(adapted, port)["routes"])
    return next(route for route in routes if path in _path_patterns(route))


def _assert_litellm_route_is_lease_authorized(route: dict) -> None:
    assert _reverse_proxy_dials(route) == ["litellm:4001", "litellm:4000"]


def test_every_browser_litellm_route_authorizes_before_proxying() -> None:
    for adapted in (
        _adapted_caddy(_environment()),
        _adapted_development_caddy(),
    ):
        _assert_litellm_route_is_lease_authorized(
            _route_for_path(adapted, port=8080, path="/v1/*")
        )
        _assert_litellm_route_is_lease_authorized(
            _route_for_path(adapted, port=8080, path="/litellm/*")
        )


def test_every_caddyfile_has_a_v1_only_internal_lease_edge() -> None:
    for adapted in (
        _adapted_caddy(_environment()),
        _adapted_development_caddy(),
    ):
        edge = _server_on_port(adapted, 8081)
        routes = edge["routes"]

        assert edge["listen"] == [":8081"]
        assert [route.get("match") for route in routes] == [
            [{"path": ["/v1/*"]}],
            None,
        ]
        _assert_litellm_route_is_lease_authorized(routes[0])
        assert routes[1]["handle"] == [
            {"handler": "static_response", "status_code": 404}
        ]


def test_native_litellm_admin_has_a_writable_root_path_and_preserves_auth_health() -> (
    None
):
    service = _rendered("compose.yaml")["services"]["litellm"]

    assert service["environment"] == {
        "DISABLE_ADMIN_UI": "False",
        "HOME": "/root",
        "LITELLM_DATABASE_URL_FILE": "/run/vonk-normalized-secrets/litellm-database-url",
        "LITELLM_MASTER_KEY_FILE": "/run/vonk-normalized-secrets/litellm-master-key",
        "LITELLM_UPSTREAM_KEY_FILE": "/run/vonk-normalized-secrets/litellm-upstream-key",
        "LITELLM_UI_PATH": "/tmp/litellm-ui",
        "PRISMA_QUERY_ENGINE_BINARY": "/root/.cache/prisma-python/binaries/5.4.2/ac9d7041ed77bcc8a8dbd2ab6616b39013829574/node_modules/@prisma/engines/query-engine-debian-openssl-3.0.x",
        "SERVER_ROOT_PATH": "/litellm",
        "STORE_MODEL_IN_DB": "False",
        "XDG_CACHE_HOME": "/root/.cache",
    }
    assert service["read_only"] is True
    assert service["tmpfs"] == [
        "/tmp",
        "/root:exec,mode=0700,uid=10002,gid=10001",
    ]
    assert service.get("secrets", []) == []
    assert any(
        volume["target"] == "/run/vonk-normalized-secrets"
        and volume["source"] == "normalized-private-keys"
        for volume in service["volumes"]
    )
    assert "/health/readiness" in json.dumps(service["healthcheck"])


def test_caddy_routes_native_ui_before_spa_and_blocks_dynamic_model_authority() -> None:
    adapted = _adapted_caddy(_environment())
    routes = _routes_with_handlers(_server_on_port(adapted, 8080)["routes"])

    inference = next(
        (index, route)
        for index, route in enumerate(routes)
        if "/v1/*" in _path_patterns(route)
    )
    model_guard = next(
        (index, route)
        for index, route in enumerate(routes)
        if "/litellm/model/*" in _path_patterns(route)
    )
    native_ui = next(
        (index, route)
        for index, route in enumerate(routes)
        if "/litellm/*" in _path_patterns(route) and _upstream(route) == "litellm:4000"
    )
    spa = next(
        (index, route)
        for index, route in enumerate(routes)
        if _upstream(route) == "control-api:8000"
    )

    assert inference[1]["match"] == [{"path": ["/v1/*"]}]
    assert _upstream(inference[1]) == "litellm:4000"
    assert model_guard[1]["match"] == [
        {
            "method": ["POST", "PUT", "PATCH", "DELETE"],
            "path": [
                "/litellm/model",
                "/litellm/model/*",
                "/litellm/model_group",
                "/litellm/model_group/*",
                "/litellm/config",
                "/litellm/config/*",
            ],
        }
    ]
    assert "static_response" in json.dumps(model_guard[1])
    assert "403" in json.dumps(model_guard[1])
    assert model_guard[0] < native_ui[0] < spa[0]


def test_every_runtime_config_enables_ui_but_keeps_database_models_disabled() -> None:
    bootstrap = json.loads(
        (ROOT / "deploy/compose/litellm/bootstrap-config.json").read_text()
    )
    assert bootstrap["general_settings"]["disable_admin_ui"] is False
    assert bootstrap["general_settings"]["store_model_in_db"] is False

    static = (ROOT / "deploy/compose/litellm/config.yaml").read_text()
    assert "  disable_admin_ui: false\n" in static
    assert "  store_model_in_db: false\n" in static
