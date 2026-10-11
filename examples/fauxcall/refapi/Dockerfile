# Build from the workspace root so the shared uv.lock is available:
#   docker build -f refapi/Dockerfile -t fauxcall-refapi .
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy FAUXCALL_HOST=0.0.0.0
WORKDIR /workspace
COPY . /workspace
RUN uv sync --package fauxcall-refapi --no-dev --frozen

EXPOSE 8701
CMD ["uv", "run", "--no-sync", "--package", "fauxcall-refapi", "fauxcall-refapi"]
