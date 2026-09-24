FROM --platform=linux/arm64 docker.io/vllm/vllm-openai-cpu@sha256:527ec4e8188f2ad480aca5863ab3b7e7c39cfda84f6c0bbb06525363a3eb5a0f

# Match the executable path emitted by the canonical vLLM harness.
RUN test -x /opt/venv/bin/vllm \
    && install -d /opt/vonk/bin \
    && ln -s /opt/venv/bin/vllm /opt/vonk/bin/vllm

LABEL ai.vonkforge.runtime-interface="v1"
