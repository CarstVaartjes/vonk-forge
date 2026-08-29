import hashlib
import http.server
import io
import json
import os
import ssl
import subprocess
import tarfile
import threading
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts/dev-image-publication-capsule"
MEDIA_INDEX = "application/vnd.oci.image.index.v1+json"
MEDIA_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MEDIA_CONFIG = "application/vnd.oci.image.config.v1+json"
MEDIA_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"


def canonical(document: object) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def content_digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def add_blob(layout: Path, content: bytes) -> dict[str, object]:
    digest = content_digest(content)
    path = layout / "blobs" / "sha256" / digest.removeprefix("sha256:")
    path.write_bytes(content)
    return {"digest": digest, "size": len(content)}


def build_capsule(
    tmp_path: Path, *, variant: str = "accepted"
) -> tuple[Path, str, set[str]]:
    layout = tmp_path / "layout"
    (layout / "blobs" / "sha256").mkdir(parents=True)
    (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')

    root_descriptors: list[dict[str, Any]] = []
    runnable_digests: dict[str, str] = {}
    external_digests: set[str] = set()
    for architecture, marker in (("amd64", b"a"), ("arm64", b"b")):
        config = add_blob(
            layout,
            canonical(
                {"architecture": architecture, "os": "linux", "variant": variant}
            ),
        )
        large_layer = add_blob(layout, marker * (1024 * 1024 + 17))
        small_layer = add_blob(layout, marker * 257)
        external_digests.add(str(large_layer["digest"]))
        manifest = canonical(
            {
                "schemaVersion": 2,
                "mediaType": MEDIA_MANIFEST,
                "config": {"mediaType": MEDIA_CONFIG, **config},
                "layers": [
                    {"mediaType": MEDIA_LAYER, **large_layer},
                    {"mediaType": MEDIA_LAYER, **small_layer},
                ],
            }
        )
        manifest_blob = add_blob(layout, manifest)
        runnable_digests[architecture] = str(manifest_blob["digest"])
        root_descriptors.append(
            {
                "mediaType": MEDIA_MANIFEST,
                **manifest_blob,
                "platform": {"architecture": architecture, "os": "linux"},
            }
        )

    for architecture, marker in (("amd64", b"c"), ("arm64", b"d")):
        config = add_blob(layout, b"{}")
        statement = add_blob(
            layout,
            canonical(
                {
                    "predicateType": "https://spdx.dev/Document",
                    "subject": architecture,
                }
            )
            + marker,
        )
        manifest = canonical(
            {
                "schemaVersion": 2,
                "mediaType": MEDIA_MANIFEST,
                "config": {"mediaType": MEDIA_CONFIG, **config},
                "layers": [
                    {
                        "mediaType": "application/vnd.in-toto+json",
                        **statement,
                    }
                ],
            }
        )
        manifest_blob = add_blob(layout, manifest)
        root_descriptors.append(
            {
                "mediaType": MEDIA_MANIFEST,
                **manifest_blob,
                "platform": {"architecture": "unknown", "os": "unknown"},
                "annotations": {
                    "vnd.docker.reference.type": "attestation-manifest",
                    "vnd.docker.reference.digest": runnable_digests[architecture],
                },
            }
        )

    root_content = canonical(
        {
            "schemaVersion": 2,
            "mediaType": MEDIA_INDEX,
            "manifests": root_descriptors,
        }
    )
    root_blob = add_blob(layout, root_content)
    (layout / "index.json").write_bytes(
        canonical(
            {
                "schemaVersion": 2,
                "mediaType": MEDIA_INDEX,
                "manifests": [{"mediaType": MEDIA_INDEX, **root_blob}],
            }
        )
    )
    capsule = tmp_path / "vonk-forge-hermes.publication-capsule.tar"
    result = subprocess.run(
        [TOOL, "create", layout, capsule],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return capsule, str(root_blob["digest"]), external_digests


def verify(capsule: Path, digest: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [TOOL, "verify", capsule, digest],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )


def test_capsule_omits_only_large_runnable_layers_and_binds_exact_root(
    tmp_path: Path,
) -> None:
    capsule, root_digest, external_digests = build_capsule(tmp_path)

    result = verify(capsule, root_digest)

    assert result.returncode == 0, result.stderr
    with tarfile.open(capsule, "r:") as stream:
        names = {member.name for member in stream.getmembers()}
        metadata = json.loads(stream.extractfile("capsule.json").read())
    assert {entry["digest"] for entry in metadata["external_blobs"]} == (
        external_digests
    )
    for digest in external_digests:
        assert f"blobs/sha256/{digest.removeprefix('sha256:')}" not in names
    assert capsule.stat().st_size < 100_000
    assert verify(capsule, f"sha256:{'f' * 64}").returncode != 0


def test_capsule_verification_rejects_tampered_included_blob(tmp_path: Path) -> None:
    capsule, root_digest, _ = build_capsule(tmp_path)
    tampered = tmp_path / "tampered.tar"
    with tarfile.open(capsule, "r:") as source, tarfile.open(tampered, "w") as target:
        changed = False
        for member in source.getmembers():
            content = source.extractfile(member).read()
            if not changed and member.name.startswith("blobs/sha256/"):
                content = bytes([content[0] ^ 1]) + content[1:]
                changed = True
            replacement = tarfile.TarInfo(member.name)
            replacement.mode = member.mode
            replacement.size = len(content)
            target.addfile(replacement, io.BytesIO(content))

    result = verify(tampered, root_digest)

    assert result.returncode != 0
    assert "capsule blob digest does not match its name" in result.stderr


def test_capsule_verification_rejects_links(tmp_path: Path) -> None:
    capsule, root_digest, _ = build_capsule(tmp_path)
    unsafe = tmp_path / "unsafe.tar"
    with tarfile.open(capsule, "r:") as source, tarfile.open(unsafe, "w") as target:
        for member in source.getmembers():
            target.addfile(member, source.extractfile(member))
        link = tarfile.TarInfo("blobs/sha256/" + "f" * 64)
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        target.addfile(link)

    result = verify(unsafe, root_digest)

    assert result.returncode != 0
    assert "unsafe or duplicate member" in result.stderr


def test_capsule_verification_stops_at_member_513(tmp_path: Path) -> None:
    capsule = tmp_path / "oversized-member-count.tar"
    with tarfile.open(capsule, "w") as stream:
        for member_number in range(513):
            name = (
                "capsule.json"
                if member_number == 0
                else f"blobs/sha256/{member_number:064x}"
            )
            member = tarfile.TarInfo(name)
            member.size = 1
            stream.addfile(member, io.BytesIO(b"x"))

    result = verify(capsule, f"sha256:{'a' * 64}")

    assert result.returncode != 0
    assert "capsule contains too many members" in result.stderr


def test_aggregate_receipt_binds_capsule_and_allows_archive_free_hermes_prefetch(
    tmp_path: Path,
) -> None:
    capsule, root_digest, _ = build_capsule(tmp_path)
    root_manifest_name = root_digest.removeprefix("sha256:")
    with tarfile.open(capsule, "r:") as stream:
        root_manifest = stream.extractfile(f"blobs/sha256/{root_manifest_name}").read()
    tools = tmp_path / "tools"
    tools.mkdir()
    raw_manifest = tools / "raw-manifest.json"
    raw_manifest.write_bytes(root_manifest)
    skopeo = tools / "skopeo"
    skopeo.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        '[[ "$1:$2:$3" == "inspect:--raw:oci-archive:"* ]]\n'
        'cat "$RAW_MANIFEST"\n'
    )
    skopeo.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "RAW_MANIFEST": str(raw_manifest),
    }
    publication = tmp_path / "publication"
    publication.mkdir()
    capsule.replace(publication / capsule.name)
    role_root = tmp_path / "roles"
    role_root.mkdir()
    commit = "a" * 40
    run_id = "12345"
    receipt_tool = ROOT / "scripts/dev-image-acceptance-receipt"
    for role in ("api", "worker", "hermes"):
        archive = publication / f"vonk-forge-{role}.oci.tar"
        archive.write_bytes(f"accepted-{role}".encode())
        command = [
            receipt_tool,
            "create-role",
            role_root / f"{role}.role-receipt.json",
            commit,
            run_id,
            role,
            archive,
        ]
        if role == "hermes":
            command.append(publication / capsule.name)
        created = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            check=False,
            text=True,
        )
        assert created.returncode == 0, created.stderr
    for name in ("docker-compose.dev.yml", "docker-compose.pinned.yml"):
        (publication / name).write_text("services: {}\n")
    receipt = tmp_path / "acceptance-receipt.json"
    aggregated = subprocess.run(
        [receipt_tool, "aggregate", receipt, commit, run_id, role_root, publication],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert aggregated.returncode == 0, aggregated.stderr
    (publication / "vonk-forge-hermes.oci.tar").unlink()

    verified = subprocess.run(
        [receipt_tool, "verify", receipt, commit, run_id, publication],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert verified.returncode == 0, verified.stderr
    document = json.loads(receipt.read_text())
    assert document["schema"] == "vonk-forge.dev-image-acceptance.v2"
    capsule_entry = document["roles"]["hermes"]["publication_capsule"]
    assert capsule_entry["artifact"] == (
        f"development-image-hermes-capsule-{commit}-{run_id}"
    )
    assert (
        capsule_entry["sha256"]
        == hashlib.sha256((publication / capsule.name).read_bytes()).hexdigest()
    )

    with (publication / capsule.name).open("ab") as stream:
        stream.write(b"tamper")
    rejected = subprocess.run(
        [receipt_tool, "verify", receipt, commit, run_id, publication],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert rejected.returncode != 0
    assert "publication capsule does not match its receipt" in rejected.stderr


class RegistryState:
    def __init__(self, external: set[str]) -> None:
        self.blobs = set(external)
        self.manifests: dict[str, bytes] = {}
        self.writes: list[tuple[str, str]] = []
        self.requests: list[tuple[str, str, str | None]] = []


def registry_handler(state: RegistryState):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def send(self, status: int, body: bytes = b"", **headers: str) -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            for key, value in headers.items():
                self.send_header(key.replace("_", "-"), value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def record(self) -> None:
            state.requests.append(
                (self.command, self.path, self.headers.get("Authorization"))
            )

        def digest_from_path(self) -> str:
            return urllib.parse.unquote(self.path.split("?", 1)[0].rsplit("/", 1)[1])

        def do_HEAD(self) -> None:
            self.record()
            if "/blobs/" not in self.path:
                self.send(404)
                return
            self.send(200 if self.digest_from_path() in state.blobs else 404)

        def do_GET(self) -> None:
            self.record()
            if "/manifests/" not in self.path:
                self.send(404)
                return
            reference = self.digest_from_path()
            body = state.manifests.get(reference)
            if body is None:
                self.send(404)
            else:
                self.send(200, body, Docker_Content_Digest=content_digest(body))

        def do_POST(self) -> None:
            self.record()
            if not self.path.endswith("/blobs/uploads/"):
                self.send(404)
                return
            state.writes.append(("POST", self.path))
            self.send(202, Location="/upload/accepted")

        def do_PUT(self) -> None:
            self.record()
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            state.writes.append(("PUT", self.path))
            if self.path.startswith("/upload/accepted"):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                digest = query["digest"][0]
                if content_digest(body) != digest:
                    self.send(400)
                    return
                state.blobs.add(digest)
                self.send(201, Docker_Content_Digest=digest)
                return
            if "/manifests/" in self.path:
                reference = self.digest_from_path()
                observed = content_digest(body)
                if reference.startswith("sha256:") and reference != observed:
                    self.send(400)
                    return
                state.manifests[reference] = body
                self.send(201, Docker_Content_Digest=observed)
                return
            self.send(404)

    return Handler


def publish_to_registry(
    tmp_path: Path,
    capsule: Path,
    state: RegistryState,
    expected_digest: str,
    *,
    image: str = "ghcr.io/example/hermes",
    output: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), registry_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return subprocess.run(
            [
                TOOL,
                "publish",
                capsule,
                image,
                f"dev-sha-{'a' * 40}",
                expected_digest,
                hashlib.sha256(capsule.read_bytes()).hexdigest(),
                output or tmp_path / "accepted-digest",
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "RUNNER_TEMP": str(tmp_path),
                "VONK_CAPSULE_TESTING": "1",
                "VONK_CAPSULE_TEST_REGISTRY": (
                    f"http://127.0.0.1:{server.server_port}"
                ),
            },
            capture_output=True,
            check=False,
            text=True,
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_capsule_publication_reuses_exact_repository_blobs_without_transferring_them(
    tmp_path: Path,
) -> None:
    capsule, root_digest, external_digests = build_capsule(tmp_path)
    state = RegistryState(external_digests)

    result = publish_to_registry(tmp_path, capsule, state, root_digest)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "accepted-digest").read_text() == f"{root_digest}\n"
    immutable_tag = f"dev-sha-{'a' * 40}"
    assert content_digest(state.manifests[immutable_tag]) == root_digest
    uploaded_paths = "\n".join(path for method, path in state.writes if method == "PUT")
    for digest in external_digests:
        assert digest not in uploaded_paths


def test_missing_external_blob_requests_full_archive_without_registry_mutation(
    tmp_path: Path,
) -> None:
    capsule, root_digest, external_digests = build_capsule(tmp_path)
    state = RegistryState(set(list(external_digests)[1:]))

    result = publish_to_registry(tmp_path, capsule, state, root_digest)

    assert result.returncode == 75
    assert "lacks accepted external blobs" in result.stderr
    assert state.writes == []
    assert not (tmp_path / "accepted-digest").exists()


def test_receipt_digest_rejects_a_valid_substituted_capsule_before_registry_access(
    tmp_path: Path,
) -> None:
    _, accepted_digest, accepted_external = build_capsule(
        tmp_path / "accepted", variant="accepted"
    )
    substituted, _, _ = build_capsule(tmp_path / "substituted", variant="substituted")
    state = RegistryState(accepted_external)

    result = publish_to_registry(tmp_path, substituted, state, accepted_digest)

    assert result.returncode != 0
    assert "capsule manifest does not match its receipt" in result.stderr
    assert state.requests == []
    assert state.writes == []


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/example//hermes",
        "ghcr.io/example/./hermes",
        "ghcr.io/example/../hermes",
        "ghcr.io/example/hermes/",
    ],
)
def test_noncanonical_repository_is_rejected_before_registry_access(
    tmp_path: Path, image: str
) -> None:
    capsule, root_digest, external = build_capsule(tmp_path)
    state = RegistryState(external)

    result = publish_to_registry(tmp_path, capsule, state, root_digest, image=image)

    assert result.returncode != 0
    assert "publication identity is invalid" in result.stderr
    assert state.requests == []
    assert state.writes == []


def test_invalid_digest_output_paths_fail_before_registry_access(
    tmp_path: Path,
) -> None:
    capsule, root_digest, external = build_capsule(tmp_path)
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    symlink_parent = tmp_path / "symlink-parent"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)
    directory_output = tmp_path / "directory-output"
    directory_output.mkdir()
    invalid_outputs = (
        tmp_path / "missing-parent" / "digest",
        symlink_parent / "digest",
        directory_output,
    )

    for output in invalid_outputs:
        state = RegistryState(external)
        result = publish_to_registry(
            tmp_path,
            capsule,
            state,
            root_digest,
            output=output,
        )
        assert result.returncode != 0
        assert "accepted digest output path is invalid" in result.stderr
        assert state.requests == []
        assert state.writes == []


class RedirectProbe:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str | None]] = []


