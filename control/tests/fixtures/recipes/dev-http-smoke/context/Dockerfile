FROM docker.io/library/python:3.12.11-slim-bookworm@sha256:9bb659dc6d5218917236f3711e866a5634bb4c2f208de9d4533aa4863f57c1d3
LABEL ai.vonkforge.runtime-interface="v1"
WORKDIR /app
COPY --chmod=0555 server.py /opt/vonk/bin/vllm
USER 10001:10001
