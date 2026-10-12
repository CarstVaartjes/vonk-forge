"""Bundle the canonical protocol owner's files into the portable NAS wheel."""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        if self.target_name != "wheel" or version == "editable":
            return
        owner = Path(self.root).parent / "agent_protocol/src/vonk_agent_protocol"
        if not owner.is_dir():
            raise ValueError("canonical agent protocol source is unavailable")
        include = build_data.setdefault("force_include", {})
        assert isinstance(include, dict)
        for path in sorted(owner.rglob("*")):
            if path.is_file() and (
                path.suffix in {".py", ".json"} or path.name == "py.typed"
            ):
                include[str(path)] = (
                    "vonk_agent_protocol/" + path.relative_to(owner).as_posix()
                )
