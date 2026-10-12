"""Complete credential intent, exact replay, and last verified material."""

from __future__ import annotations

import os
from pathlib import Path

from vonk_agent_protocol.installer_setup import (
    NasInstallTemplate,
    single_line,
    valid_required_value,
)

from .environment import parse_environment, render_environment
from .filesystem import (
    MAX_TEMPLATE_BYTES,
    ObservationUnavailable,
    SetupError,
    atomic_replace,
    discard,
    ensure_directory,
    metadata,
    read_member,
    real_directory,
    staging_directory,
    sync_directory,
    write_member,
)
from .secrets import (
    generated,
    load_members,
    postgres_url,
    secret_paths,
    validate_ed25519,
)

INTENT = ".vonk-publication"
VERIFIED = ".vonk-verified-secrets"


def validate_members(
    payload: NasInstallTemplate, environment: dict[str, str], members: dict[str, bytes]
) -> None:
    for required in payload.required_values:
        if required.env not in environment or not valid_required_value(
            environment[required.env], required.validation
        ):
            raise SetupError(f"publication environment value {required.env} is invalid")
    if payload.install_modes and "COMPOSE_PROFILES" not in environment:
        raise SetupError("publication install mode is unavailable")
    requests = generated(payload)
    for request in requests.ed25519_pkcs8_pem:
        if request.file not in members:
            raise SetupError(f"publication key {request.file} is unavailable")
        validate_ed25519(members[request.file], request.file)
    for request in requests.random_text:
        try:
            value = members[request.file].decode().rstrip("\r\n")
        except (KeyError, UnicodeDecodeError):
            raise SetupError(
                f"publication credential {request.file} is unavailable"
            ) from None
        if not value or not single_line(value):
            raise SetupError(f"publication credential {request.file} is invalid")
    for request in requests.postgres_urls:
        if request.password_file not in members or members.get(
            request.file
        ) != postgres_url(request, members[request.password_file]):
            raise SetupError(
                f"publication database URL {request.file} does not match its password"
            )
    if payload.step_ca_controller:
        from .pki import validate_pki_material

        validate_pki_material(
            payload.step_ca_controller,
            environment,
            members,
            allow_expired_controller=True,
        )


def sync_tree(root: Path) -> None:
    """Flush nested private staging before its publication name becomes visible."""
    real_directory(root)
    for path in root.iterdir():
        entry = path.lstat()
        if path.is_symlink():
            raise SetupError("unexpected symbolic link in private staging")
        if path.is_dir():
            sync_tree(path)
        elif entry.st_nlink != 1:
            raise SetupError("unexpected multiply linked file in private staging")
    sync_directory(root)


def remember_verified(payload: NasInstallTemplate, bundle: Path) -> None:
    staging = staging_directory(bundle)
    previous = staging / "previous"
    complete = False
    try:
        snapshot = staging / "secrets"
        ensure_directory(snapshot)
        for name, value in load_members(bundle / "secrets", payload).items():
            write_member(snapshot, name, value)
        atomic_replace(
            snapshot / ".template.json",
            payload.model_dump_json(exclude_none=True).encode(),
        )
        environment = read_member(bundle, ".env")
        if environment is not None:
            atomic_replace(snapshot / ".env", environment)
        sync_tree(snapshot)
        verified = bundle / VERIFIED
        if metadata(verified):
            os.replace(verified, previous)
        os.replace(snapshot, verified)
        sync_directory(bundle)
        complete = True
    except BaseException:
        if metadata(previous) is not None and metadata(bundle / VERIFIED) is None:
            os.replace(previous, bundle / VERIFIED)
            sync_directory(bundle)
        raise
    finally:
        # A failed swap retains its preceding generation, even if restoring
        # that name also fails. Only a durable new name retires old material.
        if complete:
            discard(staging)


def previous_template(bundle: Path) -> NasInstallTemplate | None:
    value = read_member(bundle / VERIFIED, ".template.json", limit=MAX_TEMPLATE_BYTES)
    if value is None:
        return None
    try:
        return NasInstallTemplate.model_validate_json(value)
    except ValueError:
        return None


def newly_requested(bundle: Path, name: str, *, environment: bool = False) -> bool:
    previous = previous_template(bundle)
    if previous is None:
        return False
    if environment:
        return name not in {item.env for item in previous.required_values}
    return name not in secret_paths(previous)


def complete_replay(bundle: Path) -> None:
    intent = bundle / INTENT
    if metadata(intent) is None:
        return
    real_directory(intent)
    template = read_member(intent, ".template.json", limit=MAX_TEMPLATE_BYTES)
    if template is None:
        raise SetupError("publication template is unavailable")
    payload = NasInstallTemplate.model_validate_json(template)
    source = intent / "secrets"
    real_directory(source)
    environment = parse_environment(intent / ".env")
    # All members and complete PKI coherence are checked before the first effect.
    members = load_members(source, payload)
    if (
        set(members) != set(secret_paths(payload))
        or read_member(intent, ".env") is None
    ):
        raise SetupError("publication intent is incomplete")
    validate_members(payload, environment, members)
    root = bundle / "secrets"
    real_directory(root)
    for name, content in members.items():
        write_member(root, name, content)
    atomic_replace(bundle / ".env", render_environment(environment))
    remember_verified(payload, bundle)
    retired = staging_directory(bundle)
    os.replace(intent, retired / "published")
    sync_directory(bundle)
    discard(retired)


def stage_intent(
    payload: NasInstallTemplate,
    bundle: Path,
    members: dict[str, bytes],
    environment: dict[str, str],
) -> Path:
    staging = staging_directory(bundle)
    try:
        root = staging / "secrets"
        ensure_directory(root)
        for name, value in members.items():
            write_member(root, name, value)
        atomic_replace(staging / ".env", render_environment(environment))
        atomic_replace(
            staging / ".template.json",
            payload.model_dump_json(exclude_none=True).encode(),
        )
        sync_tree(staging)
        return staging
    except BaseException:
        discard(staging)
        raise


def replay(bundle: Path) -> None:
    try:
        complete_replay(bundle)
    except (SetupError, ValueError):
        # Malformed intent is preserved, then only a complete verified generation
        # may restore a group that an interrupted writer partly exposed.
        intent = bundle / INTENT
        if metadata(intent) is not None:
            retired = staging_directory(bundle)
            os.replace(intent, retired / "retained-intent")
            sync_directory(bundle)
            payload = previous_template(bundle)
            if payload is None:
                raise ObservationUnavailable() from None
            retained = load_members(bundle / VERIFIED, payload)
            environment = parse_environment(bundle / VERIFIED / ".env")
            if set(retained) != set(secret_paths(payload)):
                raise ObservationUnavailable() from None
            validate_members(payload, environment, retained)
            staging = stage_intent(payload, bundle, retained, environment)
            os.replace(staging, intent)
            sync_directory(bundle)
            complete_replay(bundle)
        raise ObservationUnavailable() from None


def publish(
    payload: NasInstallTemplate,
    bundle: Path,
    members: dict[str, bytes],
    environment: dict[str, str],
) -> None:
    validate_members(payload, environment, members)
    staging = stage_intent(payload, bundle, members, environment)
    os.replace(staging, bundle / INTENT)
    sync_directory(bundle)
    replay(bundle)
