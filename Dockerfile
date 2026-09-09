FROM rust:1-bookworm AS builder

WORKDIR /app

COPY Cargo.toml Cargo.lock ./
COPY build.rs uv.lock LICENSE ./
COPY src ./src
COPY resources ./resources
COPY extension ./extension
RUN cargo build --release --locked

FROM debian:bookworm-slim
COPY --from=builder /app/target/release/blender-mcp /usr/local/bin/blender-mcp
ENV BLENDER_HOST=host.docker.internal \
    BLENDER_PORT=9876

ENTRYPOINT ["blender-mcp"]
