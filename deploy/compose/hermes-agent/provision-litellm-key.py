"""Idempotently register the installer-owned Hermes key with LiteLLM."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

LITELLM_URL = "http://litellm:4000"
MASTER_KEY_FILE = Path("/run/secrets/litellm-master-key")
HERMES_KEY_FILE = Path("/run/secrets/hermes-litellm-key")
KEY_ALIAS = "vonk-hermes-agent"
MODELS = ["hermes-agent"]
ALLOWED_ROUTES = ["openai_routes"]
MAX_SECRET_BYTES = 4096
MAX_RESPONSE_BYTES = 64 * 1024
TOKEN = re.compile(r"^[A-Za-z0-9_.~-]+$")


class ProvisionError(RuntimeError):
    """A bounded, non-secret provisioning failure."""


def _read_secret(path: Path, *, prefix: str | None = None) -> str:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ProvisionError(f"required secret {path.name} is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_size == 0
        or metadata.st_size > MAX_SECRET_BYTES
    ):
        raise ProvisionError(f"required secret {path.name} is invalid")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ProvisionError(f"required secret {path.name} is unreadable") from error
    value = raw.rstrip(b"\r\n")
    if not value or b"\r" in value or b"\n" in value:
        raise ProvisionError(f"required secret {path.name} is invalid")
    try:
        token = value.decode("ascii")
    except UnicodeDecodeError as error:
        raise ProvisionError(f"required secret {path.name} is invalid") from error
    if len(token) < 32 or TOKEN.fullmatch(token) is None:
        raise ProvisionError(f"required secret {path.name} is invalid")
    if prefix is not None and not token.startswith(prefix):
        raise ProvisionError(f"required secret {path.name} is invalid")
    return token


def _decode_response(response: Any) -> dict[str, Any]:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ProvisionError("LiteLLM returned an oversized response")
    if not body:
        return {}
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProvisionError("LiteLLM returned a malformed response") from error
    if not isinstance(payload, dict):
        raise ProvisionError("LiteLLM returned a malformed response")
    return payload


def _request(
    method: str,
    path: str,
    master_key: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    encoded = None
    headers = {"Authorization": f"Bearer {master_key}"}
    if body is not None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{LITELLM_URL}{path}", data=encoded, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status, _decode_response(response)
    except urllib.error.HTTPError as error:
        return error.code, _decode_response(error)
    except (OSError, urllib.error.URLError) as error:
        raise ProvisionError("LiteLLM key management endpoint is unavailable") from error


def _key_info(master_key: str, hermes_key: str) -> tuple[int, dict[str, Any]]:
    query = urllib.parse.urlencode({"key": hermes_key})
    return _request("GET", f"/key/info?{query}", master_key)


def _details(payload: dict[str, Any]) -> dict[str, Any]:
    info = payload.get("info")
    return info if isinstance(info, dict) else payload


def _is_exact(payload: dict[str, Any]) -> bool:
    info = _details(payload)
    return (
        info.get("key_alias") == KEY_ALIAS
        and info.get("models") == MODELS
        and info.get("allowed_routes") == ALLOWED_ROUTES
        and info.get("blocked") in (False, None)
    )


def _create(master_key: str, hermes_key: str) -> tuple[int, dict[str, Any]]:
    return _request(
        "POST",
        "/key/generate",
        master_key,
        {
            "key": hermes_key,
            "key_alias": KEY_ALIAS,
            "models": MODELS,
            "allowed_routes": ALLOWED_ROUTES,
            "blocked": False,
            "metadata": {"managed_by": "vonk-forge", "service": "hermes-agent"},
        },
    )


def _update(master_key: str, hermes_key: str) -> None:
    status, _ = _request(
        "POST",
        "/key/update",
        master_key,
        {
            "key": hermes_key,
            "key_alias": KEY_ALIAS,
            "models": MODELS,
            "allowed_routes": ALLOWED_ROUTES,
            "blocked": False,
        },
    )
    if status != 200:
        raise ProvisionError("LiteLLM rejected the Hermes key policy update")


def _replace_stale_managed_key(master_key: str, hermes_key: str) -> bool:
    status, payload = _request(
        "POST",
        "/v2/key/info",
        master_key,
        {"key_aliases": [KEY_ALIAS]},
    )
    info = payload.get("info")
    if status != 200 or not isinstance(info, list):
        return False
    matching = [
        item
        for item in info
        if isinstance(item, dict) and item.get("key_alias") == KEY_ALIAS
    ]
    if len(matching) != 1:
        return False
    delete_status, _ = _request(
        "POST",
        "/key/delete",
        master_key,
        {"key_aliases": [KEY_ALIAS]},
    )
    if delete_status != 200:
        raise ProvisionError("LiteLLM rejected stale Hermes key revocation")
    create_status, _ = _create(master_key, hermes_key)
    if create_status not in {200, 201}:
        raise ProvisionError("LiteLLM rejected Hermes replacement key creation")
    return True


def reconcile(master_key: str, hermes_key: str) -> None:
    status, info = _key_info(master_key, hermes_key)
    if status == 200:
        if not _is_exact(info):
            _update(master_key, hermes_key)
    elif status in {400, 404}:
        create_status, _ = _create(master_key, hermes_key)
        if create_status not in {200, 201}:
            # A concurrent provisioner may have created the same installer-owned
            # key after the initial lookup. Re-read before declaring failure.
            retry_status, _ = _key_info(master_key, hermes_key)
            if retry_status == 200:
                _update(master_key, hermes_key)
            elif not _replace_stale_managed_key(master_key, hermes_key):
                raise ProvisionError("LiteLLM rejected Hermes key creation")
    else:
        raise ProvisionError("LiteLLM rejected Hermes key lookup")

    verify_status, verified = _key_info(master_key, hermes_key)
    if verify_status != 200 or not _is_exact(verified):
        raise ProvisionError("LiteLLM did not persist the exact Hermes key policy")


def main() -> int:
    master_path = Path(os.environ.get("LITELLM_MASTER_KEY_FILE", MASTER_KEY_FILE))
    hermes_path = Path(os.environ.get("HERMES_LITELLM_KEY_FILE", HERMES_KEY_FILE))
    try:
        master_key = _read_secret(master_path)
        hermes_key = _read_secret(hermes_path, prefix="sk-")
        reconcile(master_key, hermes_key)
    except ProvisionError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Hermes LiteLLM client key is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
