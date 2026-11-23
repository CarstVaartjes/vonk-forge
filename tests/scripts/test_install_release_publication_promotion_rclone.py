"""Install release publication: promotion rclone."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.scripts.install_release_publication_support import (
    DIGEST,
    ROOT,
    SCRIPT,
    SOURCE_SHA,
    SPARK_PLATFORMS,
    _acceptance_receipt,
    _agent_package,
    _assemble,
    _assemble_command,
    _canonical,
    _input_mapping,
    _input_path,
    _inputs,
    _promote,
    _promote_command,
    _publish_accepted,
    _publish_candidate,
)

pytestmark = pytest.mark.needs_dpkg_deb


def _fake_rclone_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    executable_root = tmp_path / "bin"
    executable_root.mkdir()
    object_root = tmp_path / "r2"
    object_root.mkdir()
    executable = executable_root / "rclone"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import fcntl
import os
import shutil
import sys
import time
from pathlib import Path

command = sys.argv[1]
arguments = sys.argv[2:]
object_root = Path(os.environ["FAKE_RCLONE_ROOT"])

def object_path(value: str) -> Path:
    remote_path = value.split(":", 1)[1]
    key = remote_path.split("/", 1)[1]
    return object_root / key

def update_activity(delta: int) -> None:
    activity_path = os.environ.get("FAKE_RCLONE_ACTIVITY")
    if activity_path is None:
        return
    with Path(activity_path).open("a+", encoding="utf-8") as activity:
        fcntl.flock(activity.fileno(), fcntl.LOCK_EX)
        activity.seek(0)
        raw = activity.read()
        state = json.loads(raw) if raw else {"active": 0, "maximum": 0}
        state["active"] += delta
        state["maximum"] = max(state["maximum"], state["active"])
        activity.seek(0)
        activity.truncate()
        json.dump(state, activity)
        activity.flush()
        fcntl.flock(activity.fileno(), fcntl.LOCK_UN)

def wait_for_peer(kind: str, key: str) -> None:
    barrier = os.environ.get("FAKE_RCLONE_PARALLEL_BARRIER")
    if barrier is None:
        return
    selected = (
        kind == "read" and "/releases/" in key and "/acceptance/" not in key
    ) or (
        kind == "acceptance" and "/acceptance/" in key
    ) or (
        kind == "endpoint" and key in {"nas", "spark", "vonkctl"}
    )
    if not selected:
        return
    markers = Path(barrier) / kind
    markers.mkdir(parents=True, exist_ok=True)
    (markers / str(os.getpid())).touch()
    deadline = time.monotonic() + 2
    while len(list(markers.iterdir())) < 2:
        if time.monotonic() >= deadline:
            raise SystemExit(70)
        time.sleep(0.01)

def fault(name: str, key: str) -> bool:
    limit = int(os.environ.get(name, "0"))
    if limit <= 0:
        return False
    counters = object_root.parent / "faults"
    counters.mkdir(exist_ok=True)
    counter = counters / (name + "-" + key.replace("/", "_"))
    with counter.open("a+", encoding="utf-8") as state:
        fcntl.flock(state.fileno(), fcntl.LOCK_EX)
        state.seek(0)
        raw = state.read()
        seen = int(raw) if raw else 0
        state.seek(0)
        state.truncate()
        state.write(str(seen + 1))
    return seen < limit

if command == "lsjson":
    path = object_path(arguments[0])
    if not path.is_file():
        # Some object-store backends represent a missing exact object as a
        # successful stat with a non-object JSON payload.
        print("null")
        raise SystemExit(0)
    print(json.dumps({"IsDir": False, "Path": path.name, "Size": path.stat().st_size}))
elif command == "cat":
    path = object_path(arguments[0])
    wait_for_peer("read", str(path.relative_to(object_root)))
    if path.is_file() and fault("FAKE_RCLONE_CAT_FAILS", path.name):
        raise SystemExit(1)
    # rclone cat succeeds with no output when its exact source object is absent.
    if path.is_file():
        sys.stdout.buffer.write(path.read_bytes())
elif command == "copyto":
    update_activity(1)
    try:
        time.sleep(float(os.environ.get("FAKE_RCLONE_COPY_DELAY", "0")))
        source = Path(arguments[-2])
        destination = object_path(arguments[-1])
        key = str(destination.relative_to(object_root))
        wait_for_peer("acceptance", key)
        wait_for_peer("endpoint", key)
        if "--immutable" in arguments and destination.exists():
            raise SystemExit(9)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if fault("FAKE_RCLONE_PARTIAL_COPIES", destination.name):
            destination.write_bytes(source.read_bytes()[:-1] + b"?")
            raise SystemExit(0)
        shutil.copyfile(source, destination)
        if fault("FAKE_RCLONE_NOT_IMPLEMENTED_COPIES", destination.name):
            print("NotImplemented: Not Implemented", file=sys.stderr)
            raise SystemExit(1)
    finally:
        update_activity(-1)
elif command == "deletefile":
    path = object_path(arguments[0])
    if not path.is_file():
        raise SystemExit(3)
    path.unlink()
else:
    raise SystemExit(64)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{executable_root}:{environment['PATH']}"
    environment["FAKE_RCLONE_ROOT"] = str(object_root)
    return environment, object_root


def test_rclone_publication_treats_empty_cat_as_missing_object(
    tmp_path: Path,
    publication: Path,
) -> None:
    environment, object_root = _fake_rclone_environment(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "publish-candidate",
            "--bundle",
            str(publication),
            "--rclone-remote",
            "r2:vonk-forge-installers",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads((publication / "publication-plan.json").read_text())
    immutable = [entry for entry in plan["objects"] if entry["phase"] == "immutable"]
    assert immutable
    for entry in immutable:
        published = object_root / entry["key"]
        assert published.is_file()
        assert hashlib.sha256(published.read_bytes()).hexdigest() == entry["sha256"]

    (object_root / "nas").write_bytes(b"legacy NAS endpoint\n")
    (object_root / "spark").write_bytes(b"legacy Spark endpoint\n")
    receipt, signature, public_key = _acceptance_receipt(tmp_path, publication)
    encoded_public_key = subprocess.run(
        ["openssl", "pkey", "-pubin", "-in", public_key, "-outform", "DER"],
        check=True,
        capture_output=True,
    ).stdout
    promoted = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "promote",
            "--bundle",
            str(publication),
            "--accepted-evidence",
            str(receipt),
            "--accepted-evidence-signature",
            str(signature),
            "--acceptance-public-key",
            str(public_key),
            "--acceptance-public-key-sha256",
            hashlib.sha256(encoded_public_key).hexdigest(),
            "--rclone-remote",
            "r2:vonk-forge-installers",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert promoted.returncode == 0, promoted.stderr
    assert (object_root / "nas").read_bytes() == (
        publication / "objects/nas"
    ).read_bytes()
    assert (object_root / "spark").read_bytes() == (
        publication / "objects/spark"
    ).read_bytes()
    assert (object_root / "artifacts/stable/current.manifest").is_file()


@pytest.mark.parametrize(
    "fault",
    [
        {"FAKE_RCLONE_NOT_IMPLEMENTED_COPIES": "1"},
        {"FAKE_RCLONE_CAT_FAILS": "2"},
        {"FAKE_RCLONE_PARTIAL_COPIES": "1"},
    ],
)
def test_rclone_candidate_publication_survives_transient_r2_faults(
    tmp_path: Path,
    fault: dict[str, str],
    publication: Path,
) -> None:
    environment, object_root = _fake_rclone_environment(tmp_path)
    environment["INSTALL_RELEASE_PUBLICATION_RETRY_DELAY"] = "0"
    environment.update(fault)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "publish-candidate",
            "--bundle",
            str(publication),
            "--rclone-remote",
            "r2:vonk-forge-installers",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads((publication / "publication-plan.json").read_text())
    for entry in plan["objects"]:
        if entry["phase"] == "immutable":
            published = object_root / entry["key"]
            assert hashlib.sha256(published.read_bytes()).hexdigest() == entry["sha256"]


def test_rclone_candidate_verification_stays_strict_when_object_never_matches(
    tmp_path: Path,
    publication: Path,
) -> None:
    environment, _ = _fake_rclone_environment(tmp_path)
    environment["INSTALL_RELEASE_PUBLICATION_RETRY_DELAY"] = "0"
    environment["FAKE_RCLONE_PARTIAL_COPIES"] = "100"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "publish-candidate",
            "--bundle",
            str(publication),
            "--rclone-remote",
            "r2:vonk-forge-installers",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "R2 verification failed" in result.stderr


def test_rclone_candidate_objects_publish_with_bounded_parallelism(
    tmp_path: Path,
    publication: Path,
) -> None:
    environment, _ = _fake_rclone_environment(tmp_path)
    activity = tmp_path / "rclone-activity.json"
    environment["FAKE_RCLONE_ACTIVITY"] = str(activity)
    environment["FAKE_RCLONE_COPY_DELAY"] = "0.5"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "publish-candidate",
            "--bundle",
            str(publication),
            "--rclone-remote",
            "r2:vonk-forge-installers",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(activity.read_text()) == {"active": 0, "maximum": 8}
    plan = json.loads((publication / "publication-plan.json").read_text())
    immutable_keys = [
        entry["key"] for entry in plan["objects"] if entry["phase"] == "immutable"
    ]
    receipt_keys = [json.loads(line)["key"] for line in result.stdout.splitlines()]
    assert receipt_keys == immutable_keys

    replay = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "publish-candidate",
            "--bundle",
            str(publication),
            "--rclone-remote",
            "r2:vonk-forge-installers",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert replay.returncode == 0, replay.stderr
    assert [json.loads(line)["key"] for line in replay.stdout.splitlines()] == (
        immutable_keys
    )


def test_parallel_rclone_candidate_conflict_is_fail_closed_without_receipt(
    tmp_path: Path,
    publication: Path,
) -> None:
    environment, object_root = _fake_rclone_environment(tmp_path)
    plan = json.loads((publication / "publication-plan.json").read_text())
    conflict = next(entry for entry in plan["objects"] if entry["phase"] == "immutable")
    conflict_path = object_root / conflict["key"]
    conflict_path.parent.mkdir(parents=True)
    conflict_path.write_bytes(b"conflicting immutable object\n")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "publish-candidate",
            "--bundle",
            str(publication),
            "--rclone-remote",
            "r2:vonk-forge-installers",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "refusing to overwrite immutable object" in result.stderr
    assert result.stdout == ""
    assert conflict_path.read_bytes() == b"conflicting immutable object\n"


def test_rclone_promotion_parallelizes_reads_and_phase_groups_before_pointer(
    tmp_path: Path,
    publication: Path,
) -> None:
    environment, object_root = _fake_rclone_environment(tmp_path)
    candidate = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "publish-candidate",
            "--bundle",
            str(publication),
            "--rclone-remote",
            "r2:vonk-forge-installers",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert candidate.returncode == 0, candidate.stderr
    receipt, signature, public_key = _acceptance_receipt(tmp_path, publication)
    encoded_public_key = subprocess.run(
        ["openssl", "pkey", "-pubin", "-in", public_key, "-outform", "DER"],
        check=True,
        capture_output=True,
    ).stdout
    barrier = tmp_path / "parallel-barrier"
    environment["FAKE_RCLONE_PARALLEL_BARRIER"] = str(barrier)

    promoted = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "promote",
            "--bundle",
            str(publication),
            "--accepted-evidence",
            str(receipt),
            "--accepted-evidence-signature",
            str(signature),
            "--acceptance-public-key",
            str(public_key),
            "--acceptance-public-key-sha256",
            hashlib.sha256(encoded_public_key).hexdigest(),
            "--rclone-remote",
            "r2:vonk-forge-installers",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert promoted.returncode == 0, promoted.stderr
    assert all(
        len(list((barrier / phase).iterdir())) >= 2
        for phase in (
            "read",
            "acceptance",
            "endpoint",
        )
    )
    operations = [json.loads(line) for line in promoted.stdout.splitlines()]
    assert [operation["phase"] for operation in operations[-4:]] == [
        "endpoint",
        "endpoint",
        "endpoint",
        "pointer",
    ]
    pointer = object_root / "artifacts/stable/current.manifest"
    assert pointer.is_file()
    plan = json.loads((publication / "publication-plan.json").read_text())
    acceptance = (
        object_root
        / f"artifacts/stable/releases/{plan['generation']}/acceptance/receipt.json"
    )
    assert acceptance.read_bytes() == receipt.read_bytes()
    assert (object_root / "nas").is_file()
    assert (object_root / "spark").is_file()


def test_promotion_refuses_receipt_for_another_generation_without_moving_channel(
    tmp_path: Path,
    publication: Path,
) -> None:
    destination = tmp_path / "public"
    assert _publish_candidate(publication, destination).returncode == 0
    receipt, signature, public_key = _acceptance_receipt(
        tmp_path, publication, generation="f" * 64
    )

    result = _promote(publication, destination, receipt, signature, public_key)

    assert result.returncode == 2
    assert "does not match the candidate generation" in result.stderr
    assert not (destination / "artifacts/stable/current.manifest").exists()


def test_promotion_publishes_signed_acceptance_receipt_before_channel_pointer(
    tmp_path: Path,
    publication: Path,
) -> None:
    destination = tmp_path / "public"
    candidate = _publish_candidate(publication, destination)
    assert candidate.returncode == 0, candidate.stderr
    receipt, signature, public_key = _acceptance_receipt(tmp_path, publication)

    result = _promote(publication, destination, receipt, signature, public_key)

    assert result.returncode == 0, result.stderr
    operations = [json.loads(line) for line in result.stdout.splitlines()]
    assert [operation["phase"] for operation in operations[-4:]] == [
        "endpoint",
        "endpoint",
        "endpoint",
        "pointer",
    ]
    assert operations[0]["phase"] == "acceptance"
    plan = json.loads((publication / "publication-plan.json").read_text())
    acceptance = (
        destination
        / f"artifacts/stable/releases/{plan['generation']}/acceptance/receipt.json"
    )
    assert acceptance.read_bytes() == receipt.read_bytes()
    assert (destination / "nas").is_file()
    assert (destination / "spark").is_file()
    assert (destination / "artifacts/stable/current.manifest").is_file()


def test_promotion_replays_existing_signed_acceptance_without_overwrite(
    tmp_path: Path,
    publication: Path,
) -> None:
    destination = tmp_path / "public"
    assert _publish_candidate(publication, destination).returncode == 0
    first_receipt, first_signature, first_public_key = _acceptance_receipt(
        tmp_path / "first", publication
    )
    first = _promote(
        publication, destination, first_receipt, first_signature, first_public_key
    )
    assert first.returncode == 0, first.stderr
    stored_receipt = destination / (
        "artifacts/stable/releases/"
        + json.loads((publication / "publication-plan.json").read_text())["generation"]
        + "/acceptance/receipt.json"
    )
    original = stored_receipt.read_bytes()

    second_receipt, second_signature, second_public_key = _acceptance_receipt(
        tmp_path / "second", publication
    )
    second = _promote(
        publication, destination, second_receipt, second_signature, second_public_key
    )

    assert second.returncode == 0, second.stderr
    assert stored_receipt.read_bytes() == original
    assert (destination / "artifacts/stable/current.manifest").is_file()


def test_rclone_promotion_replays_existing_signed_acceptance_without_overwrite(
    tmp_path: Path,
    publication: Path,
) -> None:
    environment, object_root = _fake_rclone_environment(tmp_path)
    candidate = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "publish-candidate",
            "--bundle",
            str(publication),
            "--rclone-remote",
            "r2:vonk-forge-installers",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert candidate.returncode == 0, candidate.stderr
    first = _acceptance_receipt(tmp_path / "first", publication)
    first_result = subprocess.run(
        _promote_command(
            publication,
            first[0],
            first[1],
            first[2],
            remote="r2:vonk-forge-installers",
        ),
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert first_result.returncode == 0, first_result.stderr
    plan = json.loads((publication / "publication-plan.json").read_text())
    stored_receipt = object_root / (
        f"artifacts/stable/releases/{plan['generation']}/acceptance/receipt.json"
    )
    original = stored_receipt.read_bytes()

    second = _acceptance_receipt(tmp_path / "second", publication)
    second_result = subprocess.run(
        _promote_command(
            publication,
            second[0],
            second[1],
            second[2],
            remote="r2:vonk-forge-installers",
        ),
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert second_result.returncode == 0, second_result.stderr
    assert stored_receipt.read_bytes() == original


def test_assemble_builds_complete_immutable_generation_and_final_pointer(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    publication = _assemble(tmp_path, inputs)
    plan = json.loads((publication / "publication-plan.json").read_text())

    assert plan["schema_version"] == 2
    assert plan["channel"] == "stable"
    phases = [entry["phase"] for entry in plan["objects"]]
    phase_order = {"immutable": 0, "endpoint": 1, "pointer": 2}
    assert phases == sorted(phases, key=lambda phase: phase_order[phase])
    assert phases[-1] == "pointer"
    assert phases.count("pointer") == 1
    keys = {entry["key"] for entry in plan["objects"]}
    assert any(
        key.endswith("/nas/current/" + Path(str(inputs["nas_wheel"])).name)
        for key in keys
    )
    for platform in SPARK_PLATFORMS:
        setup_key = next(
            key
            for key in keys
            if key.endswith(f"/spark/current/{platform}/vonk-spark-setup")
            and "/acceptance-baseline/" not in key
        )
        setup_signature_key = f"{setup_key}.sig"
        assert setup_signature_key in keys
        assert any(
            key.endswith(f"/spark/current/{platform}/vonk-forge-agent.deb")
            for key in keys
        )
        release = json.loads(
            next(
                (publication / "objects").glob(
                    f"artifacts/stable/releases/{plan['generation']}/release.json"
                )
            ).read_text()
        )
        signature = publication / "objects" / setup_signature_key
        signature_record = release["artifacts"][f"spark-setup-signature-{platform}"]
        assert signature_record == {
            "path": setup_signature_key,
            "sha256": hashlib.sha256(signature.read_bytes()).hexdigest(),
            "size": signature.stat().st_size,
        }
        raw_signature = tmp_path / f"{platform}.setup.raw.sig"
        raw_signature.write_bytes(
            base64.b64decode(signature.read_bytes().strip(), validate=True)
        )
        verification = subprocess.run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-verify",
                str(inputs["signing_public_key"]),
                "-signature",
                raw_signature,
                str(_input_mapping(inputs, "spark")[platform]),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert verification.returncode == 0, verification.stderr
    assert {
        entry["key"] for entry in plan["objects"] if entry["phase"] == "endpoint"
    } == {
        "nas",
        "spark",
        "vonkctl",
    }
    assert plan["objects"][-1]["key"] == "artifacts/stable/current.manifest"


def test_assemble_binds_lower_arm64_baseline_without_a_pointer(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    publication = _assemble(tmp_path, inputs)
    plan = json.loads((publication / "publication-plan.json").read_text())
    immutable = {
        entry["key"]: entry
        for entry in plan["objects"]
        if entry["phase"] == "immutable"
    }
    baseline_releases = [
        key for key in immutable if key.endswith("/acceptance-baseline/release.json")
    ]

    assert len(baseline_releases) == 1
    baseline_release_path = publication / "objects" / baseline_releases[0]
    baseline_release = json.loads(baseline_release_path.read_text())
    assert baseline_release["version"] == inputs["baseline_version"]
    assert baseline_release["acceptance_only"] is True
    candidate = json.loads(
        baseline_release_path.parent.parent.joinpath("release.json").read_text()
    )
    # The lower agent carries the same Controller, so its observer must consume
    # that Controller's signed, wheel-bundled contract too.
    assert (
        baseline_release["artifacts"]["cli-wheel"]
        == candidate["artifacts"]["cli-wheel"]
    )
    assert set(baseline_release["artifacts"]) >= {
        "agent-package-linux-arm64",
    }
    for record in baseline_release["artifacts"].values():
        path = publication / "objects" / record["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
        assert path.stat().st_size == record["size"]
    assert all(
        entry["phase"] == "immutable"
        for entry in plan["objects"]
        if "/acceptance-baseline/" in entry["key"]
    )
    ordered = subprocess.run(
        [
            "/usr/bin/dpkg",
            "--compare-versions",
            str(inputs["version"]),
            "gt",
            str(inputs["baseline_version"]),
        ],
        check=False,
    )
    assert ordered.returncode == 0


@pytest.mark.parametrize(
    "missing",
    ["spark-linux-arm64", "package-linux-arm64"],
)
def test_assemble_rejects_incomplete_platform_matrix(
    tmp_path: Path, missing: str
) -> None:
    inputs = _inputs(tmp_path)
    kind, platform = missing.split("-", 1)
    collection = _input_mapping(
        inputs,
        {"spark": "spark", "package": "packages"}[kind],
    )
    collection.pop(platform)

    result = subprocess.run(
        _assemble_command(tmp_path, inputs),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "platforms are incomplete" in result.stderr
    assert not (tmp_path / "publication").exists()


@pytest.mark.parametrize("preflight_only", [False, True])
def test_promotion_refuses_rejected_acceptance_evidence(
    tmp_path: Path,
    preflight_only: bool,
    publication: Path,
) -> None:
    destination = tmp_path / "public"
    assert _publish_candidate(publication, destination).returncode == 0
    receipt, signature, public_key = _acceptance_receipt(
        tmp_path / "acceptance", publication, status="rejected"
    )

    result = _promote(
        publication,
        destination,
        receipt,
        signature,
        public_key,
        preflight_only=preflight_only,
    )

    assert result.returncode == 2
    assert "evidence is not accepted" in result.stderr
    assert not (destination / "artifacts/stable/current.manifest").exists()


def test_assemble_refuses_an_expired_channel_manifest(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    command = _assemble_command(tmp_path, inputs)
    command[command.index("--expires-at") + 1] = "1"

    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode == 2
    assert "expiry is not in the future" in result.stderr


def test_assemble_rejects_package_metadata_from_another_release(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    wrong = _agent_package(tmp_path / "wrong", "linux-arm64", "1.2.2")
    renamed = tmp_path / "vonk-forge-agent_1.2.3_arm64.deb"
    renamed.write_bytes(wrong.read_bytes())
    _input_mapping(inputs, "packages")["linux-arm64"] = renamed

    result = subprocess.run(
        _assemble_command(tmp_path, inputs),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "package metadata is inconsistent" in result.stderr


@pytest.mark.parametrize("role", ["api", "worker", "litellm"])
def test_assemble_refuses_mutable_image_references(tmp_path: Path, role: str) -> None:
    inputs = _inputs(tmp_path)
    command = _assemble_command(tmp_path, inputs)
    option = f"--{role}-image"
    command[command.index(option) + 1] = (
        f"ghcr.io/carstvaartjes/vonk-forge-{role}:latest@sha256:{DIGEST}"
    )

    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode == 2
    assert "image reference is mutable" in result.stderr


# Assembles a full release graph; CI runners run it 3-4x slower than local.
@pytest.mark.slow(60)
def test_development_assembly_reuses_images_from_an_accepted_ancestor(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path, channel="dev")
    command = _assemble_command(tmp_path, inputs)
    images_source_sha = "c" * 40
    command[command.index("--images-source-sha") + 1] = images_source_sha
    for role in ("api", "worker", "litellm"):
        option = f"--{role}-image"
        command[command.index(option) + 1] = command[command.index(option) + 1].replace(
            f"dev-sha-{SOURCE_SHA}", f"dev-sha-{images_source_sha}"
        )

    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr


# Assembles and signs a whole release publication through the real script.
@pytest.mark.slow(60)
@pytest.mark.parametrize(
    "image_tag", [f"acceptance-{SOURCE_SHA}-123-1", "dev-sha-" + "c" * 40]
)
def test_development_assembly_preserves_digest_pins_independent_of_tag_provenance(
    tmp_path: Path, image_tag: str
) -> None:
    command = _assemble_command(tmp_path, _inputs(tmp_path, channel="dev"))
    references: dict[str, str] = {}
    for role in ("api", "worker", "litellm"):
        index = command.index(f"--{role}-image") + 1
        reference = (
            f"ghcr.io/carstvaartjes/vonk-forge-{role}:{image_tag}@sha256:{DIGEST}"
        )
        command[index] = reference
        references[role] = reference

    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    output = Path(command[command.index("--output") + 1])
    release_path = next(
        (output / "objects/artifacts/dev/releases").glob("*/release.json")
    )
    release = json.loads(release_path.read_text())
    for role, reference in references.items():
        assert release["images"][role] == reference


def test_promotion_writes_signed_atomic_manifest_after_acceptance_and_static_endpoints(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path, _inputs(tmp_path))
    destination = tmp_path / "public"

    result = _publish_accepted(publication, destination, tmp_path / "acceptance")

    assert result.returncode == 0, result.stderr
    operations = [json.loads(line) for line in result.stdout.splitlines()]
    assert operations[-1] == {
        "key": "artifacts/stable/current.manifest",
        "phase": "pointer",
    }
    assert all(item["phase"] == "acceptance" for item in operations[:-4])
    assert [item["phase"] for item in operations[-4:]] == [
        "endpoint",
        "endpoint",
        "endpoint",
        "pointer",
    ]
    pointer = destination / "artifacts/stable/current.manifest"
    lines = pointer.read_text().splitlines()
    assert lines[0] == "schema_version=2"
    assert lines[1] == "channel=stable"
    assert lines[-1].startswith("signature=")
    claims = tmp_path / "claims"
    signature = tmp_path / "signature"
    claims.write_text("\n".join(lines[:-1]) + "\n")
    signature.write_bytes(base64.b64decode(lines[-1].removeprefix("signature=")))
    verified = subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            publication / "channel-public-key.pem",
            "-signature",
            signature,
            claims,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    assert (destination / "nas").is_file()
    assert (destination / "spark").is_file()


def test_static_endpoints_do_not_change_between_release_generations(
    tmp_path: Path,
) -> None:
    first_inputs = _inputs(tmp_path / "first")
    first = _assemble(tmp_path / "first", first_inputs)
    second_root = tmp_path / "second"
    second_inputs = _inputs(second_root)
    second_inputs["signing_key"] = first_inputs["signing_key"]
    second_inputs["signing_public_key"] = first_inputs["signing_public_key"]
    _canonical(
        _input_path(second_inputs, "payload"),
        {
            "docker_compose_yaml": "services:\n  control-api:\n    image: replacement\n",
            "schema_version": 2,
        },
    )
    command = _assemble_command(second_root, second_inputs)
    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    second = second_root / "publication"
    assert (first / "objects/nas").read_bytes() == (second / "objects/nas").read_bytes()
    assert (first / "objects/spark").read_bytes() == (
        second / "objects/spark"
    ).read_bytes()
    assert (first / "objects/vonkctl").read_bytes() == (
        second / "objects/vonkctl"
    ).read_bytes()


def test_dev_promotion_serves_unqualified_endpoints_until_stable_exists(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "dev", _inputs(tmp_path / "dev", channel="dev"))
    destination = tmp_path / "public"

    result = _publish_accepted(publication, destination, tmp_path / "dev-acceptance")

    assert result.returncode == 0, result.stderr
    for name in ("nas", "spark", "vonkctl"):
        assert (destination / name).read_bytes() == (
            destination / "dev" / name
        ).read_bytes()
    assert b"channel='dev'" in (destination / "nas").read_bytes()
    operations = [json.loads(line) for line in result.stdout.splitlines()]
    assert [item["key"] for item in operations if item["phase"] == "endpoint"][3:] == [
        "nas",
        "spark",
        "vonkctl",
    ]


def test_dev_promotion_never_replaces_unqualified_endpoints_once_stable_exists(
    tmp_path: Path,
    channel_publications: tuple[Path, Path],
) -> None:
    destination = tmp_path / "public"
    stable, dev = channel_publications
    first = _publish_accepted(stable, destination, tmp_path / "stable-acceptance")
    assert first.returncode == 0, first.stderr
    stable_nas = (destination / "nas").read_bytes()
    assert b"channel='stable'" in stable_nas

    second = _publish_accepted(dev, destination, tmp_path / "dev-acceptance")

    assert second.returncode == 0, second.stderr
    assert (destination / "nas").read_bytes() == stable_nas
    assert b"channel='dev'" in (destination / "dev/nas").read_bytes()


def test_stable_promotion_replaces_the_development_fallback_endpoints(
    tmp_path: Path,
    channel_publications: tuple[Path, Path],
) -> None:
    destination = tmp_path / "public"
    stable, dev = channel_publications
    published = _publish_accepted(dev, destination, tmp_path / "dev-acceptance")
    assert published.returncode == 0, published.stderr
    assert b"channel='dev'" in (destination / "nas").read_bytes()

    result = _publish_accepted(stable, destination, tmp_path / "stable-acceptance")

    assert result.returncode == 0, result.stderr
    assert b"channel='stable'" in (destination / "nas").read_bytes()


@pytest.mark.parametrize("preflight_only", [False, True])
def test_stable_publication_refuses_version_rollback_before_writing(
    tmp_path: Path,
    preflight_only: bool,
    version_publications: tuple[Path, Path],
) -> None:
    newest, older = version_publications
    destination = tmp_path / "public"
    first = _publish_accepted(newest, destination, tmp_path / "newest-acceptance")
    assert first.returncode == 0, first.stderr

    candidate = _publish_candidate(older, destination)
    assert candidate.returncode == 0, candidate.stderr
    receipt, signature, public_key = _acceptance_receipt(
        tmp_path / "older-acceptance", older
    )
    before = (destination / "artifacts/stable/current.manifest").read_bytes()
    rollback = _promote(
        older,
        destination,
        receipt,
        signature,
        public_key,
        preflight_only=preflight_only,
    )

    assert rollback.returncode == 2
    assert "refusing to regress" in rollback.stderr
    assert (destination / "artifacts/stable/current.manifest").read_bytes() == before


def test_refresh_extends_signed_manifest_after_verifying_all_release_objects(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path / "inputs")
    publication = _assemble(tmp_path / "inputs", inputs)
    destination = tmp_path / "public"
    published = _publish_accepted(publication, destination, tmp_path / "acceptance")
    assert published.returncode == 0, published.stderr
    manifest = destination / "artifacts/stable/current.manifest"
    before = manifest.read_text().splitlines()
    expires_at = int(time.time()) + 14 * 24 * 60 * 60

    refreshed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "refresh",
            "--channel",
            "stable",
            "--expires-at",
            str(expires_at),
            "--signing-key",
            str(inputs["signing_key"]),
            "--signing-public-key",
            str(inputs["signing_public_key"]),
            "--filesystem",
            str(destination),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert refreshed.returncode == 0, refreshed.stderr
    after = manifest.read_text().splitlines()
    assert after[:5] == before[:5]
    assert after[5] == f"expires_at={expires_at}"
    assert after[6:14] == before[6:14]
    assert after[14] != before[14]
    assert json.loads(refreshed.stdout) == {
        "channel": "stable",
        "phase": "pointer",
        "refreshed": True,
    }
