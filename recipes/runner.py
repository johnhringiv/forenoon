"""Runs Calibre news recipes on a schedule and leaves each issue where Forenoon reads it.

Standard library only: this runs in the recipes container, which has Calibre but not Forenoon.

Reads `recipes.conf` from the output folder (one `folder = Recipe name` per line) and writes
`<folder>/<YYYY-MM-DD>.epub`. A recipe that needs a login takes it from the environment as
RECIPE_<FOLDER>_USERNAME and RECIPE_<FOLDER>_PASSWORD; logins never go in the conf file.

    runner.py            run every day at RECIPES_TIME (default 05:00, in TZ)
    runner.py once       run every recipe now, then exit
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

OUT = Path(os.environ.get("RECIPES_DIR", "/recipes"))
CONF = "recipes.conf"
KEEP_DAYS = int(os.environ.get("RECIPES_KEEP_DAYS", "7"))
TIMEOUT = int(os.environ.get("RECIPES_TIMEOUT", "1800"))  # seconds per recipe
FOLDER = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")


def log(message: str) -> None:
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {message}", flush=True)


def read_conf(path: Path) -> dict[str, str]:
    """folder -> recipe. A recipe is a built-in name, or a .recipe file beside the conf."""
    recipes: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        folder, sep, recipe = (part.strip() for part in line.partition("="))
        if not sep or not recipe or not FOLDER.fullmatch(folder):
            raise ValueError(f"{path.name} line {number}: expected `folder = Recipe name`")
        recipes[folder] = recipe
    return recipes


def command(folder: str, recipe: str, target: Path, out: Path, env: dict[str, str]) -> list[str]:
    if recipe.endswith(".recipe"):
        source = str(out / Path(recipe).name)  # a custom recipe file, never a path outside
    else:
        source = f"{recipe}.recipe"
    cmd = ["ebook-convert", source, str(target), "--output-profile", "generic_eink"]
    prefix = "RECIPE_" + folder.upper().replace("-", "_")
    for flag, key in (("--username", f"{prefix}_USERNAME"), ("--password", f"{prefix}_PASSWORD")):
        if env.get(key):
            cmd += [flag, env[key]]
    return cmd


def run_recipe(folder: str, recipe: str, out: Path = OUT, today: str | None = None) -> bool:
    today = today or datetime.now().strftime("%Y-%m-%d")
    directory = out / folder
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{today}.epub"
    # Built under a name Forenoon ignores, then renamed, so a half-written issue is never read.
    with tempfile.TemporaryDirectory(dir=directory, prefix=".build-") as work:
        target = Path(work) / "issue.epub"
        try:
            # The command line carries the login; it is not logged.
            result = subprocess.run(
                command(folder, recipe, target, out, dict(os.environ)),
                capture_output=True, text=True, timeout=TIMEOUT, check=False,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            log(f"{folder}: timed out after {TIMEOUT}s")
            return False
        except OSError as exc:
            log(f"{folder}: could not start ebook-convert: {exc}")
            return False
        if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            tail = " | ".join((result.stderr or result.stdout).strip().splitlines()[-3:])
            log(f"{folder}: failed (exit {result.returncode}): {tail[:500]}")
            return False
        os.replace(target, final)
    log(f"{folder}: wrote {final.name} ({final.stat().st_size} bytes)")
    cutoff = time.time() - KEEP_DAYS * 86400
    for old in directory.glob("*.epub"):
        if old != final and old.stat().st_mtime < cutoff:
            old.unlink()
    return True


def run_all(out: Path = OUT) -> int:
    """Runs every recipe; returns how many failed."""
    try:
        recipes = read_conf(out / CONF)
    except (OSError, ValueError) as exc:
        log(f"cannot read {CONF}: {exc}")
        return 1
    if not recipes:
        log(f"{CONF} lists no recipes")
    return sum(not run_recipe(folder, recipe, out) for folder, recipe in recipes.items())


def next_run(now: datetime, at: str) -> datetime:
    hour, minute = (int(part) for part in at.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def main(argv: list[str]) -> int:
    if argv[1:] == ["once"]:
        return 1 if run_all() else 0
    at = os.environ.get("RECIPES_TIME", "05:00")
    while True:
        target = next_run(datetime.now().astimezone(), at)
        log(f"next run at {target.isoformat(timespec='minutes')}")
        while (wait := (target - datetime.now().astimezone()).total_seconds()) > 0:
            time.sleep(min(wait, 600))  # short sleeps survive clock changes and suspends
        run_all()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
