"""Wrong implementations rotate custody, defer missing authority, or lose inputs."""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from vonk_agent_protocol.installer_setup import (
    NasGroupReadableSecrets,
    NasInstallTemplate,
    NasRequiredValuePrompt,
    NasSecretPrompt,
)
from vonk_nas_setup.environment import parse_environment
from vonk_nas_setup.filesystem import ObservationUnavailable
from vonk_nas_setup.prompts import PromptIo
from vonk_nas_setup.setup import prepare


def credentials(bundle: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in (bundle / "secrets").iterdir()
        if path.is_file()
    }


def test_fresh_lab_and_secure_remote_keep_secret_answers_private(
    tmp_path: Path, template: NasInstallTemplate
) -> None:
    """Catch ignored install modes, echoed secret answers and unsafe file modes."""
    for mode, answers, expected in (
        ("lab", "lab\n192.168.1.5\nprivate-hf\n", ""),
        (
            "secure",
            "secure-remote\n192.168.1.5\nprivate-hf\nprivate-ts\nyes\n",
            "secure-remote,hermes",
        ),
    ):
        output = io.StringIO()
        bundle = prepare(
            template, tmp_path / mode, PromptIo(io.StringIO(answers), output)
        ).root
        values = parse_environment(bundle / ".env")
        assert values["COMPOSE_PROFILES"] == expected
        assert values["VONK_MANAGEMENT_CIDRS"] == "192.168.1.0/24"
        assert (
            "private-hf" not in output.getvalue()
            and "private-ts" not in output.getvalue()
        )
        assert (bundle / "secrets/hf-token").read_text() == "private-hf\n"
        for path in (
            bundle,
            bundle / "secrets",
            bundle / "secrets/gateway",
            bundle / "backups",
            bundle / "backups-offhost",
        ):
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert stat.S_IMODE((bundle / ".env").stat().st_mode) == 0o600
        assert stat.S_IMODE((bundle / "docker-compose.yaml").stat().st_mode) == 0o644
        assert all(
            stat.S_IMODE((bundle / "secrets" / name).stat().st_mode) == 0o600
            for name in credentials(bundle)
        )
        password = (bundle / "secrets/postgres-password").read_text().strip()
        url = urlsplit((bundle / "secrets/database-url").read_text().strip())
        assert url.password == password
        assert isinstance(
            serialization.load_pem_private_key(
                (bundle / "secrets/signing-key").read_bytes(), None
            ),
            Ed25519PrivateKey,
        )


def test_lost_install_response_and_upgrade_preserve_credentials_and_known_settings(
    installed: Path, template: NasInstallTemplate
) -> None:
    """Catch fresh-request rotation and Hermes overwriting secure-remote membership."""
    before = credentials(installed)
    with (installed / ".env").open("a") as stream:
        stream.write('VONK_HERMES_TIMEZONE="Europe/Amsterdam"\nRETIRED_SETTING=old\n')
    template = template.model_copy(
        update={"docker_compose_yaml": "services: {upgraded: {}}\n"}
    )
    result = prepare(
        template, installed.parent, PromptIo(io.StringIO("")), hermes_enabled=True
    )
    assert result.dropped_environment == ("RETIRED_SETTING",)
    assert credentials(installed) == before
    assert (
        parse_environment(installed / ".env")["VONK_HERMES_TIMEZONE"]
        == "Europe/Amsterdam"
    )
    assert parse_environment(installed / ".env")["COMPOSE_PROFILES"] == "hermes"
    prepare(
        template,
        installed.parent,
        PromptIo(io.StringIO("")),
        upgrade=True,
        hermes_enabled=False,
    )
    assert credentials(installed) == before
    assert parse_environment(installed / ".env")["COMPOSE_PROFILES"] == ""


def test_upgrade_asks_only_new_inputs_and_keeps_secure_remote_profile(
    tmp_path: Path, template: NasInstallTemplate
) -> None:
    """Catch unattended upgrades unexpectedly reopening existing prompts."""
    bundle = prepare(
        template,
        tmp_path,
        PromptIo(io.StringIO("secure-remote\n192.168.1.5\n\nexisting-secret\nno\n")),
    ).root
    template.required_values.append(
        NasRequiredValuePrompt(env="NEW_SITE_VALUE", prompt="New site value")
    )
    template.secrets.append(NasSecretPrompt(file="new-secret", prompt="New secret"))
    result = prepare(
        template,
        tmp_path,
        PromptIo(io.StringIO("new-value\nnew-secret-value\n")),
        upgrade=True,
        hermes_enabled=True,
    )
    assert result.hermes_enabled
    assert parse_environment(bundle / ".env")["NEW_SITE_VALUE"] == "new-value"
    assert (
        parse_environment(bundle / ".env")["COMPOSE_PROFILES"] == "secure-remote,hermes"
    )
    assert (bundle / "secrets/tailscale-key").read_text() == "existing-secret\n"
    assert (bundle / "secrets/new-secret").read_text() == "new-secret-value\n"


