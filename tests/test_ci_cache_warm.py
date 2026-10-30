"""The cache warm workflow must produce the keys that ci.yml restores.

rust-cache keys on the shared-key, the runner image and architecture, the
`rustc -vV` of the installed toolchain, Cargo manifests/lock/config and any
environment variable (workflow or job level) starting with CARGO, CC, CFLAGS,
CXX, CMAKE or RUST.
setup-uv (through the python-env action) keys on the lockfiles. A warm job that
drifts from the job it warms would save entries nothing restores.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WARM = yaml.safe_load((ROOT / ".github/workflows/ci-cache-warm.yml").read_text())
CI = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
TRIGGERS = WARM[True]  # PyYAML reads the bare key `on` as boolean True
RUST_CACHE = "Swatinem/rust-cache@"
ENV_PREFIXES = ("CARGO", "CC", "CFLAGS", "CXX", "CMAKE", "RUST")


def _steps(job: dict, prefix: str) -> list[dict]:
    return [s for s in job["steps"] if s.get("uses", "").startswith(prefix)]


def _toolchain_runs(job: dict) -> set[str]:
    return {
        s["run"].strip()
        for s in job["steps"]
        if s.get("name") == "Install pinned Rust toolchain"
    }


def _cache_env(workflow: dict, job: dict) -> dict:
    """Workflow env merged with job env, limited to what rust-cache hashes."""
    merged = {**workflow.get("env", {}), **job.get("env", {})}
    return {k: str(v) for k, v in merged.items() if k.startswith(ENV_PREFIXES)}


def test_warm_jobs_match_a_ci_job_for_every_cache_key_input() -> None:
    problems = []
    for name, warm in WARM["jobs"].items():
        (cache,) = _steps(warm, RUST_CACHE)
        warm_env = _cache_env(WARM, warm)
        twins = [
            job
            for job in CI["jobs"].values()
            if job.get("runs-on") == warm["runs-on"]
            and any(s["with"] == cache["with"] for s in _steps(job, RUST_CACHE))
            and _cache_env(CI, job) == warm_env
            and _toolchain_runs(warm) <= _toolchain_runs(job)
        ]
        if not twins:
            problems.append(
                f"{name}: no ci.yml job restores this exact cache "
                f"(effective key env {warm_env})"
            )
        python_env = [
            s for s in warm["steps"] if s.get("uses") == "./.github/actions/python-env"
        ]
        if len(python_env) != 1:
            problems.append(f"{name}: must prepare uv through the python-env action")
    assert problems == []


def test_ci_workflow_env_does_not_split_the_cache_key() -> None:
    # Cargo network settings belong in .cargo/config.toml, which is a key input.
    assert _cache_env(CI, {}) == {}
    assert (ROOT / ".cargo/config.toml").is_file()


def test_warm_workflow_covers_both_architectures_and_saves_only_on_main() -> None:
    keys = {
        cache["with"]["shared-key"]
        for job in WARM["jobs"].values()
        for cache in _steps(job, RUST_CACHE)
    }
    assert keys == {"linux-amd64", "linux-arm64"}
    assert set(TRIGGERS) == {"push", "schedule", "workflow_dispatch"}
    assert TRIGGERS["push"]["branches"] == ["main"]
