# Chrome, for article pages read signed in as the reader (see fetcher.py). Kept apart from the
# app image: it is large, and it is the only container that reads subscriber cookies. Chrome
# runs unchanged on a virtual screen (Xvfb), as an ordinary browser rather than a headless one.
FROM debian:bookworm-slim
ARG PLAYWRIGHT_VERSION=1.55.0
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates python3 python3-pip tzdata xvfb \
    && pip install --no-cache-dir --break-system-packages playwright==${PLAYWRIGHT_VERSION} \
    && python3 -m playwright install --with-deps --no-shell chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache
ENV HOME=/tmp PYTHONUNBUFFERED=1 SESSIONS_DIR=/sessions
COPY fetcher.py /usr/local/bin/fetcher.py
# Docker's default seccomp profile for 64-bit x86 and ARM only (no 32-bit system calls), plus what Chrome's own sandbox needs to make namespaces.
# The container must run with it (deploy.sh copies it out of the image; docker-compose.yml
# names this file): pictures of any site's figures are taken only with the sandbox on.
COPY seccomp.json /etc/forenoon/seccomp.json
# Same fixed UID/GID as the app, so both can use the one data folder. /browser holds what the
# browser keeps of a sign-in: a volume of its own that only this container mounts.
RUN useradd --uid 1000 --create-home forenoon && mkdir /logins /browser && chown 1000:1000 /logins /browser
USER 1000:1000
ENTRYPOINT ["python3", "/usr/local/bin/fetcher.py"]
