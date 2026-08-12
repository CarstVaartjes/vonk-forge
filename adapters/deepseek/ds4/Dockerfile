FROM nvcr.io/nvidia/cuda:13.0.1-devel-ubuntu24.04@sha256:7d2f6a8c2071d911524f95061a0db363e24d27aa51ec831fcccf9e76eb72bc92 AS build

ARG DS4_SOURCE_COMMIT=4ad370b4a338efe9723a386673c0e04f6e214108
ARG DS4_SOURCE_ARCHIVE_SHA256=7db338d0a441fed36c5e4e7af44ff670e8bfe567e88d482f00ff6a3dc0e5dbe3
ARG DS4_PATCH_SHA256=bbf9578ba17e9ddd5c080881ac1ce075590fcd47877fb8dc5cc32521b1e938e1

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential ca-certificates curl patch \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/src
COPY patches/served-model-name.patch /tmp/served-model-name.patch
RUN curl --fail --location --silent --show-error --output /tmp/ds4.tar.gz \
        https://github.com/Entrpi/ds4/archive/4ad370b4a338efe9723a386673c0e04f6e214108.tar.gz \
    && echo "${DS4_SOURCE_ARCHIVE_SHA256}  /tmp/ds4.tar.gz" | sha256sum -c - \
    && echo "${DS4_PATCH_SHA256}  /tmp/served-model-name.patch" | sha256sum -c - \
    && tar -xzf /tmp/ds4.tar.gz \
    && mv ds4-${DS4_SOURCE_COMMIT} ds4 \
    && cd ds4 \
    && patch -p1 --input /tmp/served-model-name.patch \
    && make cuda-spark \
    && test -x ds4-server \
    && grep --fixed-strings 'CUDA_ARCH := sm_121' .ds4-cuda-config.mk \
    && grep --fixed-strings 'DS4_CUDA_SPARK_HBM_CACHE=1' .ds4-cuda-config.mk

FROM nvcr.io/nvidia/cuda:13.0.1-runtime-ubuntu24.04@sha256:c3fde347d52d578c84fd644bc177bc7ec333feaf11550d990da4084d7612e4c7

ARG DS4_SOURCE_COMMIT=4ad370b4a338efe9723a386673c0e04f6e214108
ARG DS4_SOURCE_ARCHIVE_SHA256=7db338d0a441fed36c5e4e7af44ff670e8bfe567e88d482f00ff6a3dc0e5dbe3
ARG DS4_PATCH_SHA256=bbf9578ba17e9ddd5c080881ac1ce075590fcd47877fb8dc5cc32521b1e938e1

LABEL org.opencontainers.image.source="https://github.com/Entrpi/ds4.git" \
      org.opencontainers.image.revision="${DS4_SOURCE_COMMIT}" \
      io.node.ds4.source-archive-sha256="${DS4_SOURCE_ARCHIVE_SHA256}" \
      io.node.ds4.patch-sha256="${DS4_PATCH_SHA256}" \
      io.node.ds4.cuda-build-target="compute_121a/sm_121a;DS4_CUDA_SPARK_HBM_CACHE=1" \
      io.node.ds4.served-model-id="deepseek" \
      ai.vonkforge.runtime-interface="v1"

RUN apt-get update \
    && apt-get install --no-install-recommends -y busybox-static \
    && test -x /bin/busybox \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 ds4 \
    && useradd --uid 10001 --gid 10001 --home-dir /var/lib/ds4 --create-home --shell /usr/sbin/nologin ds4 \
    && install --directory --owner 10001 --group 10001 /opt/ds4 /var/log/ds4

COPY --from=build /opt/src/ds4/ds4-server /opt/ds4/ds4-server
COPY --from=build /opt/src/ds4/ds4_weight_server /opt/ds4/ds4_weight_server
COPY --from=build /opt/src/ds4/LICENSE /opt/ds4/LICENSE
COPY --from=build /opt/src/ds4/VERSION /opt/ds4/VERSION

ENV DS4_NO_UPDATE_CHECK=1 \
    HOME=/var/lib/ds4

USER 10001:10001
WORKDIR /opt/ds4
ENTRYPOINT ["/opt/ds4/ds4-server"]
