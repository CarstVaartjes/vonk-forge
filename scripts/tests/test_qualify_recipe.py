from __future__ import annotations

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "qualify-recipe"


def _library_root() -> Path:
    candidates = []
    configured = os.environ.get("VONK_RECIPE_CANONICAL_ROOT")
    if configured:
        candidates.append(Path(configured))
    candidates.extend((Path("/opt/vonk-forge-recipes"), Path("/private/tmp/vonk-forge-recipes-canonical")))
    for candidate in candidates:
        if (candidate / "contracts" / "src" / "vonk_forge_contracts").is_dir() and (candidate / "recipes").is_dir():
            return candidate
    pytest.skip("a published schema-2 recipe checkout is required for qualifier integration tests")


def _recipes(root: Path) -> list[Path]:
    return sorted((root / "recipes").glob("*.json"))


def _run(recipe: Path, library_root: Path) -> dict[str, object]:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--recipe",
            str(recipe),
            "--library-root",
            str(library_root),
            "--platform-root",
            str(ROOT),
            "--level",
            "structural",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def test_archive_safety_rejects_traversal_and_reserved_payload_namespaces() -> None:
    namespace = __import__("runpy").run_path(str(SCRIPT))
    safe_member = namespace["_safe_member"]
    safe_member("adapters/example/Dockerfile")
    safe_member("adapters/example/weights.pt")
    for member in ("../escape", "/absolute", "weights/model.safetensors", "oci/image.tar"):
        with pytest.raises(namespace["QualificationError"]):
            safe_member(member)


def test_structural_qualification_uses_dynamic_published_catalog() -> None:
    root = _library_root()
    recipes = _recipes(root)
    assert recipes
    payload = _run(recipes[0], root)
    assert payload["status"] == "passed"
    assert payload["physical_claim"] is False
    validator = payload["independent_validator"]
    assert validator["status"] == "passed"
    assert validator["recipe_count"] == len(recipes)
    assert validator["recipe_count"] > 0


def test_structural_examples_cover_source_job_and_dual_contracts() -> None:
    root = _library_root()
    selected: dict[str, Path] = {}
    for path in _recipes(root):
        document = json.loads(path.read_text(encoding="utf-8"))
        mode = document["execution"]["mode"]
        adapter = document["interfaces"][0]["adapter"]
        topology = document["topology"]["node_count"] > 1
        selected.setdefault("source", path) if mode == "build" else None
        selected.setdefault("job", path) if adapter != "openai" else None
        selected.setdefault("dual", path) if topology else None
        selected.setdefault("image", path) if mode == "image" else None
    assert {"source", "job", "dual"} <= selected.keys()
    for path in selected.values():
        payload = _run(path, root)
        assert payload["passed"] is True
        assert payload["physical_claim"] is False
        if path == selected.get("dual"):
            assert payload["compiled_roles"] > 1
    if "image" in selected:
        assert _run(selected["image"], root)["source_build"] is False


def test_qualifier_local_http_persists_and_reloads_evidence_ledger(tmp_path: Path) -> None:
    root = _library_root()
    recipe = root / "recipes" / "qwen3-8-27b-nvfp4-dspark-sglang-single.json"
    if not recipe.is_file():
        pytest.skip("the v1.0.3 GLM/Qwen producer fixture is unavailable")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._write({"data": [{"id": "qwen3-8-27b"}]})

        def do_POST(self) -> None:
            size = int(self.headers["Content-Length"] or 0)
            json.loads(self.rfile.read(size))
            self._write({"choices": [{"message": {"content": "fixture response"}}], "usage": {"completion_tokens": 1}})

        def _write(self, payload: dict[str, object]) -> None:
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    ledger_path = tmp_path / "qualification.jsonl"
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--recipe",
                str(recipe),
                "--library-root",
                str(root),
                "--platform-root",
                str(ROOT),
                "--level",
                "structural",
                "--serving-url",
                f"http://127.0.0.1:{server.server_port}",
                "--evidence-ledger",
                str(ledger_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        server.shutdown()
        worker.join(timeout=2)
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["serving"]["scope"] == "local-http"
    sys.path.insert(0, str(ROOT / "src"))
    from cluster_profiles.fleet_qualification import EvidenceLedger

    ledger = EvidenceLedger(ledger_path)
    assert [row["event"] for row in ledger.records] == [
        "step.started",
        "step.completed",
        "step.started",
        "step.completed",
    ]
    assert all(row["payload"]["step"].startswith("serving.") for row in ledger.records)


def test_qualifier_local_http_failure_persists_failed_step(tmp_path: Path) -> None:
    root = _library_root()
    recipe = root / "recipes" / "qwen3-8-27b-nvfp4-dspark-sglang-single.json"
    if not recipe.is_file():
        pytest.skip("the v1.0.3 GLM/Qwen producer fixture is unavailable")

    class FailureHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._write({"data": [{"id": "qwen3-8-27b"}]})

        def do_POST(self) -> None:
            size = int(self.headers["Content-Length"] or 0)
            self.rfile.read(size)
            self._write({"choices": [{"message": {"content": ""}}]})

        def _write(self, payload: dict[str, object]) -> None:
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), FailureHandler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    ledger_path = tmp_path / "qualification-failed.jsonl"
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--recipe",
                str(recipe),
                "--library-root",
                str(root),
                "--platform-root",
                str(ROOT),
                "--level",
                "structural",
                "--serving-url",
                f"http://127.0.0.1:{server.server_port}",
                "--evidence-ledger",
                str(ledger_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        server.shutdown()
        worker.join(timeout=2)
    assert result.returncode == 1
    sys.path.insert(0, str(ROOT / "src"))
    from cluster_profiles.fleet_qualification import EvidenceLedger

    ledger = EvidenceLedger(ledger_path)
    assert [row["event"] for row in ledger.records] == [
        "step.started",
        "step.completed",
        "step.started",
        "step.failed",
    ]


def test_container_gate_reports_environment_without_spark_claim(tmp_path: Path) -> None:
    root = _library_root()
    recipe = _recipes(root)[0]
    engine = tmp_path / "engine"
    engine.write_text("#!/bin/sh\nprintf '%s\\n' amd64\n", encoding="utf-8")
    engine.chmod(0o755)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--recipe",
            str(recipe),
            "--library-root",
            str(root),
            "--platform-root",
            str(ROOT),
            "--level",
            "container",
            "--engine",
            str(engine),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 3
    payload = json.loads(result.stdout)
    assert payload["status"] == "environment-limited"
    assert payload["required_architecture"] == "arm64"
    assert payload["physical_claim"] is False


def test_container_gate_refuses_without_production_launch_adapter(tmp_path: Path) -> None:
    root = _library_root()
    engine = tmp_path / "engine.py"
    log = tmp_path / "engine.log"
    engine.write_text(
        f"""#!/usr/bin/env python3
import sys
from pathlib import Path
args=sys.argv[1:]
Path({str(log)!r}).open('a', encoding='utf-8').write(' '.join(args)+'\\n')
if args[:1] == ['info']:
    print('arm64')
""",
        encoding="utf-8",
    )
    engine.chmod(0o755)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--recipe", str(_recipes(root)[0]), "--library-root", str(root),
         "--platform-root", str(ROOT), "--level", "container", "--engine", str(engine),
         "--artifact-root", str(tmp_path / "models")],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 3
    payload = json.loads(result.stdout)
    assert payload["status"] == "environment-limited"
    assert "production CompiledExecutionPlan" in payload["detail"]
    engine_log = log.read_text(encoding="utf-8")
    assert "info" in engine_log
    assert " run " not in engine_log
