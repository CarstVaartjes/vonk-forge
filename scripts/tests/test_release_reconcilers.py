from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERMES = ROOT / "scripts/reconcile-hermes-release-image"
RELEASE = ROOT / "scripts/reconcile-github-release"
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
IMAGE = "ghcr.io/carstvaartjes/vonk-forge-hermes"
REPOSITORY = "CarstVaartjes/vonk-forge"
SOURCE = "https://github.com/CarstVaartjes/vonk-forge"


def _write_tool(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _hermes_tools(
    tmp_path: Path, *, revision: str = SHA, attestation_exit: int = 0
) -> Path:
    tools = tmp_path / "bin"
    tools.mkdir()
    _write_tool(
        tools / "skopeo",
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if '--config' in sys.argv:\n"
        " print(json.dumps({'config': {'Labels': {'org.opencontainers.image.revision': '"
        + revision
        + "', 'org.opencontainers.image.source': '"
        + SOURCE
        + "', 'org.opencontainers.image.version': '1.2.3'}}}))\n"
        "else: print('"
        + DIGEST
        + "')\n",
    )
    _write_tool(
        tools / "docker",
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'predicateType': 'https://slsa.dev/provenance/v1', 'revision': '"
        + SHA
        + "'}))\n",
    )
    _write_tool(tools / "gh", f"#!/usr/bin/env bash\nexit {attestation_exit}\n")
    return tools


def _run_hermes(
    tools: Path, *, require_attestation: bool = True
) -> subprocess.CompletedProcess[str]:
    arguments = [
        str(HERMES),
        IMAGE,
        "1.2.3",
        SHA,
        SOURCE,
        REPOSITORY,
        "refs/tags/v1.2.3",
    ]
    if require_attestation:
        arguments.append("--require-attestation")
    return subprocess.run(
        arguments,
        cwd=ROOT,
        env={"PATH": f"{tools}:{os.environ['PATH']}"},
        check=False,
        capture_output=True,
        text=True,
    )


def test_hermes_reconciler_reuses_only_an_attested_exact_source_image(
    tmp_path: Path,
) -> None:
    result = _run_hermes(_hermes_tools(tmp_path))

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["exists=true", f"digest={DIGEST}"]


def test_hermes_reconciler_rejects_an_existing_image_from_another_commit(
    tmp_path: Path,
) -> None:
    result = _run_hermes(_hermes_tools(tmp_path, revision="c" * 40))

    assert result.returncode != 0
    assert result.stdout == ""


def test_hermes_preflight_allows_a_matching_partial_publication_to_be_attested(
    tmp_path: Path,
) -> None:
    result = _run_hermes(
        _hermes_tools(tmp_path, attestation_exit=1), require_attestation=False
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["exists=true", f"digest={DIGEST}"]


def _github_tool(tmp_path: Path) -> tuple[Path, Path]:
    tools = tmp_path / "bin"
    tools.mkdir()
    state = tmp_path / "release.json"
    state.write_text("{}\n", encoding="utf-8")
    _write_tool(
        tools / "gh",
        "#!/usr/bin/env python3\n"
        "import base64, json, os, sys\n"
        "from pathlib import Path\n"
        "state_path=Path(os.environ['VONK_RELEASE_STATE'])\n"
        "state=json.loads(state_path.read_text())\n"
        "args=sys.argv[1:]\n"
        "if args[:2] != ['release', args[1] if len(args)>1 else '']: raise SystemExit(97)\n"
        "command=args[1]\n"
        "if command == 'view':\n"
        " if os.environ.get('VONK_RELEASE_VIEW_ERROR'):\n"
        "  print(os.environ['VONK_RELEASE_VIEW_ERROR'], file=sys.stderr); raise SystemExit(1)\n"
        " if not state: print('release not found', file=sys.stderr); raise SystemExit(1)\n"
        " print(json.dumps({**{k:v for k,v in state.items() if k != 'assets'}, 'assets':[{'name':n} for n in state['assets']]})); raise SystemExit(0)\n"
        "if command == 'create':\n"
        " target=args[args.index('--target')+1]; title=args[args.index('--title')+1]\n"
        " state={'tagName':args[2],'targetCommitish':target,'name':title,'isDraft':True,'isPrerelease':False,'assets':{}}\n"
        "if command == 'upload':\n"
        " for value in args[3:]:\n"
        "  path=Path(value)\n"
        "  if path.is_file(): state['assets'][path.name]=base64.b64encode(path.read_bytes()).decode()\n"
        "if command == 'download':\n"
        " name=args[args.index('-p')+1]; destination=Path(args[args.index('-D')+1]); destination.mkdir(exist_ok=True)\n"
        " (destination/name).write_bytes(base64.b64decode(state['assets'][name]))\n"
        "if command == 'edit':\n"
        " if not state['isDraft'] and os.environ.get('VONK_REJECT_PUBLISHED_EDIT'):\n"
        "  print('published release is immutable', file=sys.stderr); raise SystemExit(91)\n"
        " state['isDraft']=False\n"
        "state_path.write_text(json.dumps(state)+'\\n')\n",
    )
    return tools, state


def _run_release(
    tools: Path,
    state: Path,
    assets: tuple[Path, ...],
    *,
    view_error: str = "",
    reject_published_edit: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (str(RELEASE), REPOSITORY, "v1.2.3", SHA, "Vonk Forge 1.2.3", *map(str, assets)),
        cwd=ROOT,
        env={
            "PATH": f"{tools}:{os.environ['PATH']}",
            "VONK_RELEASE_STATE": str(state),
            "VONK_RELEASE_VIEW_ERROR": view_error,
            "VONK_REJECT_PUBLISHED_EDIT": "1" if reject_published_edit else "",
        },
        check=False,
        capture_output=True,
        text=True,
    )


def test_github_release_reconciler_creates_then_replays_the_exact_asset_set(
    tmp_path: Path,
) -> None:
    tools, state = _github_tool(tmp_path)
    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_bytes(b"first\n")
    second.write_bytes(b"second\n")

    created = _run_release(tools, state, (first, second))
    replayed = _run_release(tools, state, (first, second))

    assert created.returncode == 0, created.stderr
    assert replayed.returncode == 0, replayed.stderr
    release = json.loads(state.read_text(encoding="utf-8"))
    assert release["tagName"] == "v1.2.3"
    assert release["targetCommitish"] == SHA
    assert release["name"] == "Vonk Forge 1.2.3"
    assert release["isDraft"] is False
    assert {
        name: base64.b64decode(content)
        for name, content in release["assets"].items()
    } == {"one.txt": b"first\n", "two.txt": b"second\n"}


def test_github_release_reconciler_rejects_a_conflicting_existing_asset(
    tmp_path: Path,
) -> None:
    tools, state = _github_tool(tmp_path)
    asset = tmp_path / "one.txt"
    asset.write_bytes(b"expected\n")
    state.write_text(
        json.dumps(
            {
                "tagName": "v1.2.3",
                "targetCommitish": SHA,
                "name": "Vonk Forge 1.2.3",
                "isDraft": True,
                "isPrerelease": False,
                "assets": {"one.txt": base64.b64encode(b"conflict\n").decode()},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_release(tools, state, (asset,))

    assert result.returncode != 0
    assert "conflicting release asset" in result.stderr


def test_github_release_reconciler_replays_a_published_release_without_editing_it(
    tmp_path: Path,
) -> None:
    tools, state = _github_tool(tmp_path)
    asset = tmp_path / "one.txt"
    asset.write_bytes(b"expected\n")
    state.write_text(
        json.dumps(
            {
                "tagName": "v1.2.3",
                "targetCommitish": SHA,
                "name": "Vonk Forge 1.2.3",
                "isDraft": False,
                "isPrerelease": False,
                "assets": {"one.txt": base64.b64encode(b"expected\n").decode()},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_release(tools, state, (asset,), reject_published_edit=True)

    assert result.returncode == 0, result.stderr


def test_github_release_reconciler_fails_closed_on_an_unknown_lookup_error(
    tmp_path: Path,
) -> None:
    tools, state = _github_tool(tmp_path)
    asset = tmp_path / "one.txt"
    asset.write_bytes(b"expected\n")

    result = _run_release(
        tools, state, (asset,), view_error="release API returned 503"
    )

    assert result.returncode != 0
    assert json.loads(state.read_text(encoding="utf-8")) == {}
