# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv
WORKDIR /src
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY feedback ./feedback
RUN uv sync --frozen --no-dev --no-editable

# The code font set in the books (see docs/kindle-books.md): one file from a pinned JetBrains
# Mono release, checked against its hash. It is OFL-licensed, so its licence travels with it.
FROM python:3.12-slim-bookworm AS font
ADD --checksum=sha256:6f6376c6ed2960ea8a963cd7387ec9d76e3f629125bc33d1fdcd7eb7012f7bbf \
    https://github.com/JetBrains/JetBrainsMono/releases/download/v2.304/JetBrainsMono-2.304.zip /tmp/font.zip
RUN python -c "import zipfile; z = zipfile.ZipFile('/tmp/font.zip'); \
    [open('/tmp/' + n.rsplit('/', 1)[-1], 'wb').write(z.read(n)) \
     for n in ('fonts/ttf/JetBrainsMonoNL-Regular.ttf', 'OFL.txt')]"

FROM python:3.12-slim-bookworm
COPY --from=build /opt/venv /opt/venv
COPY --from=font /tmp/JetBrainsMonoNL-Regular.ttf /tmp/OFL.txt /usr/share/forenoon/fonts/
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 FORENOON_DATA_DIR=/data \
    FORENOON_CODE_FONT=/usr/share/forenoon/fonts/JetBrainsMonoNL-Regular.ttf
WORKDIR /data
# Fixed UID/GID 1000; a bind-mounted data folder must be writable by it
# (compose can override with `user:`). Nothing needs root.
RUN useradd --uid 1000 --create-home forenoon
USER 1000:1000
ENTRYPOINT ["forenoon"]
CMD ["schedule"]
