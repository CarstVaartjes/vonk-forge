from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PROVISIONER = ROOT / "deploy/compose/hermes-agent/provision-litellm-key.py"
MASTER_KEY = "m" * 64
HERMES_KEY = "sk-" + "h" * 64


def _module():
    spec = importlib.util.spec_from_file_location(
        "hermes_litellm_key_provisioner", PROVISIONER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _exact(module) -> dict[str, object]:
    return {
        "info": {
            "key_alias": module.KEY_ALIAS,
            "models": module.MODELS,
            "allowed_routes": module.ALLOWED_ROUTES,
            "blocked": False,
        }
    }


def test_missing_key_is_created_with_exact_inference_only_policy(monkeypatch) -> None:
    module = _module()
    calls: list[tuple[str, str, str, dict[str, object] | None]] = []
    info_calls = 0

    def request(method, path, master_key, body=None):
        nonlocal info_calls
        calls.append((method, path, master_key, body))
        if path.startswith("/key/info?"):
            info_calls += 1
            return (404, {}) if info_calls == 1 else (200, _exact(module))
        assert path == "/key/generate"
        return 200, {}

    monkeypatch.setattr(module, "_request", request)

    module.reconcile(MASTER_KEY, HERMES_KEY)

    create = next(call for call in calls if call[1] == "/key/generate")
    assert create[2] == MASTER_KEY
    assert create[3] == {
        "key": HERMES_KEY,
        "key_alias": "vonk-hermes-agent",
        "models": ["hermes-agent"],
        "allowed_routes": ["openai_routes"],
        "blocked": False,
        "metadata": {"managed_by": "vonk-forge", "service": "hermes-agent"},
    }


def test_existing_key_value_is_reused_and_policy_drift_is_reconciled(
    monkeypatch,
) -> None:
    module = _module()
    exact = False
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def request(method, path, _master_key, body=None):
        nonlocal exact
        calls.append((method, path, body))
        if path.startswith("/key/info?"):
            if exact:
                return 200, _exact(module)
            return 200, {
                "info": {
                    "key_alias": "drifted",
                    "models": ["all-proxy-models"],
                    "allowed_routes": [],
                    "blocked": True,
                }
            }
        assert path == "/key/update"
        exact = True
        return 200, {}

    monkeypatch.setattr(module, "_request", request)

    module.reconcile(MASTER_KEY, HERMES_KEY)

    update = next(call for call in calls if call[1] == "/key/update")
    assert update[2] == {
        "key": HERMES_KEY,
        "key_alias": "vonk-hermes-agent",
        "models": ["hermes-agent"],
        "allowed_routes": ["openai_routes"],
        "blocked": False,
    }
    assert all(path != "/key/generate" for _, path, _ in calls)


def test_missing_secret_rotates_only_the_stale_managed_alias(monkeypatch) -> None:
    module = _module()
    calls: list[tuple[str, str, dict[str, object] | None]] = []
    info_calls = 0
    create_calls = 0

    def request(method, path, _master_key, body=None):
        nonlocal create_calls, info_calls
        calls.append((method, path, body))
        if path.startswith("/key/info?"):
            info_calls += 1
            return (200, _exact(module)) if info_calls == 3 else (404, {})
        if path == "/key/generate":
            create_calls += 1
            return (409, {}) if create_calls == 1 else (200, {})
        if path == "/v2/key/info":
            return 200, {"info": [{"key_alias": module.KEY_ALIAS}]}
        assert path == "/key/delete"
        return 200, {}

    monkeypatch.setattr(module, "_request", request)

    module.reconcile(MASTER_KEY, HERMES_KEY)

    assert create_calls == 2
    assert ("POST", "/v2/key/info", {"key_aliases": [module.KEY_ALIAS]}) in calls
    assert ("POST", "/key/delete", {"key_aliases": [module.KEY_ALIAS]}) in calls


def test_exact_existing_key_is_not_recreated_or_updated(monkeypatch) -> None:
    module = _module()
    calls: list[str] = []

    def request(_method, path, _master_key, body=None):
        calls.append(path)
        assert body is None
        return 200, _exact(module)

    monkeypatch.setattr(module, "_request", request)

    module.reconcile(MASTER_KEY, HERMES_KEY)

    assert len(calls) == 2
    assert all(path.startswith("/key/info?") for path in calls)


def test_main_reads_bounded_files_without_printing_keys(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    module = _module()
    master = tmp_path / "master"
    hermes = tmp_path / "hermes"
    master.write_text(MASTER_KEY + "\n", encoding="ascii")
    hermes.write_text(HERMES_KEY + "\n", encoding="ascii")
    observed: list[tuple[str, str]] = []
    monkeypatch.setenv("LITELLM_MASTER_KEY_FILE", str(master))
    monkeypatch.setenv("HERMES_LITELLM_KEY_FILE", str(hermes))
    monkeypatch.setattr(module, "reconcile", lambda first, second: observed.append((first, second)))

    assert module.main() == 0

    assert observed == [(MASTER_KEY, HERMES_KEY)]
    output = capsys.readouterr()
    assert MASTER_KEY not in output.out + output.err
    assert HERMES_KEY not in output.out + output.err


def test_secret_reader_rejects_symlinks(tmp_path: Path) -> None:
    module = _module()
    target = tmp_path / "target"
    target.write_text(HERMES_KEY, encoding="ascii")
    link = tmp_path / "link"
    link.symlink_to(target)

    with pytest.raises(module.ProvisionError):
        module._read_secret(link, prefix="sk-")
