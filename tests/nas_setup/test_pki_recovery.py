"""Actual PKI/storage/process seams catch mixed credential generations."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from vonk_agent_protocol.installer_setup import NasInstallTemplate
from vonk_nas_setup import publication
from vonk_nas_setup.environment import parse_environment
from vonk_nas_setup.pki import renew_controller_leaf, validate_pki_material
from vonk_nas_setup.prompts import PromptIo
from vonk_nas_setup.setup import prepare

PKI_PUBLISHER = """
import os, sys, time
from pathlib import Path
from vonk_nas_setup import publication
from vonk_nas_setup.filesystem import acquire_owner
root, ready, boundary = map(Path, sys.argv[1:])
bundle = root / "vonk-forge"
if str(boundary) == "snapshot":
    original = publication.os.replace
    def interrupted(source, target):
        original(source, target)
        if source == bundle / publication.VERIFIED:
            ready.write_text("snapshot-swapped")
            time.sleep(10)
    publication.os.replace = interrupted
else:
    original = publication.write_member
    def interrupted(target, name, content):
        original(target, name, content)
        if name == "controller-server-certificate":
            ready.write_text("certificate-published")
            time.sleep(10)
    publication.write_member = interrupted
with acquire_owner(root):
    publication.complete_replay(bundle)
"""


@pytest.fixture(scope="module")
def pki_project(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, NasInstallTemplate]:
    payload = NasInstallTemplate.model_validate_json("""{
      "schema_version": 2, "docker_compose_yaml": "services: {}\\n",
      "required_values": [{"env":"VONK_CONTROL_HOSTNAME","prompt":"Control hostname","default":"control.example.test","validation":"hostname"}],
      "step_ca_controller": {"hostname_env":"VONK_CONTROL_HOSTNAME","provisioner_name":"vonk-agent","password_bytes":32,
        "files": {"root_certificate":"step-ca/root-certificate","intermediate_certificate":"step-ca/intermediate-certificate",
          "intermediate_private_key":"step-ca/intermediate-key","controller_server_certificate":"controller-server-certificate",
          "controller_server_private_key":"controller-server-key","provisioner_public_jwk":"agent-ca-provisioner-public-jwk","password":"step-ca-password"}}
    }""")
    output = tmp_path_factory.mktemp("pki-recovery-source")
    return prepare(payload, output, PromptIo(io.StringIO(""))).root, payload


@pytest.mark.parametrize("boundary", ["certificate", "snapshot"])
def test_killed_pki_publisher_replays_bound_old_template_before_new_inputs(
    tmp_path: Path,
    pki_project: tuple[Path, NasInstallTemplate],
    boundary: str,
) -> None:
    """Catch key/cert mismatch after death or applying new inventory to old intent."""
    from vonk_agent_protocol.installer_setup import NasSecretPrompt

    source, template = pki_project
    bundle = tmp_path / "vonk-forge"
    shutil.copytree(source, bundle)
    request = template.step_ca_controller
    assert request is not None
    old_group = publication.load_members(bundle / "secrets", template)
    environment = parse_environment(bundle / ".env")
    environment[request.hostname_env] = "control.new.example.test"
    candidate = {**old_group, **renew_controller_leaf(request, environment, old_group)}
    assert (
        candidate[request.files.controller_server_private_key]
        != old_group[request.files.controller_server_private_key]
    )
    staging = publication.stage_intent(template, bundle, candidate, environment)
    staging.rename(bundle / publication.INTENT)
    ready = tmp_path / "publisher-ready"
    child = subprocess.Popen(
        [sys.executable, "-c", PKI_PUBLISHER, str(tmp_path), str(ready), boundary],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            if child.poll() is not None:
                pytest.fail(child.communicate()[1].decode())
            if time.monotonic() >= deadline:
                pytest.fail("publisher did not reach PKI fault boundary")
            time.sleep(0.01)
        child.kill()
        child.communicate(timeout=5)
        if boundary == "certificate":
            with pytest.raises(ValueError, match="PKI"):
                validate_pki_material(
                    request,
                    environment,
                    publication.load_members(bundle / "secrets", template),
                )
        else:
            assert not (bundle / publication.VERIFIED).exists()
            assert any(
                path.is_dir() for path in bundle.glob(".vonk-forge.setup-*/previous")
            )
        newer = template.model_copy(
            update={
                "secrets": [NasSecretPrompt(file="new-secret", prompt="New secret")]
            }
        )
        prepare(
            newer, tmp_path, PromptIo(io.StringIO("private-new-answer\n")), upgrade=True
        )
        recovered = publication.load_members(bundle / "secrets", template)
        assert recovered == candidate
        validate_pki_material(request, environment, recovered)
        assert (bundle / "secrets/new-secret").read_bytes() == b"private-new-answer\n"
        assert not (bundle / publication.INTENT).exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


def test_well_formed_foreign_controller_key_remains_explicit_blocker(
    tmp_path: Path, pki_project: tuple[Path, NasInstallTemplate]
) -> None:
    """Catch replacing coherent-but-foreign crypto from the backup without intent."""
    source, template = pki_project
    bundle = tmp_path / "vonk-forge"
    shutil.copytree(source, bundle)
    request = template.step_ca_controller
    assert request is not None
    foreign = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    target = bundle / "secrets" / request.files.controller_server_private_key
    target.write_bytes(foreign)
    template = template.model_copy(
        update={"docker_compose_yaml": "services: {upgraded: {}}\n"}
    )
    with pytest.raises(ValueError, match="PKI"):
        prepare(template, tmp_path, PromptIo(io.StringIO("")), upgrade=True)
    assert target.read_bytes() == foreign
    assert (bundle / "docker-compose.yaml").read_text() == template.docker_compose_yaml
