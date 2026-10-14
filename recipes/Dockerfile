# Calibre for running news recipes. Kept apart from the app image: it is large, it is the
# only container that reads subscriber logins, and Calibre must be updated often because recipes break
# when sites change. Rebuild to pick up the newest Calibre, or pin one with CALIBRE_VERSION.
FROM debian:bookworm-slim
ARG CALIBRE_VERSION=
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils python3 tzdata \
      libegl1 libopengl0 libxcb-cursor0 libfontconfig1 libxkbcommon0 libglx0 libnss3 \
      libxcomposite1 libxdamage1 libxrandr2 libxtst6 libasound2 libxkbfile1 \
    && curl -fsSL https://download.calibre-ebook.com/linux-installer.sh -o /tmp/install.sh \
    && sh /tmp/install.sh install_dir=/opt isolated=y ${CALIBRE_VERSION:+version=$CALIBRE_VERSION} \
    && rm -rf /var/lib/apt/lists/* /tmp/install.sh
# Chrome, for article pages read signed in as the reader (see runner.py). It runs unchanged on
# a virtual screen (Xvfb), as an ordinary browser rather than a headless one.
ARG PLAYWRIGHT_VERSION=1.55.0
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends python3-pip xvfb \
    && pip install --no-cache-dir --break-system-packages playwright==${PLAYWRIGHT_VERSION} \
    && python3 -m playwright install --with-deps --no-shell chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache
ENV PATH=/opt/calibre:$PATH QT_QPA_PLATFORM=offscreen HOME=/tmp PYTHONUNBUFFERED=1 RECIPES_DIR=/recipes
COPY runner.py /usr/local/bin/runner.py
# Same fixed UID/GID as the app, so both can use the one data folder. /browser holds what the
# browser keeps of a sign-in: a volume of its own that only this container mounts.
RUN useradd --uid 1000 --create-home forenoon && mkdir /logins /browser && chown 1000:1000 /logins /browser
USER 1000:1000
ENTRYPOINT ["python3", "/usr/local/bin/runner.py"]
