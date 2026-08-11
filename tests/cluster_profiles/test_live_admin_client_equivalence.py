from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from cluster_profiles.control_client import ControlClient


def _rewriting_opener(origin: str):
    def open_request(request: urllib.request.Request, timeout: float):
        path = urllib.parse.urlsplit(request.full_url).path
        rewritten = urllib.request.Request(
            origin + path,
            data=request.data,
            headers=dict(request.header_items()),
            method=request.method,
        )
        return urllib.request.urlopen(rewritten, timeout=timeout)

    return open_request


def test_generated_cli_and_rendered_browser_share_live_plan_and_apply_contract(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    state_file = tmp_path / "fleet-state.json"
    state_file.write_text('{"available":true}\n')
    ready_file = tmp_path / "server-ready.json"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [
            "uv",
            "run",
            "--python",
            "3.12",
            "--project",
            "control",
            "python",
            "control/tests/admin_equivalence_server.py",
            str(port),
            str(state_file),
            str(ready_file),
        ],
        cwd=root,
    )
    try:
        for _ in range(150):
            if ready_file.exists():
                try:
                    urllib.request.urlopen(f"{origin}/api/v1/healthz", timeout=0.2)
                    break
                except OSError:
                    pass
            if server.poll() is not None:
                raise AssertionError("disposable control API exited early")
            time.sleep(0.02)
        else:
            raise AssertionError("disposable control API did not start")
        token = json.loads(ready_file.read_text())["token"]
        token_file = tmp_path / "token"
        token_file.write_text(token + "\n")
        token_file.chmod(0o600)
        cli = ControlClient(
            "https://control.test",
            token_file,
            opener=_rewriting_opener(origin),
        )
        cli_plan = cli.plan_profile("production-agents").to_dict()
        expected_file = tmp_path / "cli-plan.json"
        expected_file.write_text(json.dumps(cli_plan, sort_keys=True))
        result_file = tmp_path / "browser-result.json"
        environment = {
            **os.environ,
            "VONK_LIVE_ORIGIN": origin,
            "VONK_LIVE_TOKEN": token,
            "VONK_LIVE_STATE_FILE": str(state_file),
            "VONK_LIVE_EXPECTED_FILE": str(expected_file),
            "VONK_LIVE_RESULT_FILE": str(result_file),
        }
        subprocess.run(
            [
                "npm",
                "--prefix",
                "control/web",
                "test",
                "--",
                "--run",
                "src/admin-equivalence.live.test.tsx",
            ],
            cwd=root,
            env=environment,
            check=True,
        )
        browser = json.loads(result_file.read_text())
        assert browser["commit"] == cli_plan["commit"]
        assert browser["digest"] == cli_plan["digest"]
        assert browser["targets"] == cli_plan["targets"]
        assert browser["operations"] == cli_plan["operation_graph"]["nodes"]
        assert browser["apply_body"] == {
            "fleet_evidence_digest": cli_plan["fleet_evidence_digest"],
            "plan_digest": cli_plan["digest"],
        }
        assert browser["stale_status"] == 409
        assert browser["unavailable_visible"] is True
    finally:
        server.terminate()
        server.wait(timeout=5)
