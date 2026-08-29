from __future__ import annotations

import argparse
import runpy
import urllib.error
import urllib.request
from pathlib import Path
from typing import Self

import pytest

ROOT = Path(__file__).resolve().parents[2]


class _Response:
    status = 200

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return b"{}"


def _cookie_file(
    path: Path,
    *,
    session: str = "s" * 43,
    csrf: str | None = "c" * 43,
) -> Path:
    rows = [
        "# Netscape HTTP Cookie File",
        f"control.example.test\tFALSE\t/\tTRUE\t2147483647\tvonk_session\t{session}",
    ]
    if csrf is not None:
        rows.append(
            f"control.example.test\tFALSE\t/\tTRUE\t2147483647\tvonk_csrf\t{csrf}"
        )
    path.write_text("\n".join(rows) + "\n", encoding="ascii")
    return path


def _namespace() -> dict[str, object]:
    return runpy.run_path(str(ROOT / "scripts" / "import-recipe-library"))


def test_source_files_accept_nested_directories(tmp_path: Path) -> None:
    context = tmp_path / "context"
    (context / "vendor" / "nested").mkdir(parents=True)
    (context / "Dockerfile").write_text("FROM scratch\n")
    (context / "vendor" / "nested" / "artifact.txt").write_text("payload\n")

    namespace = _namespace()

    assert namespace["_files"](context) == {
        "Dockerfile": b"FROM scratch\n",
        "vendor/nested/artifact.txt": b"payload\n",
    }


def test_selected_recipe_imports_only_its_transitive_entity_closure() -> None:
    namespace = _namespace()
    library = ROOT / "config"
    entities = namespace["_load_entities"](library)
    recipes = namespace["_load_recipes"](
        library,
        {"deepseek-v4-flash-0731-ds4-single"},
        False,
    )

    selected = namespace["_entity_closure"](entities, recipes, ROOT)

    assert [item["path"] for item in selected] == [
        "model-groups/deepseek-flash.json",
        "models/deepseek-v4-flash-0731.json",
        "model-versions/deepseek-v4-flash-0731-ds4.json",
        "runtime-distributions/ds4-spark.json",
    ]


def test_selected_entity_closure_is_deterministic_and_follows_patch_references() -> (
    None
):
    namespace = _namespace()
    library = ROOT / "config"
    entities = namespace["_load_entities"](library)
    recipes = namespace["_load_recipes"](
        library,
        {"deepseek-v4-flash-0731-mia-dual"},
        False,
    )

    forward = namespace["_entity_closure"](entities, recipes, ROOT)
    reverse = namespace["_entity_closure"](list(reversed(entities)), recipes, ROOT)

    expected = [
        "model-groups/deepseek-flash.json",
        "models/deepseek-v4-flash-0731.json",
        "model-versions/deepseek-v4-flash-0731-official.json",
        "runtime-distributions/anemll-vllm-mia.json",
        "patch-bundles/mia-deepseek-v4-flash-0731.json",
    ]
    assert [item["path"] for item in forward] == expected
    assert [item["path"] for item in reverse] == expected


def test_patch_without_compatible_versions_uses_validator_empty_edge_semantics() -> (
    None
):
    namespace = _namespace()
    fixture = (
        ROOT
        / "tests/fixtures/import-recipe-library"
        / "sglang-qwen38-flash-next-dual-profile.json"
    )
    document = namespace["_json"](fixture)

    references = namespace["_entity_references"](document, str(fixture))

    assert [reference.portable_identity for reference in references] == [
        (
            "runtime-distribution",
            "miaai-lab",
            "sglang-qwen38-flash-next-dspark-6d1a5194-arm64",
            "d5dee535d9f8c4947246525d0f1089b16f504c51696cd51aed38d378c1934835",
        )
    ]


