import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/lifecycle-counts.yml"


def test_counts_check_consumes_current_api_body(tmp_path: Path) -> None:
    """Catch stale event bodies on reruns and missing automatic edit checks."""
    text = WORKFLOW.read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert "edited" in workflow["on"]["pull_request"]["types"]
    assert "github.event.pull_request.body" not in text
    job = workflow["jobs"]["report"]
    assert job["permissions"] == {"contents": "read", "pull-requests": "read"}
    step = next(step for step in job["steps"] if "env" in step)
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert step["env"]["GH_REPO"] == "${{ github.repository }}"
    assert step["env"]["PR_NUMBER"] == "${{ github.event.pull_request.number }}"

    # Execute the actual workflow shell, replacing only its external commands.
    # The API response differs from the stale event and includes shell syntax
    # to prove that the body stays data when handed to the counts checker.
    current_body = "Current counts\n$(exit 99)\n`exit 99`\n"
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "assert sys.argv[1:] == ['pr', 'view', '42', '--json', 'body', '-q', '.body']\n"
        "assert os.environ['GH_TOKEN'] == 'test-token'\n"
        "assert os.environ['GH_REPO'] == 'example/vonk-forge'\n"
        "sys.stdout.write(os.environ['CURRENT_BODY'])\n"
    )
    gh.chmod(0o755)
    checker = tmp_path / "python3"
    checker.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "assert sys.argv[1:7] == ['scripts/lifecycle-counts', 'check', "
        "'--base', 'base-sha', '--head', 'head-sha']\n"
        "assert sys.argv[7] == '--body-file'\n"
        "Path(os.environ['CHECKED_BODY']).write_bytes(Path(sys.argv[8]).read_bytes())\n"
    )
    checker.chmod(0o755)
    checked_body = tmp_path / "checked-body.md"
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "GH_TOKEN": "test-token",
            "GH_REPO": "example/vonk-forge",
            "PR_NUMBER": "42",
            "BASE_SHA": "base-sha",
            "HEAD_SHA": "head-sha",
            "RUNNER_TEMP": str(tmp_path),
            "PR_BODY": "Stale event counts",
            "CURRENT_BODY": current_body,
            "CHECKED_BODY": str(checked_body),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert checked_body.read_text() == current_body
