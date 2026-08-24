from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/qualify-recipe"
DS4 = ROOT / "config/recipes/deepseek-v4-flash-0731-ds4-single.json"
MIA = ROOT / "config/recipes/deepseek-v4-flash-0731-mia-dual.json"


def test_structural_qualification_supports_standard_media_outputs() -> None:
    namespace = runpy.run_path(str(SCRIPT))
    supported = namespace["SUPPORTED_CHECKS"]

    assert "artifact.mime.image-png" in supported
    assert "artifact.mime.audio-wav" in supported
    assert "artifact.mime.video-mp4" in supported
    assert "artifact.mime.model-gltf-binary" in supported
    assert "artifact.mime.application-octet-stream" in supported


def _fake_engine(path: Path, architecture: str) -> Path:
    engine = path / "docker"
    engine.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = info ]; then\n"
        f"  printf '%s\\n' '{architecture}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 97\n",
        encoding="utf-8",
    )
    engine.chmod(0o755)
    return engine


def _behavioral_engine(path: Path, architecture: str = "arm64") -> tuple[Path, Path, Path]:
    engine = path / "docker"
    state = path / "engine-state.json"
    log = path / "engine-log.jsonl"
    state.write_text('{"running":[]}', encoding="utf-8")
    engine.write_text(
        f"""#!{sys.executable}
import json
import os
import pathlib
import sys

state_path = pathlib.Path(os.environ["VONK_FAKE_ENGINE_STATE"])
log_path = pathlib.Path(os.environ["VONK_FAKE_ENGINE_LOG"])
arguments = sys.argv[1:]
with log_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(arguments, separators=(",", ":")) + "\\n")
state = json.loads(state_path.read_text(encoding="utf-8"))
running = set(state["running"])
if arguments[:1] == ["info"]:
    print({architecture!r})
elif arguments[:1] == ["build"]:
    if arguments.count("--pull=false") != 1 or "--pull" in arguments:
        print("malformed or pull-enabled build invocation", file=sys.stderr)
        raise SystemExit(96)
    print("qualified-image")
elif arguments[:1] == ["run"]:
    name = arguments[arguments.index("--name") + 1]
    running.add(name)
    print(name)
elif arguments[:1] == ["inspect"]:
    name = arguments[-1]
    if name not in running:
        raise SystemExit(1)
    print("true")
elif arguments[:1] in (["stop"], ["start"], ["rm"]):
    name = arguments[-1]
    if arguments[0] == "start":
        running.add(name)
    else:
        running.discard(name)
else:
    raise SystemExit(97)
state_path.write_text(json.dumps({{"running": sorted(running)}}), encoding="utf-8")
""",
        encoding="utf-8",
    )
    engine.chmod(0o755)
    return engine, state, log


class _QualificationHandler(BaseHTTPRequestHandler):
    state_path: Path
    required_ranks: int

    def log_message(self, _format: str, *_arguments: object) -> None:
        pass

    def _healthy(self) -> bool:
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        return len(state["running"]) == self.required_ranks

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._healthy():
            self._json(503, {"detail": "collective unavailable"})
            return
        self._json(200, {"data": [{"id": "deepseek-v4-flash-dspark"}]})

    def do_POST(self) -> None:
        if not self._healthy():
            self._json(503, {"detail": "collective unavailable"})
            return
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length))
        assert request["max_tokens"] == 64
        self._json(
            200,
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "qualified"}}
                ]
            },
        )


