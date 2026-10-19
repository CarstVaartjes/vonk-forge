"""The image correction must preserve, use and require its upstream credential."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

PATCH = Path(__file__).resolve().parents[1] / "litellm/patch-vllm-passthrough.py"


def test_patched_provider_uses_configured_bearer_without_losing_headers(
    tmp_path: Path,
) -> None:
    module = tmp_path / "transformation.py"
    module.write_text(
        "class VLLMModelInfo:\n"
        "    @staticmethod\n"
        "    def get_api_key(api_key=None):\n"
        "        return None\n"
        "class BasePassthroughConfig:\n"
        "    pass\n"
        "class VLLMPassthroughConfig(VLLMModelInfo, BasePassthroughConfig):\n"
        "    pass\n"
    )
    runpy.run_path(str(PATCH))["patch"](module)
    provider = runpy.run_path(str(module))["VLLMPassthroughConfig"]()
    key = provider.get_api_key("fixture-upstream-key")
    headers = provider.validate_environment(
        {"Content-Type": "application/json", "Authorization": "Bearer client-key"},
        "d3-edge",
        [],
        {},
        {},
        api_key=key,
    )
    assert headers == {
        "Content-Type": "application/json",
        "Authorization": "Bearer fixture-upstream-key",
    }
    with pytest.raises(ValueError, match="configured upstream key"):
        provider.validate_environment({}, "d3-edge", [], {}, {}, api_key=None)


def test_changed_upstream_layout_fails_image_build_without_modifying_it(
    tmp_path: Path,
) -> None:
    module = tmp_path / "transformation.py"
    source = "class RenamedProvider:\n    pass\n"
    module.write_text(source)
    with pytest.raises(ValueError, match="class changed"):
        runpy.run_path(str(PATCH))["patch"](module)
    assert module.read_text() == source
