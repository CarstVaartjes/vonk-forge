"""Connected real DEB producer bytes consumed by the Controller source reader."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / path) for path in ("scripts", "agent_protocol/src", "control/src", "tests/scripts")]
import httpx
from agent_package_source_publication import package_source
from vonk_control.agent_package_source import load_package_source


@unittest.skipUnless(Path("/usr/bin/dpkg-deb").exists(), "Debian package producer runs in OrbStack")
class PackageSourcePublicationTests(unittest.TestCase):
    def test_signed_deb_producer_bytes_are_consumed_at_exact_binary_lookup(self):
        from test_install_release_publication import _agent_package
        with tempfile.TemporaryDirectory() as temporary:
            package = _agent_package(Path(temporary), "linux-arm64", "0.1.0")
            source = package_source(package, version="0.1.0")
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
                package_source(package, version="0.1.0")


if __name__ == "__main__":
    unittest.main()