def test_present_compatible_versions_must_be_a_contract_array() -> None:
    namespace = _namespace()
    fixture = (
        ROOT
        / "tests/fixtures/import-recipe-library"
        / "sglang-qwen38-flash-next-dual-profile.json"
    )
    document = namespace["_json"](fixture)
    document["compatible_model_versions"] = ()

    with pytest.raises(namespace["ImportError"], match="dependency is invalid"):
        namespace["_entity_references"](document, str(fixture))


def test_selected_entity_closure_fails_closed_on_missing_dependency() -> None:
    namespace = _namespace()
    library = ROOT / "config"
    entities = [
        item
        for item in namespace["_load_entities"](library)
        if item["path"] != "models/deepseek-v4-flash-0731.json"
    ]
    recipes = namespace["_load_recipes"](
        library,
        {"deepseek-v4-flash-0731-ds4-single"},
        False,
    )

    with pytest.raises(namespace["ImportError"], match="dependency is missing"):
        namespace["_entity_closure"](entities, recipes, ROOT)


def test_selected_entity_closure_fails_closed_on_ambiguous_dependency() -> None:
    namespace = _namespace()
    library = ROOT / "config"
    entities = namespace["_load_entities"](library)
    model = next(
        item
        for item in entities
        if item["path"] == "models/deepseek-v4-flash-0731.json"
    )
    entities.append({"path": "models/duplicate.json", "document": model["document"]})
    recipes = namespace["_load_recipes"](
        library,
        {"deepseek-v4-flash-0731-ds4-single"},
        False,
    )

    with pytest.raises(namespace["ImportError"], match="dependency is ambiguous"):
        namespace["_entity_closure"](entities, recipes, ROOT)


def test_unselected_import_preserves_all_entity_behavior() -> None:
    namespace = _namespace()
    entities = namespace["_load_entities"](ROOT / "config")

    selected = namespace["_scoped_entities"](entities, [], None, ROOT)

    assert selected is entities


def test_control_client_requires_exactly_one_authentication_method(
    tmp_path: Path,
) -> None:
    namespace = _namespace()
    client_type = namespace["ControlClient"]
    error_type = namespace["ImportError"]
    cookie_file = _cookie_file(tmp_path / "cookies.txt")

    with pytest.raises(error_type, match="exactly one"):
        client_type("https://control.example.test")
    with pytest.raises(error_type, match="exactly one"):
        client_type(
            "https://control.example.test",
            token="token-value",
            cookie_file=cookie_file,
        )


@pytest.mark.parametrize("csrf", [None, "too-short"])
def test_cookie_auth_rejects_missing_or_malformed_csrf_for_mutations(
    tmp_path: Path, csrf: str | None
) -> None:
    namespace = _namespace()
    client = namespace["ControlClient"](
        "https://control.example.test",
        cookie_file=_cookie_file(tmp_path / "cookies.txt", csrf=csrf),
    )

    with pytest.raises(namespace["ImportError"], match="valid CSRF cookie"):
        client.request("POST", "/api/v1/catalog/imports/recipe-library", {})


def test_cookie_auth_sends_matching_csrf_for_preview_apply_and_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace = _namespace()
    csrf = "c" * 43
    session = "s" * 43
    client = namespace["ControlClient"](
        "https://control.example.test",
        cookie_file=_cookie_file(tmp_path / "cookies.txt", session=session, csrf=csrf),
    )
    requests: list[tuple[urllib.request.Request, int]] = []

    def open_request(request: urllib.request.Request, *, timeout: int) -> _Response:
        requests.append((request, timeout))
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", open_request)

    client.request("POST", "/api/v1/catalog/imports/recipe-library/preview", {})
    client.request("POST", "/api/v1/catalog/imports/recipe-library", {})
    client.upload(
        "/api/v1/catalog/source-bundles/sha256",
        b"archive",
        "application/vnd.vonk-forge.source-bundle.v1+tar",
    )

    assert len(requests) == 3
    assert [timeout for _, timeout in requests] == [30, 30, 300]
    for request, _ in requests:
        assert request.get_header("Authorization") is None
        assert request.get_header("Cookie") == (
            f"vonk_session={session}; vonk_csrf={csrf}"
        )
        assert request.get_header("X-csrf-token") == csrf


