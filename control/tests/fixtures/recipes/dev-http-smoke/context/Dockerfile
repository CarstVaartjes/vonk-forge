FROM docker.io/library/python:3.14.7-slim-trixie@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
LABEL ai.vonkforge.runtime-interface="v1"
WORKDIR /app
COPY --chmod=0555 server.py /opt/vonk/bin/vllm
USER 10001:10001
