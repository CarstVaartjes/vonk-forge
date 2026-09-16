FROM docker.io/library/python:3.14.7-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f
LABEL ai.vonkforge.runtime-interface="v1"
WORKDIR /app
COPY --chmod=0555 server.py /opt/vonk/bin/vllm
USER 10001:10001
