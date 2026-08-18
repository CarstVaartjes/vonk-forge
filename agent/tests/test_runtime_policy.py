from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from vonk_agent.config import AgentConfig
from vonk_agent.main import build_agent
from vonk_agent.releases import ReleaseInstaller
from vonk_agent.runtime_policy import RuntimePolicy, RuntimePolicyError
from vonk_agent.update import (
    AgentUpdater,
    LocalSupervisor,
    ORASAgentTransport,
    PlatformAgentTrust,
    PlatformTUFRouteFetcher,
)
from vonk_agent.workloads import WorkloadOperations


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write(path: Path, raw: bytes, mode: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)
    return path


def _elf(tmp_path: Path) -> Path:
    source = tmp_path / "oras.c"
    source.write_text("int main(void) { return 0; }\n")
    target = tmp_path / "oras"
    subprocess.run(["cc", "-o", str(target), str(source)], check=True)
    target.chmod(0o555)
    return target


def policy_fixture(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    architecture = "aarch64" if platform.machine() in {"aarch64", "arm64"} else "x86_64"
    unhashed_executable = _elf(tmp_path)
    executable_digest = hashlib.sha256(unhashed_executable.read_bytes()).hexdigest()
    executable = tmp_path / f"opt/vonk-forge/third-party/oras/{executable_digest}/oras"
    executable.parent.mkdir(parents=True)
    unhashed_executable.rename(executable)
    auth = _write(
        tmp_path / "var/lib/vonk-forge-agent/registry-auth.json", b"{}\n", 0o600
    )
    bootstrap = _write(tmp_path / "etc/vonk-forge-agent/tuf-root.json", b"{}\n", 0o644)
    workload_bootstrap = _write(
        tmp_path / "etc/vonk-forge-agent/workload-tuf-root.json", b"{}\n", 0o644
    )
    metadata = tmp_path / "var/lib/vonk-forge-agent/tuf/metadata"
    targets = tmp_path / "var/lib/vonk-forge-agent/tuf/targets"
    workload_metadata = tmp_path / "var/lib/vonk-forge-agent/workload-tuf/metadata"
    workload_targets = tmp_path / "var/lib/vonk-forge-agent/workload-tuf/targets"
    releases = tmp_path / "var/lib/vonk-forge/releases"
    staging = tmp_path / "var/lib/vonk-forge/release-staging"
    for directory in (
        metadata,
        targets,
        workload_metadata,
        workload_targets,
        releases,
        staging,
    ):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    document = {
        "adapter": {
            "adapter_id": "node-runtime-v1",
            "executable_relative_path": "bin/runtime-adapter",
            "output_limit_bytes": 65536,
            "timeout_seconds": 60,
        },
        "architecture": architecture,
        "oras": {
            "auth_path": str(auth),
            "executable": str(executable),
            "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "version": "1.3.3",
        },
        "registry_origin": "https://registry.example:8443",
        "release_root": str(releases),
        "repository": "vonk-forge/releases",
        "schema_version": 1,
        "staging_root": str(staging),
        "tuf": {
            "bootstrap_root_path": str(bootstrap),
            "bootstrap_root_sha256": hashlib.sha256(bootstrap.read_bytes()).hexdigest(),
            "metadata_root": str(metadata),
            "target_root": str(targets),
        },
        "workload_tuf": {
            "bootstrap_root_path": str(workload_bootstrap),
            "bootstrap_root_sha256": hashlib.sha256(
                workload_bootstrap.read_bytes()
            ).hexdigest(),
            "metadata_root": str(workload_metadata),
            "target_root": str(workload_targets),
        },
    }
    policy = _write(
        tmp_path / "etc/vonk-forge-agent/runtime-policy.json",
        _canonical(document),
        0o644,
    )
    return policy, document


def test_runtime_policy_loads_only_exact_installed_transport_and_roots(
    tmp_path: Path,
) -> None:
    path, document = policy_fixture(tmp_path)

    policy = RuntimePolicy._load_for_test(path, tmp_path)
    policy.verify_installed()

    assert policy.architecture == document["architecture"]
    assert policy.registry_origin == "https://registry.example:8443"
    assert policy.repository == "vonk-forge/releases"
    assert policy.oras.version == "1.3.3"
    assert policy.adapter.adapter_id == "node-runtime-v1"
    assert policy.adapter.executable_relative_path == "bin/runtime-adapter"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=True),
        lambda d: d.update(architecture="mips64"),
        lambda d: d.update(registry_origin="http://registry.example"),
        lambda d: d.update(repository="../escape"),
        lambda d: d["oras"].update(version="latest"),
        lambda d: d["oras"].update(sha256="A" * 64),
        lambda d: d["adapter"].update(timeout_seconds=61),
        lambda d: d["tuf"].update(metadata_root="relative"),
    ],
)
def test_runtime_policy_rejects_unknown_unreviewed_or_unsafe_values(
    tmp_path: Path, mutate
) -> None:
    path, document = policy_fixture(tmp_path)
    mutate(document)
    path.write_bytes(_canonical(document))

    with pytest.raises(RuntimePolicyError):
        RuntimePolicy._load_for_test(path, tmp_path)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("oras", "executable"),
        ("oras", "auth_path"),
        ("tuf", "bootstrap_root_path"),
        ("tuf", "metadata_root"),
        ("tuf", "target_root"),
        ("workload_tuf", "bootstrap_root_path"),
        ("workload_tuf", "metadata_root"),
        ("workload_tuf", "target_root"),
        (None, "release_root"),
        (None, "staging_root"),
    ],
)
def test_runtime_policy_rejects_alternate_absolute_installed_paths(
    tmp_path: Path, section: str | None, field: str
) -> None:
    path, document = policy_fixture(tmp_path)
    target = document if section is None else document[section]
    assert isinstance(target, dict)
    target[field] = str(tmp_path / "safe-root-owned-alternative" / field)
    path.write_bytes(_canonical(document))

    with pytest.raises(RuntimePolicyError, match="installed location"):
        RuntimePolicy._load_for_test(path, tmp_path)


