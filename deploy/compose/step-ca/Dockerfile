FROM --platform=$BUILDPLATFORM golang:1.25.8-bookworm@sha256:4557cf171e3cdf5053a298d5171b1a5f5734d920260c25f22c79e94760eb2084 AS builder

ADD --checksum=sha256:744e4bc95fa41c6601872a897d1ea8ef6a71eff8de63076c9464b74ca030021d https://codeload.github.com/smallstep/certificates/tar.gz/6e8ec61405239cf3f37b2bbf260a587b7d2e4e31 /tmp/certificates.tar.gz
WORKDIR /src/deploy/compose/step-ca
COPY deploy/compose/step-ca/upstream/context-signing.patch upstream/context-signing.patch
RUN mkdir upstream/certificates && tar -xzf /tmp/certificates.tar.gz --strip-components=1 -C upstream/certificates \
    && cd upstream/certificates && git apply --check ../context-signing.patch && git apply ../context-signing.patch
COPY deploy/compose/step-ca/service service
WORKDIR /src/deploy/compose/step-ca/service
ENV GOTOOLCHAIN=local CGO_ENABLED=0
# The same pinned compiler runs natively; only the static Go output targets
# the requested runtime platform. Module verification remains shared and exact.
RUN go mod download && go mod verify
ARG TARGETOS
ARG TARGETARCH
RUN GOOS="${TARGETOS:?}" GOARCH="${TARGETARCH:?}" \
    go build -mod=readonly -trimpath -buildvcs=false -o /out/vonk-step-ca .

# Preserve the existing step health CLI, UID, home, trust mounts and Badger
# volume. Only the CA executable changes; this is the existing CA service.
FROM smallstep/step-ca:0.30.2@sha256:a2b17872915c193259b75a5474c398326f41bd199f0842093e52cf4182bc8270 AS runtime
USER 0:0
RUN rm /usr/local/bin/step-ca /entrypoint.sh
COPY --from=builder --chmod=0555 /out/vonk-step-ca /usr/local/bin/vonk-step-ca
USER 1000:1000
ENTRYPOINT ["vonk-step-ca"]
CMD ["--config", "/run/vonk-normalized-secrets/step-ca/ca.json", "--password-file", "/run/vonk-normalized-secrets/step-ca/password"]
HEALTHCHECK CMD step ca health --ca-url https://step-ca:9000 --root /run/vonk-normalized-secrets/step-ca/root-certificate