def redirect_probe_handler(probe: RedirectProbe):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            probe.requests.append(
                (self.command, self.path, self.headers.get("Authorization"))
            )
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_HEAD = do_GET

    return Handler


def run_server(handler: type[http.server.BaseHTTPRequestHandler]):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def run_https_server(
    handler: type[http.server.BaseHTTPRequestHandler], certificate: Path, key: Path
):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def stop_server(
    server: http.server.ThreadingHTTPServer, thread: threading.Thread
) -> None:
    server.shutdown()
    thread.join()
    server.server_close()


def test_basic_credentials_do_not_follow_token_redirect_to_second_server(
    tmp_path: Path,
) -> None:
    capsule, root_digest, _ = build_capsule(tmp_path)
    attacker = RedirectProbe()
    attacker_server, attacker_thread = run_server(redirect_probe_handler(attacker))
    registry_requests: list[tuple[str, str, str | None]] = []

    class RegistryHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            registry_requests.append(
                (self.command, self.path, self.headers.get("Authorization"))
            )
            if self.path.startswith("/token?"):
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{attacker_server.server_port}/steal-basic",
                )
            else:
                realm = f"http://127.0.0.1:{self.server.server_port}/token"
                self.send_response(401)
                self.send_header(
                    "WWW-Authenticate",
                    f'Bearer realm="{realm}",service="fixture"',
                )
            self.send_header("Content-Length", "0")
            self.end_headers()

    registry_server, registry_thread = run_server(RegistryHandler)
    try:
        result = subprocess.run(
            [
                TOOL,
                "publish",
                capsule,
                "ghcr.io/example/hermes",
                f"dev-sha-{'a' * 40}",
                root_digest,
                hashlib.sha256(capsule.read_bytes()).hexdigest(),
                tmp_path / "accepted-digest",
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "VONK_CAPSULE_TESTING": "1",
                "VONK_CAPSULE_TEST_REGISTRY": (
                    f"http://127.0.0.1:{registry_server.server_port}"
                ),
            },
            capture_output=True,
            check=False,
            text=True,
        )
    finally:
        stop_server(registry_server, registry_thread)
        stop_server(attacker_server, attacker_thread)

    assert result.returncode != 0
    assert any(
        path.startswith("/token?") and authorization.startswith("Basic ")
        for _, path, authorization in registry_requests
        if authorization is not None
    )
    assert attacker.requests == []


