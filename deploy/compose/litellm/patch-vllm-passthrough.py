"""Bind native vLLM passthrough to the configured upstream Bearer credential.

LiteLLM 1.103.0's VLLMModelInfo drops api_key and otherwise emits x-api-key.
vLLM's server authenticates Authorization: Bearer. Keep this correction scoped
to the passthrough provider; the proxy's client authentication remains unchanged.
"""

from pathlib import Path


def patch(path: Path) -> None:
    source = path.read_text()
    anchor = "class VLLMPassthroughConfig(VLLMModelInfo, BasePassthroughConfig):\n"
    if source.count(anchor) != 1:
        raise ValueError(
            "pinned vLLM passthrough class changed; review the credential patch"
        )
    methods = """    @staticmethod
    def get_api_key(api_key=None):
        return api_key

    def validate_environment(self, headers, model, messages, optional_params,
                             litellm_params, api_key=None, api_base=None):
        if not api_key:
            raise ValueError("vLLM passthrough requires its configured upstream key")
        headers["Authorization"] = "Bearer " + api_key
        return headers

"""
    path.write_text(source.replace(anchor, anchor + methods))


if __name__ == "__main__":
    import importlib.util

    spec = importlib.util.find_spec("litellm.llms.vllm.passthrough.transformation")
    if spec is None or spec.origin is None:
        raise RuntimeError("pinned LiteLLM vLLM passthrough module is absent")
    patch(Path(spec.origin))
