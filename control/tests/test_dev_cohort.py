from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from vonk_control import dev_cohort
from vonk_control.dev_cohort import (
    DEVELOPMENT_IMAGE_IDENTITY_PATH,
    DEVELOPMENT_SOURCE_REPOSITORY,
    DevelopmentCohort,
    DevelopmentCohortError,
    DevelopmentImageIdentity,
    SelectedDevelopmentCohort,
    build_identity,
    canonical_json,
    read_identity,
    verify_cohort,
)

COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER_COMMIT = "89abcdef0123456789abcdef0123456789abcdef"
SCHEMA_VERSION = 1


def test_development_database_revision_is_the_exact_single_alembic_head() -> None:
    control_root = Path(__file__).resolve().parents[1]
    config = Config(control_root / "alembic.ini")
    config.set_main_option("script_location", str(control_root / "migrations"))
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["0027_execution_harness_catalog"]
    assert dev_cohort.DEVELOPMENT_DATABASE_REVISION == script.get_heads()[0]


def _canonical_for_expected(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )


def _expected_build_digest(source_commit: str = COMMIT) -> str:
    common = {
        "channel": "development",
        "database_revision": "0027_execution_harness_catalog",
        "platform_version": "0.1.0",
        "protocol_maximum": 3,
        "protocol_minimum": 1,
        "schema_version": SCHEMA_VERSION,
        "source_commit": source_commit,
        "source_repository": DEVELOPMENT_SOURCE_REPOSITORY,
    }
    return "sha256:" + hashlib.sha256(_canonical_for_expected(common)).hexdigest()


def _identity_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "source_repository": DEVELOPMENT_SOURCE_REPOSITORY,
        "source_commit": COMMIT,
        "channel": "development",
        "platform_version": "0.1.0",
        "build_digest": _expected_build_digest(),
        "database_revision": "0027_execution_harness_catalog",
        "protocol_minimum": 1,
        "protocol_maximum": 3,
        "image_role": "api",
    }
    document.update(overrides)
    return document


def _identity_bytes(**overrides: object) -> bytes:
    return canonical_json(_identity_document(**overrides))


@pytest.mark.parametrize("non_finite", (float("nan"), float("inf"), float("-inf")))
def test_canonical_json_rejects_non_finite_numbers(non_finite: float) -> None:
    with pytest.raises(DevelopmentCohortError, match="not canonical JSON"):
        canonical_json({"value": non_finite})


def test_identity_parser_accepts_only_the_canonical_development_identity() -> None:
    raw = _identity_bytes()

    identity = DevelopmentImageIdentity.from_bytes(raw, expected_role="api")

    assert identity == DevelopmentImageIdentity(
        schema_version=SCHEMA_VERSION,
        source_repository=DEVELOPMENT_SOURCE_REPOSITORY,
        source_commit=COMMIT,
        channel="development",
        platform_version="0.1.0",
        build_digest=_expected_build_digest(),
        database_revision="0027_execution_harness_catalog",
        protocol_minimum=1,
        protocol_maximum=3,
        image_role="api",
    )
    assert identity.to_bytes() == raw


@pytest.mark.parametrize(
    ("label", "raw"),
    (
        ("malformed", b"{"),
        ("not an object", b"[]\n"),
        (
            "duplicate field",
            b'{"build_digest":"sha256:'
            + b"a" * 64
            + b'","build_digest":"sha256:'
            + b"b" * 64
            + b'","channel":"development","database_revision":"0027_execution_harness_catalog",'
            + b'"image_role":"api","platform_version":"0.1.0","protocol_maximum":3,'
            + b'"protocol_minimum":1,"schema_version":1,"source_commit":"'
            + COMMIT.encode("ascii")
            + b'","source_repository":"'
            + DEVELOPMENT_SOURCE_REPOSITORY.encode("ascii")
            + b'"}\n',
        ),
        (
            "non canonical whitespace",
            json.dumps(_identity_document(), sort_keys=True).encode("ascii"),
        ),
        (
            "non canonical key order",
            b'{"schema_version":1,"source_repository":"'
            + DEVELOPMENT_SOURCE_REPOSITORY.encode("ascii")
            + b'","source_commit":"'
            + COMMIT.encode("ascii")
            + b'","channel":"development","platform_version":"0.1.0","build_digest":"sha256:'
            + b"a" * 64
            + b'","database_revision":"0027_execution_harness_catalog","protocol_minimum":1,'
            + b'"protocol_maximum":3,"image_role":"api"}\n',
        ),
        ("oversized", b" " * 17000),
        ("hostile integer", b'{"schema_version":' + b"1" * 5000 + b"}\n"),
    ),
)
def test_identity_parser_rejects_untrusted_json_encodings(
    label: str, raw: bytes
) -> None:
    with pytest.raises(DevelopmentCohortError, match="identity"):
        DevelopmentImageIdentity.from_bytes(raw, expected_role="api")


