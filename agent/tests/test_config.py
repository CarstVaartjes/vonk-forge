from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from vonk_agent.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_STATE_ROOT,
    AgentConfig,
    AgentConfigError,
)

NODE_ID = "spk_0123456789abcdef0123456789abcdef"


def _regular_file(path: Path, *, mode: int = 0o644) -> str:
    path.write_text("test material", encoding="utf-8")
    path.chmod(mode)
    return str(path)


def _valid_document(tmp_path: Path) -> dict[str, object]:
    return {
        "control_origin": "https://control.example.test",
        "enrollment_origin": "https://enroll.example.test",
        "node_id": NODE_ID,
        "certificate_path": _regular_file(tmp_path / "node.crt"),
        "private_key_path": _regular_file(tmp_path / "node.key", mode=0o600),
        "ca_fingerprint": "a" * 64,
        "poll_min_seconds": 2,
        "poll_max_seconds": 30,
        "state_root": str(tmp_path / "state"),
        "installed_policy_path": _regular_file(tmp_path / "policy.json"),
        "runtime_policy_path": _regular_file(tmp_path / "runtime-policy.json"),
        "enrollment_token_path": _regular_file(
            tmp_path / "enrollment-token", mode=0o600
        ),
    }


def _write_config(
    tmp_path: Path, document: object, *, raw: bytes | None = None
) -> Path:
    path = tmp_path / "config.json"
    path.write_bytes(raw if raw is not None else json.dumps(document).encode("utf-8"))
    return path


def test_defaults_are_fixed_system_locations() -> None:
    assert DEFAULT_CONFIG_PATH == Path("/etc/vonk-forge-agent/config.json")
    assert DEFAULT_STATE_ROOT == Path("/var/lib/vonk-forge-agent")


def test_valid_configuration_is_immutable_and_typed(tmp_path: Path) -> None:
    document = _valid_document(tmp_path)

    config = AgentConfig.load(_write_config(tmp_path, document))

    assert config.control_origin == "https://control.example.test"
    assert config.enrollment_origin == "https://enroll.example.test"
    assert config.node_id == NODE_ID
    assert config.certificate_path == Path(document["certificate_path"])
    assert config.private_key_path == Path(document["private_key_path"])
    assert config.ca_path == Path(document["ca_path"])
    assert config.poll_min_seconds == 2
    assert config.poll_max_seconds == 30
    assert config.state_root == Path(document["state_root"])
    assert config.installed_policy_path == Path(document["installed_policy_path"])
    assert config.runtime_policy_path == Path(document["runtime_policy_path"])
    assert config.enrollment_token_path == Path(document["enrollment_token_path"])
    with pytest.raises(AttributeError):
        config.node_id = "spk_ffffffffffffffffffffffffffffffff"  # type: ignore[misc]

def test_configuration_accepts_valid_ca_fingerprint(tmp_path: Path) -> None:
    config = AgentConfig.load(_write_config(tmp_path, _valid_document(tmp_path)))

    assert config.ca_fingerprint == "a" * 64


@pytest.mark.parametrize(
    "fingerprint",
    ["A" * 64, "a" * 63, "a" * 65, "g" * 64, "a" * 63 + " ", 123],
    ids=["uppercase", "short", "long", "non-hex", "whitespace", "non-string"],
)
def test_configuration_rejects_malformed_ca_fingerprint(
    tmp_path: Path, fingerprint: object
) -> None:
    document = _valid_document(tmp_path)
    document["ca_fingerprint"] = fingerprint

    with pytest.raises(AgentConfigError, match="fingerprint"):
        AgentConfig.load(_write_config(tmp_path, document))


def test_configuration_rejects_missing_ca_fingerprint(tmp_path: Path) -> None:
    document = _valid_document(tmp_path)
    document.pop("ca_fingerprint")

    with pytest.raises(AgentConfigError):
        AgentConfig.load(_write_config(tmp_path, document))


def test_configuration_allows_exact_bootstrap_and_post_enrollment_file_states(
    tmp_path: Path,
) -> None:
    bootstrap = _valid_document(tmp_path)
    Path(bootstrap["certificate_path"]).unlink()
    Path(bootstrap["private_key_path"]).unlink()
    assert AgentConfig.load(_write_config(tmp_path, bootstrap)).node_id == NODE_ID

    enrolled = _valid_document(tmp_path)
    Path(enrolled["enrollment_token_path"]).unlink()
    assert AgentConfig.load(_write_config(tmp_path, enrolled)).node_id == NODE_ID

    partial = _valid_document(tmp_path)
    Path(partial["private_key_path"]).unlink()
    with pytest.raises(AgentConfigError, match="paired"):
        AgentConfig.load(_write_config(tmp_path, partial))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("node_id"),
        lambda value: value.update({"enrollment_token": "never-persist-me"}),
    ],
    ids=["missing-field", "unknown-field"],
)
def test_configuration_rejects_missing_or_unknown_fields(
    tmp_path: Path, mutate
) -> None:
    document = _valid_document(tmp_path)
    mutate(document)

    with pytest.raises(AgentConfigError):
        AgentConfig.load(_write_config(tmp_path, document))


