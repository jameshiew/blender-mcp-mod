FROM rust:1-bookworm AS builder

WORKDIR /app

COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY resources ./resources
COPY addon.py ./addon.py
RUN cargo build --release --locked

FROM debian:bookworm-slim
COPY --from=builder /app/target/release/blender-mcp /usr/local/bin/blender-mcp
ENV BLENDER_HOST=host.docker.internal \
    BLENDER_PORT=9876

ENTRYPOINT ["blender-mcp"]