def test_bearer_credentials_do_not_follow_blob_redirect_to_second_server(
    tmp_path: Path,
) -> None:
    capsule, root_digest, _ = build_capsule(tmp_path)
    attacker = RedirectProbe()
    attacker_server, attacker_thread = run_server(redirect_probe_handler(attacker))
    registry_requests: list[tuple[str, str, str | None]] = []

    class RegistryHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def record(self) -> None:
            registry_requests.append(
                (self.command, self.path, self.headers.get("Authorization"))
            )

        def do_GET(self) -> None:
            self.record()
            if self.path.startswith("/token?"):
                body = b'{"token":"accepted-bearer"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.headers.get("Authorization") != "Bearer accepted-bearer":
                realm = f"http://127.0.0.1:{self.server.server_port}/token"
                self.send_response(401)
                self.send_header(
                    "WWW-Authenticate",
                    f'Bearer realm="{realm}",service="fixture"',
                )
            else:
                self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self) -> None:
            self.record()
            self.send_response(307)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{attacker_server.server_port}/steal-bearer",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()

    registry_server, registry_thread = run_server(RegistryHandler)
    try:
        result = subprocess.run(
            [
                TOOL,
                "publish",
                capsule,
                "ghcr.io/example/hermes",
                f"dev-sha-{'a' * 40}",
                root_digest,
                hashlib.sha256(capsule.read_bytes()).hexdigest(),
                tmp_path / "accepted-digest",
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "VONK_CAPSULE_TESTING": "1",
                "VONK_CAPSULE_TEST_REGISTRY": (
                    f"http://127.0.0.1:{registry_server.server_port}"
                ),
            },
            capture_output=True,
            check=False,
            text=True,
        )
    finally:
        stop_server(registry_server, registry_thread)
        stop_server(attacker_server, attacker_thread)

    assert result.returncode != 0
    assert "external blob redirect must use HTTPS" in result.stderr
    assert any(
        method == "HEAD" and authorization == "Bearer accepted-bearer"
        for method, _, authorization in registry_requests
    )
    assert attacker.requests == []