def test_configuration_rejects_duplicate_fields(tmp_path: Path) -> None:
    document = _valid_document(tmp_path)
    raw = json.dumps(document).encode("utf-8")
    raw = raw[:-1] + b',"node_id":"spk_ffffffffffffffffffffffffffffffff"}'

    with pytest.raises(AgentConfigError, match="duplicate"):
        AgentConfig.load(_write_config(tmp_path, {}, raw=raw))


@pytest.mark.parametrize("raw", [b"[]", b'"text"', b"null"])
def test_configuration_rejects_non_object_json(tmp_path: Path, raw: bytes) -> None:
    with pytest.raises(AgentConfigError):
        AgentConfig.load(_write_config(tmp_path, {}, raw=raw))


def test_configuration_reads_are_utf8_and_size_bounded(tmp_path: Path) -> None:
    with pytest.raises(AgentConfigError):
        AgentConfig.load(_write_config(tmp_path, {}, raw=b"\xff"))

    with pytest.raises(AgentConfigError, match="large"):
        AgentConfig.load(
            _write_config(tmp_path, {}, raw=b"{" + b" " * (64 * 1024) + b"}")
        )


@pytest.mark.parametrize(
    "field",
    [
        "certificate_path",
        "private_key_path",
        "ca_path",
        "installed_policy_path",
        "runtime_policy_path",
        "enrollment_token_path",
    ],
)
def test_configuration_rejects_oversized_identity_or_policy_files(
    tmp_path: Path, field: str
) -> None:
    document = _valid_document(tmp_path)
    oversized = tmp_path / f"large-{field}"
    oversized.write_bytes(b"x" * (64 * 1024 + 1))
    oversized.chmod(
        0o600 if field in {"private_key_path", "enrollment_token_path"} else 0o644
    )
    document[field] = str(oversized)

    with pytest.raises(AgentConfigError, match="large"):
        AgentConfig.load(_write_config(tmp_path, document))


@pytest.mark.parametrize(
    "control_origin",
    [
        "http://control.example.test",
        "https://user:pass@control.example.test",
        "https://control.example.test/path",
        "https://control.example.test?query=yes",
        "https://control.example.test#fragment",
        "https://control.example.test/",
    ],
)
def test_configuration_rejects_non_origin_control_urls(
    tmp_path: Path, control_origin: str
) -> None:
    document = _valid_document(tmp_path)
    document["control_origin"] = control_origin

    with pytest.raises(AgentConfigError):
        AgentConfig.load(_write_config(tmp_path, document))


@pytest.mark.parametrize(
    "node_id",
    [
        "SPK_0123456789abcdef0123456789abcdef",
        "spk_abc",
        "spk_0123456789ABCDEF0123456789ABCDEF",
    ],
)
def test_configuration_rejects_noncanonical_node_ids(
    tmp_path: Path, node_id: str
) -> None:
    document = _valid_document(tmp_path)
    document["node_id"] = node_id

    with pytest.raises(AgentConfigError):
        AgentConfig.load(_write_config(tmp_path, document))


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [(True, 30), (2, False), (0, 30), (2, 301), (31, 30), (1.5, 30)],
)
def test_configuration_rejects_invalid_poll_bounds(
    tmp_path: Path, minimum: object, maximum: object
) -> None:
    document = _valid_document(tmp_path)
    document["poll_min_seconds"] = minimum
    document["poll_max_seconds"] = maximum

    with pytest.raises(AgentConfigError):
        AgentConfig.load(_write_config(tmp_path, document))


@pytest.mark.parametrize(
    "field",
    [
        "certificate_path",
        "private_key_path",
        "ca_path",
        "state_root",
        "installed_policy_path",
        "runtime_policy_path",
        "enrollment_token_path",
    ],
)
def test_configuration_rejects_relative_paths(tmp_path: Path, field: str) -> None:
    document = _valid_document(tmp_path)
    document[field] = "relative/path"

    with pytest.raises(AgentConfigError):
        AgentConfig.load(_write_config(tmp_path, document))


def test_configuration_rejects_noncanonical_absolute_path(tmp_path: Path) -> None:
    document = _valid_document(tmp_path)
    document["state_root"] = f"{tmp_path}/../state"

    with pytest.raises(AgentConfigError, match="canonical"):
        AgentConfig.load(_write_config(tmp_path, document))


@pytest.mark.parametrize(
    "field",
    [
        "certificate_path",
        "private_key_path",
        "ca_path",
        "installed_policy_path",
        "runtime_policy_path",
        "enrollment_token_path",
    ],
)
def test_configuration_rejects_non_regular_files(tmp_path: Path, field: str) -> None:
    document = _valid_document(tmp_path)
    document[field] = str(tmp_path)

    with pytest.raises(AgentConfigError):
        AgentConfig.load(_write_config(tmp_path, document))


