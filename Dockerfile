# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv
WORKDIR /src
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
RUN uv sync --frozen --no-dev --no-editable

# Fonts set in the books (see docs/kindle-books.md), each from a pinned source checked against
# its hash, with its licence: JetBrains Mono NL for code, and Noto Emoji for emoji, which
# is published as a variable font and made a fixed Regular weight here (OFL). Arimo and Tinos
# (Apache 2.0) set the text of SVG pictures (app/images.py): they take the room Arial and
# Times New Roman do; Arimo is published as a variable font, made Regular and Bold here.
FROM python:3.12-slim-bookworm AS fonts
RUN pip install --no-cache-dir fonttools==4.67.0
ADD --checksum=sha256:6f6376c6ed2960ea8a963cd7387ec9d76e3f629125bc33d1fdcd7eb7012f7bbf \
    https://github.com/JetBrains/JetBrainsMono/releases/download/v2.304/JetBrainsMono-2.304.zip /tmp/jbm.zip
ADD --checksum=sha256:de6c18832938afc99caf132b39d6a30a19bac7f2e812e28db2535b4608d27551 \
    https://raw.githubusercontent.com/google/fonts/b979dba422e445492b0eb9951ac52ee0b4d648c3/ofl/notoemoji/NotoEmoji%5Bwght%5D.ttf \
    /tmp/NotoEmoji-var.ttf
ADD --chmod=644 --checksum=sha256:500bb1ccf43df7bbb522112f9133a52b16e1c35e809632f5d8609b179152de5b \
    https://raw.githubusercontent.com/google/fonts/a73b9ab0a5df191bcfed817159a903911ea7958a/ofl/notoemoji/OFL.txt \
    /out/NotoEmoji-OFL.txt
ADD --checksum=sha256:c75270dfa8b5747c666d9e1915b8c9a6cb6e2de74d103cd0f6ee0104675a3604 \
    https://raw.githubusercontent.com/google/fonts/b979dba422e445492b0eb9951ac52ee0b4d648c3/apache/arimo/Arimo%5Bwght%5D.ttf \
    /tmp/Arimo-var.ttf
ADD --chmod=644 --checksum=sha256:1061395ac6775f3cea27dc9ef3d7a3b9cc34c2b4a2d97aa649411294d5165990 \
    https://raw.githubusercontent.com/google/fonts/b979dba422e445492b0eb9951ac52ee0b4d648c3/apache/tinos/Tinos-Regular.ttf /out/Tinos-Regular.ttf
ADD --chmod=644 --checksum=sha256:6a0afe87068ba5ddeb6a9bb7b9d0dc41666966c044767df69de3efb7e33f5af8 \
    https://raw.githubusercontent.com/google/fonts/b979dba422e445492b0eb9951ac52ee0b4d648c3/apache/tinos/Tinos-Bold.ttf /out/Tinos-Bold.ttf
ADD --chmod=644 --checksum=sha256:cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30 \
    https://raw.githubusercontent.com/google/fonts/b979dba422e445492b0eb9951ac52ee0b4d648c3/apache/arimo/LICENSE.txt /out/Arimo-Tinos-LICENSE.txt
RUN <<PY python
import zipfile
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

jbm = zipfile.ZipFile("/tmp/jbm.zip")
open("/out/JetBrainsMonoNL-Regular.ttf", "wb").write(jbm.read("fonts/ttf/JetBrainsMonoNL-Regular.ttf"))
open("/out/JetBrainsMono-OFL.txt", "wb").write(jbm.read("OFL.txt"))
instancer.instantiateVariableFont(TTFont("/tmp/NotoEmoji-var.ttf"), {"wght": 400}).save("/out/NotoEmoji-Regular.ttf")
for weight, name in ((400, "Regular"), (700, "Bold")):
    instancer.instantiateVariableFont(TTFont("/tmp/Arimo-var.ttf"), {"wght": weight}).save(f"/out/Arimo-{name}.ttf")
PY

FROM python:3.12-slim-bookworm
COPY --from=build /opt/venv /opt/venv
COPY --from=fonts /out/ /usr/share/forenoon/fonts/
# The shipped models and their prices; the web pages build in the same file (app/models.py).
COPY web/models.json /usr/share/forenoon/models.json
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 FORENOON_DATA_DIR=/data \
    FORENOON_MODELS=/usr/share/forenoon/models.json \
    FORENOON_CODE_FONT=/usr/share/forenoon/fonts/JetBrainsMonoNL-Regular.ttf \
    FORENOON_EMOJI_FONT=/usr/share/forenoon/fonts/NotoEmoji-Regular.ttf \
    FORENOON_FONT_DIR=/usr/share/forenoon/fonts
WORKDIR /data
# Fixed UID/GID 1000; a bind-mounted data folder must be writable by it
# (compose can override with `user:`). Nothing needs root.
RUN useradd --uid 1000 --create-home forenoon
USER 1000:1000
ENTRYPOINT ["forenoon"]
CMD ["schedule"]
