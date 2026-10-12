# Build from the workspace root so the shared uv.lock is available (.dockerignore keeps every
# .env out of the image; settings come from the environment at run time):
#   docker build -f everything/Dockerfile -t fauxcall-everything .
# It installs FauxCall from PyPI: a workspace that uses a local FauxCall checkout can't build images.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy FAUXCALL_HOST=0.0.0.0
WORKDIR /workspace
COPY . /workspace
RUN uv sync --package fauxcall-everything --no-dev --frozen

EXPOSE 8701
CMD ["uv", "run", "--no-sync", "--package", "fauxcall-everything", "fauxcall-everything"]
