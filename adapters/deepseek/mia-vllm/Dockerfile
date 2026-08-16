FROM ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8

LABEL org.opencontainers.image.source="https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark" \
      org.opencontainers.image.revision="f752cd04ab30f2cf42077dd8811a5e1e682d63e7" \
      org.opencontainers.image.licenses="MIT AND Apache-2.0" \
      io.vonk.mia-source-archive-sha256="1b5abd647bbead78b1863632424ac332b9e0044e521743813058ac07f3606e04" \
      io.vonk.anemll-source-revision="47503f8e38dadd4dededca798150db2619594fce" \
      io.vonk.pre-patch-tree-sha256="c95903b2148fee727e4e88ffa9b0e65d86239f36bcbec33e352c9a5b92580a03" \
      io.vonk.patch-tree-sha256="d8995ee56a347544e794269479595768a4aac24b6fac447bf599dc04b73b3325"

COPY encoding /opt/vonk-build/encoding
COPY patches /opt/vonk-build/patches
COPY licenses /opt/vonk/licenses
COPY --chmod=0755 apply-build-patches.py verify-patched-tree.py /opt/vonk-build/
COPY --chmod=0755 vllm-wrapper.py /opt/vonk/bin/vllm

RUN install --directory --owner 10001 --group 10001 /state /outputs \
    && python3 /opt/vonk-build/verify-patched-tree.py \
        /usr/local/lib/python3.12/dist-packages/vllm c95903b2148fee727e4e88ffa9b0e65d86239f36bcbec33e352c9a5b92580a03 \
    && python3 /opt/vonk-build/apply-build-patches.py \
    && python3 /opt/vonk-build/verify-patched-tree.py \
        /usr/local/lib/python3.12/dist-packages/vllm d8995ee56a347544e794269479595768a4aac24b6fac447bf599dc04b73b3325 \
    && rm -rf /opt/vonk-build \
    && test -x /opt/vonk/bin/vllm

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    VLLM_NO_USAGE_STATS=1 \
    HOME=/state
WORKDIR /state
USER 10001:10001
ENTRYPOINT []
