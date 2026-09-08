"""Connected real DEB producer bytes consumed by the Controller source reader."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / path) for path in ("scripts", "agent_protocol/src", "control/src", "tests/scripts")]
import httpx
import yaml
from agent_package_source_publication import package_source
from vonk_control.agent_package_source import load_package_source


@unittest.skipUnless(Path("/usr/bin/dpkg-deb").exists(), "Debian package producer runs in OrbStack")
class PackageSourcePublicationTests(unittest.TestCase):
    def test_signed_deb_producer_bytes_are_consumed_at_exact_binary_lookup(self):
        from test_agent_deb import (
            PACKAGE_BINARIES,
            _aarch64_fixture,
            _build,
            _release_key,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binaries = root / "binaries"
            binaries.mkdir()
            for name in PACKAGE_BINARIES:
                binary = binaries / name
                _aarch64_fixture(binary, name.encode())
                binary.chmod(0o644)
                binary.write_bytes(binary.read_bytes().replace(
                    b"VONK_AGENT_SEMANTIC_VERSION=0.1.1",
                    b"VONK_AGENT_SEMANTIC_VERSION=0.1.0",
                ))
                binary.chmod(0o555)
            key = root / "release.pem"
            _release_key(key)
            version = "0.1.0~acceptance.1+g0123456789ab"
            built = _build(root / "baseline", binaries, key,
                           version=version, acceptance_baseline=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            # Consume only the files actually uploaded by the production action.
            action = yaml.safe_load((ROOT / ".github/actions/agent-package-build/action.yml").read_text())
            upload = next(step for step in action["runs"]["steps"]
                          if step.get("name") == "Upload immutable acceptance baseline packages")
            downloaded = root / "downloaded"
            downloaded.mkdir()
            for item in upload["with"]["path"].splitlines():
                artifact = root / item.replace("${{ inputs.baseline_version }}", version)
                shutil.copy2(artifact, downloaded / artifact.name)
            package = downloaded / f"vonk-forge-agent_{version}_arm64.deb"
            source = package_source(package, version=version)
            raw = source.model_dump_json().encode()
            requested = []
            def respond(request):
                requested.append(request.url.path)
                return httpx.Response(200, content=raw)
            with httpx.Client(base_url="https://install.vonkforge.ai", transport=httpx.MockTransport(respond)) as client:
                loaded = load_package_source(client, "dev", source.build_digest, source.package.binary_sha256)
                self.assertEqual(loaded, source)
                self.assertEqual(requested, ["/" + source.object_key("dev")])
                with self.assertRaises(ValueError):
                    load_package_source(client, "dev", source.build_digest, "a" * 64)
            provenance = package.with_suffix(".provenance.json")
            document = json.loads(provenance.read_bytes())
            document["subject"][1]["digest"]["sha256"] = "f" * 64
            provenance.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "provenance"):
                package_source(package, version=version)


if __name__ == "__main__":
    unittest.main()
