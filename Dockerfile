# drkit in a container, for hosts where installing Python tools is not wanted.
#
#   docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v /etc/drkit:/etc/drkit:ro \
#     -v /srv:/srv -v /var/lib/docker/volumes:/var/lib/docker/volumes:ro \
#     -v /var/lib/drkit:/var/lib/drkit drkit backup
#
# The container drives the host's Docker through its socket and reads what it backs up from
# the mounted paths, so mount them at the same locations as on the host. Bases are pinned by
# digest, restic is built from its checksummed source.

FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

# restic from its release source (checksum verified), built with a current Go toolchain and
# the dependencies that carry published fixes: the release binary still ships the Go
# standard library and modules it was built with, several of them with HIGH advisories.
FROM golang:1.27.3-bookworm@sha256:dc0963f32053e4bc451d24d87e3892d42d01021b61f093e98eb95c0352bb84b0 AS restic
ARG RESTIC_VERSION=0.19.1
ARG RESTIC_SOURCE_SHA256=bb9b1a19040744d26d8a79be029d4e6b189c45ccc9d8831d7fe367d3c33df725
WORKDIR /src
RUN curl -fsSL -o restic.tar.gz \
        "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic-${RESTIC_VERSION}.tar.gz" \
    && echo "${RESTIC_SOURCE_SHA256}  restic.tar.gz" | sha256sum --check --strict \
    && tar -xzf restic.tar.gz --strip-components=1 \
    && go get \
        github.com/go-jose/go-jose/v4@v4.1.6 \
        github.com/klauspost/compress@v1.18.7 \
        golang.org/x/crypto@v0.57.0 \
        golang.org/x/net@v0.60.0 \
        golang.org/x/text@v0.42.0 \
        google.golang.org/grpc@v1.83.2 \
    && go mod tidy \
    && CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o /restic ./cmd/restic \
    && /restic version

FROM python:3.12-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY examples/demo-app ./examples/demo-app
# Dependencies at the versions of uv.lock, then drkit itself, in a self-contained venv.
RUN uv venv /opt/drkit \
    && uv export --frozen --no-dev --no-emit-project --format requirements-txt > requirements.txt \
    && uv pip install --python /opt/drkit/bin/python --require-hashes -r requirements.txt \
    && uv build --wheel --out-dir /dist \
    && uv pip install --python /opt/drkit/bin/python --no-deps /dist/*.whl

FROM python:3.12-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# Docker CLI and Compose from Docker's signed repository.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg git openssh-client \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && echo "deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
COPY --from=restic /restic /usr/local/bin/restic
COPY --from=build /opt/drkit /opt/drkit
ENV PATH="/opt/drkit/bin:${PATH}"
LABEL org.opencontainers.image.title="drkit" \
      org.opencontainers.image.description="Backup and disaster recovery for Docker Compose servers" \
      org.opencontainers.image.source="https://github.com/caiodassumpcao/drkit" \
      org.opencontainers.image.licenses="MIT"
# Root: drkit reads volume data and the root-owned /etc/drkit, as on the host (DESIGN §1.3).
ENTRYPOINT ["drkit"]
CMD ["--help"]
