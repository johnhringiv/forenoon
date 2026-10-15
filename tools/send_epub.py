"""Send one EPUB to a Kindle address through the configured mail server, for checks on the device.

    set -a && . ~/forenoon-deploy/forenoon.env && set +a
    uv run python tools/send_epub.py "run/out/FN golden rendering test.epub" you_123@kindle.com

The mail settings come from the environment, as for `forenoon run` (FORENOON_SMTP_HOST,
FORENOON_SMTP_USERNAME, FORENOON_SMTP_SENDER, FORENOON_SMTP_PASSWORD). Give a resent book a new
title and identifier, or the Kindle treats it as the book it already has.
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.config import User, from_env
from app.deliver import Attachment, build_messages, send


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    path, address = Path(argv[1]), argv[2]
    smtp = from_env().smtp
    user = User(id="device-test", name="Device test", kindle_addresses=(address,))
    messages = [msg for msg, _ in build_messages(user, [Attachment(path.name, path.read_bytes())], smtp)]
    send(messages, smtp)
    print(f"sent {path.name} ({path.stat().st_size} bytes) from {smtp.sender} to {address}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
