FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv
WORKDIR /src
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY feedback ./feedback
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 FORENOON_DATA_DIR=/data FORENOON_CONFIG=/data/config.yaml
WORKDIR /data
# Fixed UID/GID 1000; a bind-mounted data folder must be writable by it
# (compose can override with `user:`). Nothing needs root.
RUN useradd --uid 1000 --create-home forenoon
USER 1000:1000
ENTRYPOINT ["forenoon"]
CMD ["schedule"]
