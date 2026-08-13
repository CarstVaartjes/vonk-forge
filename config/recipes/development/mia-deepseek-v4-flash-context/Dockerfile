FROM ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8
USER 0
COPY --chmod=0644 encoding/encoding_dsv4.py /opt/vonk/encoding/encoding_dsv4.py
COPY --chmod=0755 patches/ /opt/vonk/patches/
COPY --chmod=0755 apply-reasoning-default.py /opt/vonk/apply-reasoning-default.py
COPY --chmod=0755 resolve-model.py /opt/vonk/resolve-model.py
COPY --chmod=0755 resolve-roce.py /opt/vonk/resolve-roce.py
COPY --chmod=0755 mia-deepseek-v4-flash /opt/vonk/mia-deepseek-v4-flash
RUN set -eux; \
    cp /opt/vonk/encoding/encoding_dsv4.py /usr/local/lib/python3.12/dist-packages/vllm/tokenizers/deepseek_v4_encoding.py; \
    python3 /opt/vonk/patches/hotfix-encoding-dsv4-issue21.py; \
    bash /opt/vonk/patches/hotfix-nvfp4-ds-mla-issue22.sh; \
    bash /opt/vonk/patches/hotfix-dsv4-mtp-buffer-50312.sh; \
    bash /opt/vonk/patches/hotfix-dsv4-adaptive-topk-50004.sh; \
    bash /opt/vonk/patches/hotfix-dsv4-skip-topk-49486.sh; \
    bash /opt/vonk/patches/hotfix-dsv4-dense-prefill-indexer-48407.sh; \
    bash /opt/vonk/patches/hotfix-dsv4-skip-empty-c128-48957.sh; \
    bash /opt/vonk/patches/hotfix-dsv4-flashmla-workspace-50298.sh; \
    bash /opt/vonk/patches/hotfix-dsv4-grammar-advance.sh; \
    python3 /opt/vonk/patches/hotfix-dsv4-issue27-partial-prefill-concurrency.py; \
    python3 /opt/vonk/patches/hotfix-dsv4-issue43-decode-fairness-and-diag.py; \
    python3 /opt/vonk/patches/hotfix-dsv4-issue26-hybrid-swa-min.py; \
    python3 /opt/vonk/patches/hotfix-dsv4-suppress-stops-in-reasoning.py; \
    python3 /opt/vonk/apply-reasoning-default.py; \
    python3 -m py_compile \
      /opt/vonk/resolve-model.py \
      /opt/vonk/resolve-roce.py \
      /usr/local/lib/python3.12/dist-packages/vllm/v1/core/sched/scheduler.py \
      /usr/local/lib/python3.12/dist-packages/vllm/tokenizers/deepseek_v4_encoding.py \
      /usr/local/lib/python3.12/dist-packages/vllm/tokenizers/deepseek_v4.py
LABEL ai.vonkforge.runtime-interface="v1"
USER 10001:10001
ENTRYPOINT []
