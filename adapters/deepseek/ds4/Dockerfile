FROM nvcr.io/nvidia/cuda:13.0.1-devel-ubuntu24.04@sha256:5c36750138dc1447a17dafbb397674f167d3b44ce18d9160d769df114577b35d AS build

WORKDIR /opt/src
COPY vendor/ds4-84cc882352757baf628a1776badf7cc54d584e28.tar.gz /tmp/ds4.tar.gz
RUN test "$(uname -m)" = aarch64 \
    && echo "3ab2c4485bee87f36166b12ab59abbc293ad9fdfadb1c2920d1cbc7f617da165  /tmp/ds4.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/ds4.tar.gz \
    && mv "ds4-84cc882352757baf628a1776badf7cc54d584e28" ds4 \
    && make -C ds4 cuda-spark \
    && test -x ds4/ds4-server

FROM nvcr.io/nvidia/cuda:13.0.1-runtime-ubuntu24.04@sha256:36050649ad1acc5d3de2c26620191c25850fb12a5771b6c22996033003d952e4

LABEL org.opencontainers.image.source="https://github.com/antirez/ds4" \
      org.opencontainers.image.revision="84cc882352757baf628a1776badf7cc54d584e28" \
      org.opencontainers.image.licenses="MIT" \
      io.vonk.source-archive-sha256="3ab2c4485bee87f36166b12ab59abbc293ad9fdfadb1c2920d1cbc7f617da165"

RUN groupadd --gid 10001 vonk \
    && useradd --uid 10001 --gid 10001 --home-dir /state --no-create-home \
        --shell /usr/sbin/nologin vonk \
    && install --directory --owner 10001 --group 10001 /opt/vonk/bin /state
COPY --from=build --chmod=0755 /opt/src/ds4/ds4-server /opt/vonk/bin/ds4-serve
COPY --from=build /opt/src/ds4/LICENSE /opt/vonk/LICENSE.ds4

ENV HOME=/state \
    HF_HUB_OFFLINE=1
WORKDIR /state
USER 10001:10001
ENTRYPOINT []
