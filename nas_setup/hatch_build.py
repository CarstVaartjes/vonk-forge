"""Bundle the canonical protocol owner's files into the portable NAS wheel."""

from pathlib import Path
from typing import cast

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: object) -> None:
        if self.target_name != "wheel" or version == "editable":
            return
        owner = Path(self.root).parent / "agent_protocol/src/vonk_agent_protocol"
        if not owner.is_dir():
            raise ValueError("canonical agent protocol source is unavailable")
        # Hatch owns the full build record; validate only the path mapping this
        # hook consumes and modifies at that external boundary.
        if not isinstance(build_data, dict):
            raise TypeError("Hatch build data must be a dictionary")
        raw_include = build_data.setdefault("force_include", {})
        if not isinstance(raw_include, dict) or any(
            not isinstance(source, str) or not isinstance(target, str)
            for source, target in raw_include.items()
        ):
            raise TypeError("Hatch force_include must map source paths to target paths")
        include = cast(dict[str, str], raw_include)
        for path in sorted(owner.rglob("*")):
            if path.is_file() and (
                path.suffix in {".py", ".json"} or path.name == "py.typed"
            ):
                include[str(path)] = (
                    "vonk_agent_protocol/" + path.relative_to(owner).as_posix()
                )
