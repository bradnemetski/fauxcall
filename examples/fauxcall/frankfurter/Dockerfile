# Build from the workspace root so the shared uv.lock is available:
#   docker build -f frankfurter/Dockerfile -t fauxcall-frankfurter .
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy FAUXCALL_HOST=0.0.0.0
WORKDIR /workspace
COPY . /workspace
RUN uv sync --package fauxcall-frankfurter --no-dev --frozen

EXPOSE 8701
CMD ["uv", "run", "--no-sync", "--package", "fauxcall-frankfurter", "fauxcall-frankfurter"]