def test_runtime_policy_rejects_duplicate_symlink_hardlink_and_tampered_artifact(
    tmp_path: Path,
) -> None:
    path, document = policy_fixture(tmp_path)
    raw = _canonical(document)
    path.write_bytes(raw[:-2] + b',"schema_version":1}\n')
    with pytest.raises(RuntimePolicyError, match="duplicate"):
        RuntimePolicy._load_for_test(path, tmp_path)

    path.write_bytes(raw)
    linked_policy = tmp_path / "linked-policy.json"
    linked_policy.symlink_to(path)
    with pytest.raises(RuntimePolicyError):
        RuntimePolicy._load_for_test(linked_policy, tmp_path)

    executable = Path(document["oras"]["executable"])
    hardlink = executable.with_name("oras-link")
    os.link(executable, hardlink)
    with pytest.raises(RuntimePolicyError):
        RuntimePolicy._load_for_test(path, tmp_path).verify_installed()
    hardlink.unlink()
    executable.chmod(0o755)
    executable.write_bytes(b"tampered")
    executable.chmod(0o555)
    with pytest.raises(RuntimePolicyError):
        RuntimePolicy._load_for_test(path, tmp_path).verify_installed()


def test_runtime_policy_rejects_tampered_workload_bootstrap_root(tmp_path: Path) -> None:
    path, document = policy_fixture(tmp_path)
    workload_root = Path(document["workload_tuf"]["bootstrap_root_path"])
    workload_root.write_bytes(b"tampered\n")
    with pytest.raises(RuntimePolicyError, match="workload TUF bootstrap root digest"):
        RuntimePolicy._load_for_test(path, tmp_path).verify_installed()


def test_build_agent_constructs_all_closed_handlers_with_one_credential_store(
    tmp_path: Path, monkeypatch
) -> None:
    policy_path, _ = policy_fixture(tmp_path)
    runtime = RuntimePolicy._load_for_test(policy_path, tmp_path)
    state = tmp_path / "agent-state"
    ca = _write(tmp_path / "ca.pem", b"ca", 0o644)
    certificate = _write(tmp_path / "cert.pem", b"cert", 0o644)
    key = _write(tmp_path / "key.pem", b"key", 0o600)
    nvidia = _write(tmp_path / "nvidia-policy.json", b"{}\n", 0o644)
    token = _write(tmp_path / "token", b"A" * 43 + b"\n", 0o600)
    config = AgentConfig(
        control_origin="https://control.example:8443",
        enrollment_origin="https://enroll.example:8443",
        node_id="spk_0123456789abcdef0123456789abcdef",
        certificate_path=certificate,
        private_key_path=key,
        ca_path=ca,
        poll_min_seconds=1,
        poll_max_seconds=60,
        state_root=state,
        installed_policy_path=nvidia,
        runtime_policy_path=policy_path,
        enrollment_token_path=token,
    )
    sentinel_nvidia = object()
    monkeypatch.setattr(
        "vonk_agent.main.InstalledPolicy.load", lambda _: sentinel_nvidia
    )
    monkeypatch.setattr("vonk_agent.main.RuntimePolicy.load", lambda _: runtime)
    monkeypatch.setenv("VONK_AGENT_PLATFORM_VERSION", "1.0.0")
    monkeypatch.setenv("VONK_AGENT_BUILD_DIGEST", "sha256:" + "b" * 64)
    monkeypatch.setenv("VONK_AGENT_SUPERVISOR_SLOT", "A")
    monkeypatch.setenv("VONK_AGENT_SUPERVISOR_SHA256", "a" * 64)
    monkeypatch.setenv("VONK_AGENT_SUPERVISOR_GENERATION", "1")

    agent = build_agent(config, readiness=SimpleNamespace(report=lambda: None))

    assert isinstance(agent._context.releases, ReleaseInstaller)
    assert isinstance(agent._context.workloads, WorkloadOperations)
    assert isinstance(agent._context.updates, AgentUpdater)
    assert isinstance(agent._context.updates._trust, PlatformAgentTrust)
    assert isinstance(agent._context.updates._transport, ORASAgentTransport)
    assert isinstance(agent._context.updates._supervisor, LocalSupervisor)
    assert agent._context.probe.policy is sentinel_nvidia
    transport = agent._context.releases._transport
    trust = agent._context.releases._trust
    assert transport._policy.credential_provider is agent._credentials
    assert trust._fetcher._credential_provider is agent._credentials
    assert transport._policy.registry_origin == "https://registry.example:8443"
    assert transport._policy.repository == "vonk-forge/releases"
    assert agent._context.updates._transport._client is transport
    platform_fetcher = agent._context.updates._trust._trust._fetcher
    assert isinstance(platform_fetcher, PlatformTUFRouteFetcher)
    assert platform_fetcher._delegate is trust._fetcher
    assert agent._context.updates._trust._trust._metadata_root == (
        runtime.tuf.metadata_root / "platform"
    )
    assert agent._context.updates._trust._trust._target_root == (
        runtime.tuf.target_root / "platform"
    )
    assert agent._context.updates._architecture == {
        "aarch64": "linux-arm64",
        "x86_64": "linux-x86_64",
    }[runtime.architecture]