def test_https_blob_redirect_is_followed_with_authorization_stripped(
    tmp_path: Path,
) -> None:
    capsule, root_digest, _ = build_capsule(tmp_path)
    certificate = tmp_path / "storage.crt"
    key = tmp_path / "storage.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            key,
            "-out",
            certificate,
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-days",
            "1",
        ],
        capture_output=True,
        check=True,
    )
    storage = RedirectProbe()
    storage_server, storage_thread = run_https_server(
        redirect_probe_handler(storage), certificate, key
    )

    class RegistryHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self) -> None:
            self.send_response(307)
            self.send_header(
                "Location",
                f"https://127.0.0.1:{storage_server.server_port}/external-blob",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()

    registry_server, registry_thread = run_server(RegistryHandler)
    try:
        result = subprocess.run(
            [
                TOOL,
                "publish",
                capsule,
                "ghcr.io/example/hermes",
                f"dev-sha-{'a' * 40}",
                root_digest,
                hashlib.sha256(capsule.read_bytes()).hexdigest(),
                tmp_path / "accepted-digest",
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "SSL_CERT_FILE": str(certificate),
                "VONK_CAPSULE_TESTING": "1",
                "VONK_CAPSULE_TEST_REGISTRY": (
                    f"http://127.0.0.1:{registry_server.server_port}"
                ),
            },
            capture_output=True,
            check=False,
            text=True,
        )
    finally:
        stop_server(registry_server, registry_thread)
        stop_server(storage_server, storage_thread)

    assert result.returncode != 0  # fixture intentionally has no upload endpoint
    assert storage.requests
    assert all(authorization is None for _, _, authorization in storage.requests)
