"""The current fresh-install and identity-preserving upgrade workflow."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from vonk_agent_protocol.installer_setup import NasInstallTemplate
from vonk_agent_protocol.lifecycle_vocabulary import SecurityRefusalReason, WaitReason

from . import publication
from .environment import (
    configure_hermes,
    profiles,
    recover_environment,
    render_environment,
    required_default,
    retain_known,
)
from .filesystem import (
    ObservationUnavailable,
    acquire_owner,
    atomic_replace,
    discard,
    ensure_directory,
    metadata,
    output_root,
    real_directory,
    retry,
    staging_directory,
    sync_directory,
    write_member,
)
from .prompts import PromptIo
from .secrets import (
    apply_secret_group,
    generate_missing,
    generated,
    load_members,
    repair_stored_members,
    secret_content,
    secret_paths,
    validate_ed25519,
)


@dataclass(frozen=True)
class SetupOutcome:
    root: Path
    hermes_enabled: bool | None
    dropped_environment: tuple[str, ...] = ()


def permission_note(payload: NasInstallTemplate, root: Path, prompt: PromptIo) -> None:
    pending = apply_secret_group(payload, root)
    if pending and payload.group_readable_secrets:
        prompt.note(
            f"{SecurityRefusalReason.PERMISSION_DENIED.value}: group {payload.group_readable_secrets.gid}: {', '.join(pending)}"
        )


def fresh(
    payload: NasInstallTemplate,
    bundle: Path,
    prompt: PromptIo,
    requested_hermes: bool | None,
) -> SetupOutcome:
    staging = staging_directory(bundle.parent)
    try:
        lab = (
            prompt.install_mode(payload.install_modes)
            if payload.install_modes
            else False
        )
        if not lab and payload.preflight:
            prompt.note("Before continuing, complete this preflight:")
            for item in payload.preflight:
                prompt.note(f"  [ ] {item}")
        environment = {item.env: item.value for item in payload.internal_values}
        if payload.install_modes:
            modes = payload.install_modes
            environment["COMPOSE_PROFILES"] = "" if lab else modes.secure_remote_value
            if lab:
                environment.update((item.env, item.value) for item in modes.lab_values)
        for required in payload.required_values:
            if required.env in environment:
                continue
            value = required_default(required, environment)
            if value is None:
                value = prompt.required(required)
            else:
                prompt.note(f"{required.prompt}: {value} (change it in .env)")
            environment[required.env] = value
        members = {
            item.file: secret_content(
                "" if lab and item.secure_remote_only else prompt.secret(item)
            )
            for item in payload.secrets
        }
        generate_missing(payload, members)
        if payload.step_ca_controller:
            from .pki import generate_pki, validate_pki_material

            request = payload.step_ca_controller
            members.update(retry(lambda: generate_pki(request, environment)))
            validate_pki_material(request, environment, members)
        if payload.hermes and requested_hermes is None:
            requested_hermes = False if lab else prompt.confirm(payload.hermes.prompt)
        hermes_enabled = configure_hermes(payload, environment, requested_hermes)
        ensure_directory(staging / "secrets")
        ensure_directory(staging / "secrets" / "gateway")
        for name in ("backups", "backups-offhost"):
            ensure_directory(staging / name)
        atomic_replace(
            staging / "docker-compose.yaml", payload.docker_compose_yaml.encode(), 0o644
        )
        atomic_replace(staging / ".env", render_environment(environment))
        for name, value in members.items():
            write_member(staging / "secrets", name, value)
        permission_note(payload, staging / "secrets", prompt)
        publication.remember_verified(payload, staging)
        publication.sync_tree(staging)
        os.replace(staging, bundle)
        sync_directory(bundle.parent)
        return SetupOutcome(bundle, hermes_enabled)
    except BaseException:
        discard(staging)
        raise


def upgrade_credentials(
    payload: NasInstallTemplate,
    bundle: Path,
    prompt: PromptIo,
    requested_hermes: bool | None,
) -> SetupOutcome:
    for name in ("backups", "backups-offhost"):
        ensure_directory(bundle / name)
    root = bundle / "secrets"
    ensure_directory(root)
    publication.replay(bundle)
    ensure_directory(root / "gateway")
    environment = recover_environment(payload, bundle)
    if payload.install_modes and "COMPOSE_PROFILES" not in environment:
        raise ObservationUnavailable()
    lab = (
        payload.install_modes is not None
        and payload.install_modes.secure_remote_value
        not in profiles(environment.get("COMPOSE_PROFILES", ""))
    )
    if payload.install_modes and lab:
        for item in payload.install_modes.lab_values:
            environment.setdefault(item.env, item.value)
    for required in payload.required_values:
        if required.env in environment:
            continue
        value = required_default(required, environment)
        if value is None:
            if not publication.newly_requested(bundle, required.env, environment=True):
                raise ObservationUnavailable()
            value = prompt.required(required)
        environment[required.env] = value
    hermes_enabled = configure_hermes(payload, environment, requested_hermes)
    dropped = retain_known(payload, environment)
    # Compose and environment are independently repairable projections. A PKI
    # custody failure below never rolls them back or fabricates signing authority.
    atomic_replace(bundle / ".env", render_environment(environment))
    members = load_members(root, payload)
    retained = load_members(bundle / publication.VERIFIED, payload)
    for name, value in retained.items():
        members.setdefault(name, value)
    repair_stored_members(payload, members, retained)
    for item in payload.secrets:
        if item.file in members:
            continue
        if item.optional or lab and item.secure_remote_only:
            members[item.file] = b""
        elif publication.newly_requested(bundle, item.file):
            members[item.file] = secret_content(prompt.secret(item))
        else:
            raise ObservationUnavailable()
    requests = generated(payload)
    for request in (*requests.random_text, *requests.ed25519_pkcs8_pem):
        if request.file not in members and not publication.newly_requested(
            bundle, request.file
        ):
            raise ObservationUnavailable()
    generate_missing(payload, members)
    for request in requests.ed25519_pkcs8_pem:
        validate_ed25519(members[request.file], request.file)
    if payload.step_ca_controller:
        from .pki import (
            normalize_controller_key,
            normalize_intermediate_key,
            renew_controller_leaf,
            validate_pki_material,
        )

        request = payload.step_ca_controller
        validated = validate_pki_material(
            request, environment, members, allow_expired_controller=True
        )
        members.update(normalize_intermediate_key(request, members))
        members.update(normalize_controller_key(request, members))
        if validated.controller_needs_renewal:
            members.update(renew_controller_leaf(request, environment, members))
    if set(members) != set(secret_paths(payload)):
        raise ObservationUnavailable()
    publication.publish(payload, bundle, members, environment)
    permission_note(payload, root, prompt)
    retired = root / "runtime-configs"
    if metadata(retired):
        try:
            if stat.S_ISDIR(retired.lstat().st_mode):
                retry(lambda: shutil.rmtree(retired))
            else:
                retry(retired.unlink)
        except PermissionError:
            raise
        except OSError:
            print(WaitReason.CLEANUP_UNCONFIRMED.value, file=sys.stderr)
    return SetupOutcome(bundle, hermes_enabled, dropped)


def prepare(
    payload: NasInstallTemplate,
    output: Path,
    prompt: PromptIo,
    *,
    upgrade: bool = False,
    hermes_enabled: bool | None = None,
) -> SetupOutcome:
    """Serialize publishers; an install response lost after rename becomes upgrade."""
    root = retry(lambda: output_root(output))
    with acquire_owner(root):
        bundle = root / "vonk-forge"
        if not upgrade and metadata(bundle) is None:
            return fresh(payload, bundle, prompt, hermes_enabled)
        real_directory(bundle)
        atomic_replace(
            bundle / "docker-compose.yaml", payload.docker_compose_yaml.encode(), 0o644
        )
        return retry(
            lambda: upgrade_credentials(payload, bundle, prompt, hermes_enabled)
        )