def test_container_qualification_reports_non_arm64_as_limitation(
    tmp_path: Path,
) -> None:
    assert SCRIPT.is_file()
    engine = _fake_engine(tmp_path, "amd64")
    result = subprocess.run(
        [
            str(SCRIPT),
            "--recipe",
            str(DS4),
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
    assert payload["detected_architecture"] == "amd64"
    assert payload["passed"] is False


def test_structural_qualification_validates_both_native_recipes() -> None:
    assert SCRIPT.is_file()
    for recipe in (DS4, MIA):
        result = subprocess.run(
            [str(SCRIPT), "--recipe", str(recipe), "--level", "structural"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        payload = json.loads(result.stdout)
        assert payload["status"] == "passed"
        assert payload["passed"] is True
        assert payload["recipe"] == recipe.name


def test_structural_qualification_compiles_the_selected_harness(
    tmp_path: Path,
) -> None:
    recipe = json.loads(DS4.read_text(encoding="utf-8"))
    recipe["runtime"]["entrypoint"] = ["ds4-serve"]
    recipe_path = tmp_path / "invalid-entrypoint.json"
    recipe_path.write_text(json.dumps(recipe), encoding="utf-8")

    result = subprocess.run(
        [str(SCRIPT), "--recipe", str(recipe_path), "--level", "structural"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["passed"] is False
    assert payload["status"] == "failed"
    assert payload["error"] == "harness recipe entrypoint is invalid"


def test_container_qualification_executes_generic_distributed_lifecycle(
    tmp_path: Path,
) -> None:
    engine, state, log = _behavioral_engine(tmp_path)
    artifacts = tmp_path / "models"
    artifacts.mkdir()
    recipe = json.loads(MIA.read_text(encoding="utf-8"))
    recipe["identity"]["slug"] = "user-authored-vllm-two-node"
    recipe["build"]["context"]["path"] = "adapters/deepseek/mia-vllm"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _QualificationHandler)
    _QualificationHandler.state_path = state
    _QualificationHandler.required_ranks = 2
    recipe["interfaces"][0]["port"] = server.server_port
    recipe_path = tmp_path / "user-recipe.json"
    recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [
                str(SCRIPT),
                "--recipe",
                str(recipe_path),
                "--level",
                "container",
                "--engine",
                str(engine),
                "--endpoint-host",
                "127.0.0.1",
                "--artifact-root",
                str(artifacts),
                "--timeout-seconds",
                "5",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env={
                **os.environ,
                "VONK_FAKE_ENGINE_STATE": str(state),
                "VONK_FAKE_ENGINE_LOG": str(log),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.returncode == 0, result.stderr or result.stdout
    evidence = json.loads(result.stdout)
    assert evidence["passed"] is True
    assert evidence["recipe"] == "user-authored-vllm-two-node"
    assert [step["check"] for step in evidence["steps"]] == [
        "image.build",
        "runtime.start",
        "collective.two-ranks",
        "endpoint-owner.ready",
        "chat.invoke",
        "runtime.stop",
        "runtime.restart",
        "rank-loss.withdrawal",
        "rank-recovery.healthy",
        "chat.recovered",
        "runtime.cleanup",
    ]
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    starts = [call for call in calls if call[:1] == ["run"]]
    assert len(starts) == 6
    assert all(
        sum("dst=/models" in value for value in call) == 1 for call in starts
    )
    assert "worker" in starts[0][starts[0].index("--name") + 1]
    assert "entrypoint" in starts[1][starts[1].index("--name") + 1]
    rank_loss_stop = next(
        index
        for index, call in enumerate(calls)
        if call[:1] == ["stop"] and "worker" in call[-1]
    )
    recovery_worker = next(
        index
        for index, call in enumerate(calls[rank_loss_stop + 1 :], rank_loss_stop + 1)
        if call[:1] == ["run"] and "worker" in call[call.index("--name") + 1]
    )
    recovery_owner = next(
        index
        for index, call in enumerate(calls[recovery_worker + 1 :], recovery_worker + 1)
        if call[:1] == ["run"] and "entrypoint" in call[call.index("--name") + 1]
    )
    assert recovery_worker < recovery_owner
    assert json.loads(state.read_text(encoding="utf-8")) == {"running": []}


def test_bridge_qualification_publishes_the_bounded_endpoint_and_builds_offline(
    tmp_path: Path,
) -> None:
    engine, state, log = _behavioral_engine(tmp_path)
    artifacts = tmp_path / "models"
    artifacts.mkdir()
    recipe = json.loads(DS4.read_text(encoding="utf-8"))
    recipe["identity"]["slug"] = "user-authored-ds4-single"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _QualificationHandler)
    _QualificationHandler.state_path = state
    _QualificationHandler.required_ranks = 1
    recipe["interfaces"][0]["port"] = server.server_port
    recipe_path = tmp_path / "user-ds4-recipe.json"
    recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [
                str(SCRIPT),
                "--recipe",
                str(recipe_path),
                "--level",
                "container",
                "--engine",
                str(engine),
                "--endpoint-host",
                "127.0.0.1",
                "--artifact-root",
                str(artifacts),
                "--timeout-seconds",
                "5",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env={
                **os.environ,
                "VONK_FAKE_ENGINE_STATE": str(state),
                "VONK_FAKE_ENGINE_LOG": str(log),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.returncode == 0, result.stderr or result.stdout
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    build = next(call for call in calls if call[:1] == ["build"])
    assert build[build.index("--network") + 1] == "none"
    assert build.count("--pull=false") == 1
    assert "--pull" not in build
    starts = [call for call in calls if call[:1] == ["run"]]
    assert len(starts) == 2
    assert all(
        call[call.index("--publish") + 1]
        == f"127.0.0.1:{server.server_port}:{server.server_port}"
        for call in starts
    )
    expected_engine_argv = [
        "/opt/vonk/bin/ds4-serve",
        "--model",
        "/models/DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf",
        "--mtp",
        "/models/DeepSeek-V4-Flash-DSpark-support-0731.gguf",
        "--ctx",
        "131072",
        "--batched-session",
        "2",
        "--dspark",
        "--cuda",
        "--host",
        "0.0.0.0",
        "--port",
        str(server.server_port),
    ]
    assert all(call[-len(expected_engine_argv) :] == expected_engine_argv for call in starts)


def test_container_qualification_is_fail_closed_on_failed_invocation(
    tmp_path: Path,
) -> None:
    engine, state, log = _behavioral_engine(tmp_path)
    artifacts = tmp_path / "models"
    artifacts.mkdir()
    recipe = json.loads(DS4.read_text(encoding="utf-8"))
    recipe["build"]["context"]["path"] = "adapters/deepseek/ds4"
    recipe["interfaces"][0]["port"] = 65432
    recipe_path = tmp_path / "unhealthy.json"
    recipe_path.write_text(json.dumps(recipe), encoding="utf-8")

    result = subprocess.run(
        [
            str(SCRIPT),
            "--recipe",
            str(recipe_path),
            "--level",
            "container",
            "--engine",
            str(engine),
            "--endpoint-host",
            "127.0.0.1",
            "--artifact-root",
            str(artifacts),
            "--timeout-seconds",
            "1",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={
            **os.environ,
            "VONK_FAKE_ENGINE_STATE": str(state),
            "VONK_FAKE_ENGINE_LOG": str(log),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )

    assert result.returncode == 1
    evidence = json.loads(result.stdout)
    assert evidence["passed"] is False
    assert evidence["status"] == "failed"
    assert json.loads(state.read_text(encoding="utf-8")) == {"running": []}
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    start = next(call for call in calls if call[:1] == ["run"])
    assert sum("dst=/models" in value for value in start) == 1