@pytest.mark.parametrize(
    "overrides",
    (
        {"unknown": "value"},
        {"source_repository": "https://github.com/CarstVaartjes/vonk-forge.git"},
        {"source_repository": "https://github.com/evil/vonk-forge"},
        {"source_commit": COMMIT.upper()},
        {"source_commit": "1234"},
        {"channel": "latest"},
        {"platform_version": "v0.1.0"},
        {"platform_version": "0.1.1"},
        {"build_digest": "sha256:" + "A" * 64},
        {"build_digest": "not-a-digest"},
        {"build_digest": "sha256:" + "a" * 64},
        {"database_revision": ""},
        {"database_revision": "0021_next_revision"},
        {"protocol_minimum": 0},
        {"protocol_minimum": 3, "protocol_maximum": 2},
        {"protocol_minimum": 1, "protocol_maximum": 2},
        {"protocol_maximum": 1000},
        {"image_role": "scheduler"},
    ),
)
def test_identity_parser_rejects_values_that_break_the_development_trust_boundary(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(DevelopmentCohortError):
        DevelopmentImageIdentity.from_bytes(
            _identity_bytes(**overrides), expected_role="api"
        )


def test_identity_parser_rejects_missing_required_fields() -> None:
    document = _identity_document()
    del document["source_commit"]

    with pytest.raises(DevelopmentCohortError, match="missing"):
        DevelopmentImageIdentity.from_bytes(
            canonical_json(document), expected_role="api"
        )


def test_identity_parser_binds_the_expected_role_to_the_reader() -> None:
    with pytest.raises(DevelopmentCohortError, match="role"):
        DevelopmentImageIdentity.from_bytes(
            _identity_bytes(image_role="worker"), expected_role="api"
        )


def test_build_identity_uses_fixed_development_values_and_one_cohort_digest() -> None:
    api = build_identity(role="api", source_commit=COMMIT)
    worker = build_identity(role="worker", source_commit=COMMIT)

    assert api.source_repository == DEVELOPMENT_SOURCE_REPOSITORY
    assert api.source_commit == COMMIT
    assert api.channel == "development"
    assert api.platform_version == "0.1.0"
    assert api.database_revision == "0027_execution_harness_catalog"
    assert api.protocol_minimum == 1
    assert api.protocol_maximum == 3
    assert api.image_role == "api"
    assert worker.image_role == "worker"
    assert api.build_digest == worker.build_digest
    assert api.to_bytes() == canonical_json(json.loads(api.to_bytes()))
    assert worker.to_bytes() == canonical_json(json.loads(worker.to_bytes()))


def test_read_identity_uses_the_fixed_embedded_path_and_rejects_symlinked_input(
    tmp_path: Path,
) -> None:
    assert DEVELOPMENT_IMAGE_IDENTITY_PATH == Path(
        "/usr/local/share/vonk-forge/development-image-identity.json"
    )
    target = tmp_path / "identity.json"
    target.write_bytes(_identity_bytes())
    symlink = tmp_path / "identity-link.json"
    symlink.symlink_to(target)

    assert read_identity(target, expected_role="api").source_commit == COMMIT
    with pytest.raises(DevelopmentCohortError, match="unsafe"):
        read_identity(symlink, expected_role="api")


def test_identity_model_cannot_be_mutated_into_an_invalid_public_identity() -> None:
    identity = DevelopmentImageIdentity.from_bytes(
        _identity_bytes(), expected_role="api"
    )

    with pytest.raises(DevelopmentCohortError):
        replace(identity, source_commit=OTHER_COMMIT.upper())


def _unsafe_identity(
    identity: DevelopmentImageIdentity, **overrides: object
) -> DevelopmentImageIdentity:
    clone = object.__new__(DevelopmentImageIdentity)
    values = identity.to_document()
    values.update(overrides)
    for key, value in values.items():
        object.__setattr__(clone, key, value)
    return clone


def test_verify_cohort_rejects_duplicate_or_missing_development_roles() -> None:
    api = build_identity(role="api", source_commit=COMMIT)
    worker = build_identity(role="worker", source_commit=COMMIT)

    with pytest.raises(DevelopmentCohortError, match="roles"):
        verify_cohort([api])
    with pytest.raises(DevelopmentCohortError, match="roles"):
        verify_cohort([api, api])
    with pytest.raises(DevelopmentCohortError, match="roles"):
        verify_cohort([api, worker, worker])


@pytest.mark.parametrize(
    "overrides",
    (
        {"source_commit": OTHER_COMMIT},
        {"build_digest": "sha256:" + "f" * 64},
        {"platform_version": "0.1.1"},
        {"database_revision": "0021_next_revision"},
        {"protocol_minimum": 2},
        {"protocol_maximum": 2},
        {"source_repository": "https://github.com/CarstVaartjes/vonk-forge-local"},
        {"channel": "development-local"},
    ),
)
def test_verify_cohort_rejects_any_mismatch_in_common_identity_metadata(
    overrides: dict[str, object],
) -> None:
    api = build_identity(role="api", source_commit=COMMIT)
    worker = _unsafe_identity(
        build_identity(role="worker", source_commit=COMMIT), **overrides
    )

    with pytest.raises(DevelopmentCohortError, match="cohort"):
        verify_cohort([api, worker])


def test_matching_identities_produce_one_canonical_selected_cohort_document() -> None:
    api = build_identity(role="api", source_commit=COMMIT)
    worker = build_identity(role="worker", source_commit=COMMIT)
    expected_api_identity_digest = (
        "sha256:" + hashlib.sha256(api.to_bytes()).hexdigest()
    )
    expected_worker_identity_digest = (
        "sha256:" + hashlib.sha256(worker.to_bytes()).hexdigest()
    )
    expected_common = {
        "build_digest": api.build_digest,
        "channel": "development",
        "database_revision": "0027_execution_harness_catalog",
        "platform_version": "0.1.0",
        "protocol_maximum": 3,
        "protocol_minimum": 1,
        "schema_version": SCHEMA_VERSION,
        "source_commit": COMMIT,
        "source_repository": DEVELOPMENT_SOURCE_REPOSITORY,
    }
    expected_release_digest = (
        "sha256:"
        + hashlib.sha256(
            (
                json.dumps(expected_common, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("ascii")
        ).hexdigest()
    )
    expected_generation_seed = {
        "api_identity_digest": expected_api_identity_digest,
        "common": expected_common,
        "worker_identity_digest": expected_worker_identity_digest,
    }
    expected_generation_hash = hashlib.sha256(
        (
            json.dumps(expected_generation_seed, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("ascii")
    ).hexdigest()

    selected = verify_cohort([worker, api])

    assert selected == SelectedDevelopmentCohort(
        schema_version=SCHEMA_VERSION,
        source_repository=DEVELOPMENT_SOURCE_REPOSITORY,
        source_commit=COMMIT,
        channel="development",
        platform_version="0.1.0",
        database_revision="0027_execution_harness_catalog",
        protocol_minimum=1,
        protocol_maximum=3,
        build_digest=api.build_digest,
        release_digest=expected_release_digest,
        api_identity_digest=expected_api_identity_digest,
        worker_identity_digest=expected_worker_identity_digest,
        api_image=f"development.invalid/vonk-forge-api@{expected_api_identity_digest}",
        worker_image=f"development.invalid/vonk-forge-worker@{expected_worker_identity_digest}",
        generation_id="gen-" + expected_release_digest.removeprefix("sha256:")[:24],
        start_nonce=hashlib.sha256(
            f"vonk-forge:development-start:{expected_generation_hash}".encode("ascii")
        ).hexdigest(),
    )
    assert selected.to_document() == {
        **expected_common,
        "release_digest": expected_release_digest,
        "api_identity_digest": expected_api_identity_digest,
        "worker_identity_digest": expected_worker_identity_digest,
        "api_image": f"development.invalid/vonk-forge-api@{expected_api_identity_digest}",
        "worker_image": f"development.invalid/vonk-forge-worker@{expected_worker_identity_digest}",
        "generation_id": "gen-" + expected_release_digest.removeprefix("sha256:")[:24],
        "start_nonce": hashlib.sha256(
            f"vonk-forge:development-start:{expected_generation_hash}".encode("ascii")
        ).hexdigest(),
    }
    assert selected.to_bytes() == canonical_json(selected.to_document())
    assert (
        DevelopmentCohort(api=api, worker=worker).common_document() == expected_common
    )


def test_selected_cohort_round_trips_and_rejects_tampered_canonical_documents() -> None:
    selected = verify_cohort(
        [
            build_identity(role="api", source_commit=COMMIT),
            build_identity(role="worker", source_commit=COMMIT),
        ]
    )
    document = selected.to_document()
    tampered = dict(document, source_commit=OTHER_COMMIT)

    assert SelectedDevelopmentCohort.from_bytes(selected.to_bytes()) == selected
    with pytest.raises(DevelopmentCohortError, match="selected"):
        SelectedDevelopmentCohort.from_bytes(canonical_json(tampered))
    with pytest.raises(DevelopmentCohortError, match="canonical"):
        SelectedDevelopmentCohort.from_bytes(
            json.dumps(document, sort_keys=True).encode("ascii")
        )


def test_selected_cohort_rejects_coordinated_identity_digest_tampering() -> None:
    selected = verify_cohort(
        [
            build_identity(role="api", source_commit=COMMIT),
            build_identity(role="worker", source_commit=COMMIT),
        ]
    )
    document = selected.to_document()
    common = {
        "build_digest": document["build_digest"],
        "channel": document["channel"],
        "database_revision": document["database_revision"],
        "platform_version": document["platform_version"],
        "protocol_maximum": document["protocol_maximum"],
        "protocol_minimum": document["protocol_minimum"],
        "schema_version": document["schema_version"],
        "source_commit": document["source_commit"],
        "source_repository": document["source_repository"],
    }
    forged_api_digest = "sha256:" + "f" * 64
    forged_generation_seed = {
        "api_identity_digest": forged_api_digest,
        "common": common,
        "worker_identity_digest": document["worker_identity_digest"],
    }
    forged_generation_hash = hashlib.sha256(
        _canonical_for_expected(forged_generation_seed)
    ).hexdigest()
    tampered = {
        **document,
        "api_identity_digest": forged_api_digest,
        "api_image": f"development.invalid/vonk-forge-api@{forged_api_digest}",
        "start_nonce": hashlib.sha256(
            f"vonk-forge:development-start:{forged_generation_hash}".encode("ascii")
        ).hexdigest(),
    }

    with pytest.raises(DevelopmentCohortError, match="selected"):
        SelectedDevelopmentCohort.from_bytes(canonical_json(tampered))


def _embedded_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    role: str,
    source_commit: str = COMMIT,
) -> Path:
    path = tmp_path / f"embedded-{role}-{source_commit}.json"
    path.write_bytes(build_identity(role=role, source_commit=source_commit).to_bytes())
    path.chmod(0o444)
    monkeypatch.setattr(dev_cohort, "DEVELOPMENT_IMAGE_IDENTITY_PATH", path)
    return path


def _allow_test_reset(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    ownership: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        os,
        "fchown",
        lambda _descriptor, uid, gid: ownership.append((uid, gid)),
    )
    return ownership


def test_reset_cohort_root_is_root_only_and_requires_a_normalized_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cohort"
    root.mkdir()
    monkeypatch.setattr(os, "geteuid", lambda: 10001)

    with pytest.raises(DevelopmentCohortError, match="root"):
        dev_cohort.reset_cohort_root(root)

    _allow_test_reset(monkeypatch)
    with pytest.raises(DevelopmentCohortError, match="absolute"):
        dev_cohort.reset_cohort_root(Path("cohort"))
    with pytest.raises(DevelopmentCohortError, match="normalized"):
        dev_cohort.reset_cohort_root(Path(f"{root}/../cohort"))


def test_reset_cohort_root_clears_only_safe_lifecycle_files_and_sets_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cohort"
    root.mkdir(mode=0o755)
    for name in ("api.json", "worker.json", "selected.json"):
        (root / name).write_text("old\n", encoding="utf-8")
    ownership = _allow_test_reset(monkeypatch)

    dev_cohort.reset_cohort_root(root)

    assert list(root.iterdir()) == []
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert ownership == [(10001, 10001)]


@pytest.mark.parametrize("unsafe_kind", ("symlink", "directory", "unexpected"))
def test_reset_cohort_root_rejects_unsafe_volume_contents_without_following_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_kind: str,
) -> None:
    root = tmp_path / "cohort"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("preserve\n", encoding="utf-8")
    if unsafe_kind == "symlink":
        (root / "api.json").symlink_to(outside)
    elif unsafe_kind == "directory":
        (root / "selected.json").mkdir()
    else:
        (root / "operator-owned").write_text("preserve\n", encoding="utf-8")
    _allow_test_reset(monkeypatch)

    with pytest.raises(DevelopmentCohortError, match="unsafe"):
        dev_cohort.reset_cohort_root(root)

    assert outside.read_text(encoding="utf-8") == "preserve\n"
    assert len(list(root.iterdir())) == 1


def test_reset_cohort_root_rejects_symlink_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "cohort"
    link.symlink_to(actual, target_is_directory=True)
    _allow_test_reset(monkeypatch)

    with pytest.raises(DevelopmentCohortError, match="unsafe"):
        dev_cohort.reset_cohort_root(link)


def test_report_identity_publishes_only_the_verified_role_and_refuses_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cohort"
    root.mkdir(mode=0o700)
    identity_path = _embedded_identity(monkeypatch, tmp_path, role="api")

    reported = dev_cohort.report_identity(root, "api")

    target = root / "api.json"
    assert reported == read_identity(identity_path, expected_role="api")
    assert target.read_bytes() == identity_path.read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o444
    assert {path.name for path in root.iterdir()} == {"api.json"}
    with pytest.raises(DevelopmentCohortError, match="exists"):
        dev_cohort.report_identity(root, "api")

    target.unlink()
    outside = tmp_path / "outside-report"
    outside.write_text("preserve\n", encoding="utf-8")
    target.symlink_to(outside)
    with pytest.raises(DevelopmentCohortError, match="exists"):
        dev_cohort.report_identity(root, "api")
    assert outside.read_text(encoding="utf-8") == "preserve\n"


def test_report_identity_rejects_an_embedded_identity_for_another_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cohort"
    root.mkdir(mode=0o700)
    _embedded_identity(monkeypatch, tmp_path, role="worker")

    with pytest.raises(DevelopmentCohortError, match="role"):
        dev_cohort.report_identity(root, "api")
    assert list(root.iterdir()) == []


def _write_report(root: Path, role: str, source_commit: str = COMMIT) -> None:
    path = root / f"{role}.json"
    path.write_bytes(build_identity(role=role, source_commit=source_commit).to_bytes())
    path.chmod(0o444)


def test_select_cohort_requires_exact_reports_and_atomically_publishes_read_only_selection(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cohort"
    root.mkdir(mode=0o700)
    _write_report(root, "api")
    _write_report(root, "worker")

    selected = dev_cohort.select_cohort(root)

    target = root / "selected.json"
    assert target.read_bytes() == selected.to_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o444
    assert {path.name for path in root.iterdir()} == {
        "api.json",
        "worker.json",
        "selected.json",
    }


@pytest.mark.parametrize("failure", ("missing", "extra", "symlink", "oversized"))
def test_select_cohort_rejects_incomplete_or_unsafe_report_sets(
    tmp_path: Path, failure: str
) -> None:
    root = tmp_path / "cohort"
    root.mkdir(mode=0o700)
    _write_report(root, "api")
    _write_report(root, "worker")
    if failure == "missing":
        (root / "worker.json").unlink()
    elif failure == "extra":
        (root / "unexpected.json").write_text("{}\n", encoding="utf-8")
    elif failure == "symlink":
        (root / "worker.json").unlink()
        (root / "worker.json").symlink_to(root / "api.json")
    else:
        (root / "worker.json").chmod(0o644)
        (root / "worker.json").write_bytes(b"x" * (64 * 1024 + 1))

    with pytest.raises(DevelopmentCohortError):
        dev_cohort.select_cohort(root)
    assert not (root / "selected.json").exists()


def test_select_cohort_rejects_a_report_that_changes_while_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cohort"
    root.mkdir(mode=0o700)
    _write_report(root, "api")
    _write_report(root, "worker")
    (root / "api.json").chmod(0o644)
    real_read = os.read
    changed = False

    def changing_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        chunk = real_read(descriptor, size)
        if chunk and not changed:
            changed = True
            with (root / "api.json").open("ab") as stream:
                stream.write(b"x")
        return chunk

    monkeypatch.setattr(os, "read", changing_read)

    with pytest.raises(DevelopmentCohortError, match="changed"):
        dev_cohort.select_cohort(root)
    assert not (root / "selected.json").exists()


def test_require_selected_cohort_binds_selection_to_the_current_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cohort"
    root.mkdir(mode=0o700)
    _write_report(root, "api")
    _write_report(root, "worker")
    selected = dev_cohort.select_cohort(root)
    _embedded_identity(monkeypatch, tmp_path, role="api")

    assert dev_cohort.require_selected_cohort(root / "selected.json", "api") == selected

    _embedded_identity(
        monkeypatch,
        tmp_path,
        role="api",
        source_commit=OTHER_COMMIT,
    )
    with pytest.raises(DevelopmentCohortError, match="current image"):
        dev_cohort.require_selected_cohort(root / "selected.json", "api")


def test_run_selected_cli_executes_only_after_current_image_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cohort"
    root.mkdir(mode=0o700)
    _write_report(root, "api")
    _write_report(root, "worker")
    dev_cohort.select_cohort(root)
    monkeypatch.setenv("VONK_DEV_SELECTED_COHORT_FILE", str(root / "selected.json"))
    executed: list[tuple[str, list[str]]] = []

    def capture_exec(file: str, arguments: list[str]) -> None:
        executed.append((file, arguments))
        raise RuntimeError("process replacement captured")

    monkeypatch.setattr(os, "execvp", capture_exec)
    _embedded_identity(
        monkeypatch,
        tmp_path,
        role="api",
        source_commit=OTHER_COMMIT,
    )
    with pytest.raises(DevelopmentCohortError, match="current image"):
        dev_cohort.main(["run-selected", "--role", "api", "--", "echo", "ready"])
    assert executed == []

    _embedded_identity(monkeypatch, tmp_path, role="api")
    with pytest.raises(RuntimeError, match="captured"):
        dev_cohort.main(["run-selected", "--role", "api", "--", "echo", "ready"])
    assert executed == [("echo", ["echo", "ready"])]


def test_cli_exposes_build_reset_report_and_verify_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    assert (
        dev_cohort.main(["build-identity", "--role", "api", "--source-commit", COMMIT])
        == 0
    )
    assert (
        capsysbinary.readouterr().out
        == build_identity(role="api", source_commit=COMMIT).to_bytes()
    )

    root = tmp_path / "cohort"
    root.mkdir()
    _allow_test_reset(monkeypatch)
    assert dev_cohort.main(["reset", str(root)]) == 0

    _embedded_identity(monkeypatch, tmp_path, role="api")
    assert dev_cohort.main(["report", str(root), "--role", "api"]) == 0
    _embedded_identity(monkeypatch, tmp_path, role="worker")
    assert dev_cohort.main(["report", str(root), "--role", "worker"]) == 0
    assert dev_cohort.main(["verify", str(root)]) == 0
    assert (root / "selected.json").is_file()
