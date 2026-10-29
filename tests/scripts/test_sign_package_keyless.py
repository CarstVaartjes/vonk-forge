from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/sign-package-keyless"
CONFLICT = "[POST /api/v1/log/entries][409] createLogEntryConflict"

# A fake cosign: sign-blob fails ``FAIL_SIGNS`` times with Rekor's 409 and then
# writes the bundle; verify-blob fails when ``VERIFY_FAILS`` is set.
FAKE = f"""#!/bin/sh
echo "$1" >> "$CALLS"
case "$1" in
sign-blob)
  n=$(grep -c sign-blob "$CALLS")
  if [ "$n" -le "$FAIL_SIGNS" ]; then echo '{CONFLICT}' >&2; exit 1; fi
  while [ "$#" -gt 0 ]; do
    if [ "$1" = --bundle ]; then echo '{{}}' > "$2"; fi
    shift
  done ;;
verify-blob)
  test -f "$3" && test -z "$VERIFY_FAILS" ;;
esac
"""


def run(
    tmp_path: Path, fail_signs: int, verify_fails: str = ""
) -> tuple[int, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cosign = bin_dir / "cosign"
    cosign.write_text(FAKE)
    cosign.chmod(0o755)
    package = tmp_path / "a.deb"
    package.write_bytes(b"deb")
    calls = tmp_path / "calls"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CALLS": str(calls),
        "FAIL_SIGNS": str(fail_signs),
        "VERIFY_FAILS": verify_fails,
    }
    done = subprocess.run(
        [str(SCRIPT), str(package), str(tmp_path / "a.deb.sigstore.json"), "id"],
        env=env,
        capture_output=True,
        check=False,
    )
    return done.returncode, calls.read_text().split()


def test_signs_once_and_verifies(tmp_path: Path) -> None:
    assert run(tmp_path, 0) == (0, ["sign-blob", "verify-blob"])


def test_a_log_conflict_is_retried_with_a_new_signing(tmp_path: Path) -> None:
    code, calls = run(tmp_path, 2)
    assert code == 0
    assert calls == ["sign-blob"] * 3 + ["verify-blob"]


def test_gives_up_after_bounded_attempts(tmp_path: Path) -> None:
    code, calls = run(tmp_path, 9)
    assert code != 0
    assert calls == ["sign-blob"] * 3


def test_failed_verification_fails_closed(tmp_path: Path) -> None:
    code, _ = run(tmp_path, 0, verify_fails="1")
    assert code != 0