def test_configuration_rejects_leaf_and_parent_symlinks(tmp_path: Path) -> None:
    leaf_document = _valid_document(tmp_path)
    leaf = tmp_path / "leaf.crt"
    leaf.symlink_to(Path(leaf_document["certificate_path"]))
    leaf_document["certificate_path"] = str(leaf)
    with pytest.raises(AgentConfigError):
        AgentConfig.load(_write_config(tmp_path, leaf_document))

    parent_document = _valid_document(tmp_path)
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    parent_document["ca_path"] = _regular_file(actual / "ca.crt")
    parent_document["ca_path"] = str(linked / "ca.crt")
    with pytest.raises(AgentConfigError):
        AgentConfig.load(_write_config(tmp_path, parent_document))


def test_configuration_rejects_fifo_and_device_without_blocking(tmp_path: Path) -> None:
    document = _valid_document(tmp_path)
    fifo = tmp_path / "credential.fifo"
    os.mkfifo(fifo)
    document["certificate_path"] = str(fifo)
    with pytest.raises(AgentConfigError):
        AgentConfig.load(_write_config(tmp_path, document))

    document = _valid_document(tmp_path)
    document["ca_path"] = "/dev/null"
    with pytest.raises(AgentConfigError):
        AgentConfig.load(_write_config(tmp_path, document))


def test_configuration_file_rejects_symlink_and_fifo_without_reading_them(
    tmp_path: Path,
) -> None:
    document = _valid_document(tmp_path)
    target = _write_config(tmp_path, document)
    linked = tmp_path / "linked-config.json"
    linked.symlink_to(target)
    with pytest.raises(AgentConfigError):
        AgentConfig.load(linked)

    fifo = tmp_path / "config.fifo"
    os.mkfifo(fifo)
    with pytest.raises(AgentConfigError):
        AgentConfig.load(fifo)


def test_configuration_rejects_permissive_private_key(tmp_path: Path) -> None:
    document = _valid_document(tmp_path)
    Path(document["private_key_path"]).chmod(0o640)

    with pytest.raises(AgentConfigError, match="private key"):
        AgentConfig.load(_write_config(tmp_path, document))


def test_configuration_errors_do_not_echo_secret_values(tmp_path: Path) -> None:
    sentinel = "highly-sensitive-enrollment-value"
    path = _write_config(
        tmp_path, {}, raw=('{"enrollment_token":"' + sentinel).encode()
    )

    with pytest.raises(AgentConfigError) as caught:
        AgentConfig.load(path)

    assert sentinel not in str(caught.value)


@pytest.mark.parametrize(
    "origin",
    [
        "https://CONTROL.example.test",
        "HTTPS://control.example.test",
        "https://control.example.test?",
        "https://control.example.test#",
        "https://control.example.test:0",
        "https://control.example.test:65536",
        "https://control.example.test:",
        "https://[2001:db8::1",
        "https://2001:db8::1",
        "https://control.example.test\\path",
        "https://control.example.test\n",
        "https:///missing-host",
        "https://127.1",
        "https://2130706433",
        "https://0177.0.0.1",
        "https://0x7f.0.0.1",
    ],
)
def test_configuration_rejects_noncanonical_or_unusable_https_origins(
    tmp_path: Path, origin: str
) -> None:
    document = _valid_document(tmp_path)
    document["control_origin"] = origin

    with pytest.raises(AgentConfigError):
        AgentConfig.load(_write_config(tmp_path, document))


@pytest.mark.parametrize(
    "origin",
    [
        "https://control.example.test:8443",
        "https://192.0.2.10",
        "https://[2001:db8::1]:8443",
    ],
)
def test_configuration_accepts_canonical_https_dns_and_ip_origins(
    tmp_path: Path, origin: str
) -> None:
    document = _valid_document(tmp_path)
    document["control_origin"] = origin

    assert AgentConfig.load(_write_config(tmp_path, document)).control_origin == origin


def test_configuration_rejects_untrusted_writable_credential_path(
    tmp_path: Path,
) -> None:
    document = _valid_document(tmp_path)
    certificate = Path(document["certificate_path"])
    certificate.chmod(0o666)

    with pytest.raises(AgentConfigError):
        AgentConfig.load(_write_config(tmp_path, document))


def test_configuration_rejects_a_credential_swapped_to_symlink_during_open(
    tmp_path: Path, monkeypatch
) -> None:
    document = _valid_document(tmp_path)
    certificate = Path(document["certificate_path"])
    replacement = tmp_path / "replacement.crt"
    _regular_file(replacement)
    real_open = os.open

    def swapping_open(path, flags, *args, **kwargs):
        if path == certificate.name and kwargs.get("dir_fd") is not None:
            certificate.unlink()
            certificate.symlink_to(replacement)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("vonk_agent.config.os.open", swapping_open)
    with pytest.raises(AgentConfigError):
        AgentConfig.load(_write_config(tmp_path, document))


def test_project_imports_normally_outside_the_repository(tmp_path: Path) -> None:
    project = Path(__file__).parents[1]
    result = subprocess.run(
        ["uv", "run", "--project", str(project), "python", "-c", "import vonk_agent"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
