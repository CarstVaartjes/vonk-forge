FROM ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8

LABEL org.opencontainers.image.source="https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark" \
      org.opencontainers.image.revision="f104c39a8c6fa68d4ea0b342ad6eed172bc0e635" \
      org.opencontainers.image.licenses="MIT AND Apache-2.0" \
      io.vonk.mia-source-archive-sha256="57e72d1ee30aac77aa239c24d0caaba929a981edaf7ffc36dbd98ad315d33355" \
      io.vonk.anemll-source-revision="47503f8e38dadd4dededca798150db2619594fce" \
      io.vonk.pre-patch-tree-sha256="c95903b2148fee727e4e88ffa9b0e65d86239f36bcbec33e352c9a5b92580a03" \
      io.vonk.patch-tree-sha256="25e036c025a68c01de1408450f3ba6d2d368cfab016ea3ba7cc4d4ca63b9a3ba"

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
        /usr/local/lib/python3.12/dist-packages/vllm 25e036c025a68c01de1408450f3ba6d2d368cfab016ea3ba7cc4d4ca63b9a3ba \
    && rm -rf /opt/vonk-build \
    && test -x /opt/vonk/bin/vllm

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    VLLM_NO_USAGE_STATS=1 \
    HOME=/state
WORKDIR /state
USER 10001:10001
ENTRYPOINT []