@pytest.mark.parametrize("damage", ["missing", "garbled", "ambiguous", "foreign-root"])
def test_projection_repair_reuses_verified_authority(
    installed: Path, template: NasInstallTemplate, damage: str
) -> None:
    """Catch minting new database/signing credentials when stored projections fail."""
    before = credentials(installed)
    if damage == "foreign-root":
        (installed / "secrets").rename(installed / "saved-secrets")
        (installed / "secrets").write_text("preserve this foreign object")
    else:
        (installed / "secrets/signing-key").unlink()
        if damage == "missing":
            (installed / ".env").unlink()
        elif damage == "garbled":
            (installed / ".env").write_text("not dotenv\n")
        else:
            (installed / ".env").write_text(
                "NAS_LAN_IP=10.1.1.1\nNAS_LAN_IP=10.2.2.2\n"
            )
    prepare(template, installed.parent, PromptIo(io.StringIO("")), upgrade=True)
    assert credentials(installed) == before
    assert parse_environment(installed / ".env")["NAS_LAN_IP"] == "192.168.1.5"
    if damage == "foreign-root":
        assert any(
            path.read_text() == "preserve this foreign object"
            for path in installed.glob(".vonk-forge.setup-*/preserved")
        )


def test_absent_custody_does_not_block_compose_and_fresh_request_recovers(
    installed: Path, template: NasInstallTemplate
) -> None:
    """Catch treating missing signer as permission to mint authority or poisoned state."""
    signer = installed / "secrets/signing-key"
    retained = installed / ".vonk-verified-secrets/signing-key"
    original = signer.read_bytes()
    signer.unlink()
    retained.unlink()
    template = template.model_copy(
        update={"docker_compose_yaml": "services: {new: {}}\n"}
    )
    with pytest.raises(ObservationUnavailable):
        prepare(template, installed.parent, PromptIo(io.StringIO("")), upgrade=True)
    assert (
        installed / "docker-compose.yaml"
    ).read_text() == template.docker_compose_yaml
    assert not signer.exists()
    retained.write_bytes(original)
    prepare(template, installed.parent, PromptIo(io.StringIO("")), upgrade=True)
    assert signer.read_bytes() == original


def test_consumed_symlinks_and_hardlinks_never_touch_outside_alias(
    installed: Path, template: NasInstallTemplate, tmp_path: Path
) -> None:
    """Catch reads/chmod/chown through consumed aliases outside the bundle."""
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside private material")
    outside.chmod(0o600)
    signer = installed / "secrets/signing-key"
    original = signer.read_bytes()
    for alias in ("symlink", "hardlink"):
        signer.unlink()
        if alias == "symlink":
            signer.symlink_to(outside)
        else:
            os.link(outside, signer)
        prepare(template, installed.parent, PromptIo(io.StringIO("")), upgrade=True)
        assert signer.read_bytes() == original
        assert outside.read_bytes() == b"outside private material"
        assert stat.S_IMODE(outside.stat().st_mode) == 0o600
    unrelated = installed / "unmanaged-link"
    unrelated.symlink_to(outside)
    stale = installed / ".vonk-forge.setup-1-1"
    stale.mkdir()
    (stale / "evidence").write_text("preserve")
    prepare(template, installed.parent, PromptIo(io.StringIO("")), upgrade=True)
    assert unrelated.is_symlink() and (stale / "evidence").read_text() == "preserve"


def test_exact_group_list_excludes_private_signer_and_rejects_alias(
    installed: Path, template: NasInstallTemplate, tmp_path: Path
) -> None:
    """Catch broad group permission fixes and chmod of a multiply linked lock."""
    template = template.model_copy(
        update={
            "group_readable_secrets": NasGroupReadableSecrets(
                gid=os.getgid() or 1, files=["hf-token"]
            )
        }
    )
    prepare(template, installed.parent, PromptIo(io.StringIO("")), upgrade=True)
    assert stat.S_IMODE((installed / "secrets/hf-token").stat().st_mode) == 0o640
    assert stat.S_IMODE((installed / "secrets/signing-key").stat().st_mode) == 0o600
    lock = installed.parent / ".vonk-setup.lock"
    outside = tmp_path / "outside-lock"
    outside.write_text("not a publication owner")
    outside.chmod(0o600)
    lock.unlink()
    os.link(outside, lock)
    prepare(template, installed.parent, PromptIo(io.StringIO("")), upgrade=True)
    assert lock.stat().st_nlink == 1
    assert outside.read_text() == "not a publication owner"


def test_canonical_payload_rejects_unsafe_effects_before_setup(
    template: NasInstallTemplate,
) -> None:
    """Catch consumer-only validation allowing traversal/duplicate/conflicting effects."""
    original = template.model_dump(mode="json")
    for secret in (
        "../outside",
        "/outside",
        "nested/../outside",
        "signing-key",
        "nested",
    ):
        document = json.loads(json.dumps(original))
        document["secrets"].append({"file": secret, "prompt": "Private input"})
        if secret == "nested":
            document["secrets"].append(
                {"file": "nested/member", "prompt": "Conflicting input"}
            )
        with pytest.raises(ValueError):
            NasInstallTemplate.model_validate_json(json.dumps(document))