@pytest.mark.parametrize("value", ["29", "1801", "not-an-integer"])
def test_upload_timeout_is_bounded(value: str) -> None:
    namespace = _namespace()

    with pytest.raises(argparse.ArgumentTypeError, match="upload timeout"):
        namespace["_upload_timeout"](value)


def test_upload_propagates_exact_operator_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _namespace()
    timeouts: list[int] = []
    client = namespace["ControlClient"](
        "https://control.example.test", token="administrator-token"
    )

    def open_request(request: urllib.request.Request, *, timeout: int) -> _Response:
        timeouts.append(timeout)
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", open_request)

    client.upload(
        "/api/v1/catalog/source-bundles/sha256",
        b"archive",
        "application/vnd.vonk-forge.source-bundle.v1+tar",
        timeout=417,
    )

    assert timeouts == [417]


@pytest.mark.parametrize("wrapped", [False, True])
def test_upload_timeout_is_distinct_and_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, wrapped: bool
) -> None:
    namespace = _namespace()
    attempts = 0
    client = namespace["ControlClient"](
        "https://control.example.test", token="administrator-token"
    )

    def timeout_request(request: urllib.request.Request, *, timeout: int) -> _Response:
        nonlocal attempts
        attempts += 1
        error = TimeoutError("slow ingestion")
        raise urllib.error.URLError(error) if wrapped else error

    monkeypatch.setattr(urllib.request, "urlopen", timeout_request)

    with pytest.raises(
        namespace["ImportError"],
        match=(
            r"timed out after 417 seconds: PUT "
            r"/api/v1/catalog/source-bundles/sha256"
        ),
    ):
        client.upload(
            "/api/v1/catalog/source-bundles/sha256",
            b"archive",
            "application/vnd.vonk-forge.source-bundle.v1+tar",
            timeout=417,
        )

    assert attempts == 1


def test_cookie_auth_get_omits_csrf_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace = _namespace()
    request_seen: list[urllib.request.Request] = []
    client = namespace["ControlClient"](
        "https://control.example.test",
        cookie_file=_cookie_file(tmp_path / "cookies.txt"),
    )

    def open_request(request: urllib.request.Request, *, timeout: int) -> _Response:
        request_seen.append(request)
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", open_request)

    client.request("GET", "/api/v1/catalog/entities")

    assert request_seen[0].get_header("Cookie") is not None
    assert request_seen[0].get_header("X-csrf-token") is None


def test_bearer_auth_behavior_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _namespace()
    request_seen: list[urllib.request.Request] = []
    client = namespace["ControlClient"](
        "https://control.example.test", token="administrator-token"
    )

    def open_request(request: urllib.request.Request, *, timeout: int) -> _Response:
        request_seen.append(request)
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", open_request)

    client.request("POST", "/api/v1/catalog/imports/recipe-library", {})

    assert request_seen[0].get_header("Authorization") == ("Bearer administrator-token")
    assert request_seen[0].get_header("Cookie") is None
    assert request_seen[0].get_header("X-csrf-token") is None


def test_error_redaction_removes_bearer_and_cookie_secrets(tmp_path: Path) -> None:
    namespace = _namespace()
    token_client = namespace["ControlClient"](
        "https://control.example.test", token="administrator-token"
    )
    cookie_client = namespace["ControlClient"](
        "https://control.example.test",
        cookie_file=_cookie_file(tmp_path / "cookies.txt"),
    )
    cookie_client._auth_headers(
        "POST", "https://control.example.test/api/v1/catalog/imports/recipe-library"
    )

    assert token_client.redact("failed: administrator-token") == ("failed: [REDACTED]")
    redacted = cookie_client.redact(f"failed: {'s' * 43} {'c' * 43}")
    assert "s" * 43 not in redacted
    assert "c" * 43 not in redacted
    assert redacted == "failed: [REDACTED] [REDACTED]"
