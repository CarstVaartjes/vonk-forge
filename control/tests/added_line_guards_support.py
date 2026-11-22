from .added_line_guards import check_source

PATH = "control/src/vonk_control/example.py"


_WHOLE_MEMBERSHIP = (
    "def read(session, sets):\n"
    "    every = session.scalars(select(ModelCacheSetArtifact.artifact_sha256))\n"
    "    named = session.scalars(\n"
    "        select(ModelCacheSetArtifact.artifact_sha256).where(\n"
    "            ModelCacheSetArtifact.artifact_set_sha256.in_(sets)\n"
    "        )\n"
    "    )\n"
    "    return every, named\n"
)


_BARRIER_TEST = "control/tests/test_example.py"


_LISTING_PATH = "rust/crates/vonk-agent-helper/src/operations/example.rs"


def _call(callee: str, words: str) -> str:
    return f"fn read(&self) {{\n    let output = self.{callee}(\n        &[{words}],\n        timeout,\n    );\n}}\n"


def _docker_listings(source: str, added: set[int]) -> list[str]:
    return check_source(_LISTING_PATH, source, added, modes=("docker-listings",))
