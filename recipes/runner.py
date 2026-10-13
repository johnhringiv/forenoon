"""Runs Calibre news recipes on a schedule and leaves each issue where Forenoon reads it.

Standard library only: this runs in the recipes container, which has Calibre but not Forenoon.

Reads `recipes.conf` from the output folder (one `folder = Recipe name` per line; the web
pages write it from the Sources page) and writes `<folder>/<YYYY-MM-DD>.epub`. A recipe's
login is the file `<folder>.json` in the logins folder, which only this container and the web
pages mount; logins never go in the conf file or the output folder.

It also leaves two files for the web pages: `catalogue.json`, the recipes this Calibre has
and which of them want a login, and `status.json`, how each recipe's last run went. A file
named for a folder in `.requests/` asks for that recipe to be run now.

    runner.py            run every day at RECIPES_TIME (default 05:00, in TZ)
    runner.py once       run every recipe now, then exit
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

OUT = Path(os.environ.get("RECIPES_DIR", "/recipes"))
LOGINS = Path(os.environ.get("RECIPES_LOGINS", "/logins"))
CALIBRE_INDEX = Path(os.environ.get("CALIBRE_RECIPES", "/opt/calibre/resources/builtin_recipes.xml"))
CONF = "recipes.conf"
CATALOGUE = "catalogue.json"
STATUS = "status.json"
REQUESTS = ".requests"
POLL = 20  # seconds between looks at the requests folder
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


def write_json(path: Path, data: object) -> None:
    """Whole or not at all: the web pages read these while this container writes them."""
    partial = path.with_name(f".{path.name}.tmp")
    partial.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(partial, path)


def write_catalogue(out: Path = OUT, index: Path = CALIBRE_INDEX) -> int:
    """List what can be chosen on the Sources page: Calibre's recipes and any .recipe file here."""
    recipes = []
    try:
        for node in ElementTree.parse(index).getroot():
            title = node.get("title") or ""
            needs = node.get("needs_subscription") or "no"
            # A title the conf file could not carry is left out rather than half-working.
            if title and title == title.strip() and "#" not in title and "=" not in title:
                recipes.append(
                    {"title": title, "language": node.get("language") or "", "login": needs if needs in ("yes", "optional") else "no"}
                )  # fmt: skip
    except (OSError, ElementTree.ParseError) as exc:
        log(f"cannot read Calibre's recipe list at {index}: {exc}")
    custom = sorted(p.name for p in out.glob("*.recipe") if p.is_file())
    write_json(out / CATALOGUE, {"written": datetime.now().astimezone().isoformat(timespec="seconds"), "recipes": recipes, "custom": custom})  # fmt: skip
    return len(recipes) + len(custom)


def read_login(folder: str, logins: Path = LOGINS) -> dict[str, str]:
    try:
        saved = json.loads((logins / f"{folder}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in saved.items() if k in ("username", "password") and isinstance(v, str) and v}


def command(folder: str, recipe: str, target: Path, out: Path, login: dict[str, str]) -> list[str]:
    if recipe.endswith(".recipe"):
        source = str(out / Path(recipe).name)  # a custom recipe file, never a path outside
    else:
        source = f"{recipe}.recipe"
    cmd = ["ebook-convert", source, str(target), "--output-profile", "generic_eink"]
    for flag, key in (("--username", "username"), ("--password", "password")):
        if login.get(key):
            cmd += [flag, login[key]]
    return cmd


def record(out: Path, folder: str, ok: bool, note: str, running: bool = False) -> None:
    """Keep how this recipe's last run went, or that one has begun, for the Sources page."""
    try:
        status = json.loads((out / STATUS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    if not isinstance(status, dict):
        status = {}
    at = datetime.now().astimezone().isoformat(timespec="seconds")
    status[folder] = {"at": at, "ok": ok, "running": running, "note": note[:300]}
    try:
        write_json(out / STATUS, status)
    except OSError as exc:
        log(f"cannot write {STATUS}: {exc}")


def run_recipe(folder: str, recipe: str, out: Path = OUT, today: str | None = None) -> bool:
    record(out, folder, False, "running", running=True)
    ok, note = _run_recipe(folder, recipe, out, today)
    log(f"{folder}: {note}")
    record(out, folder, ok, note)
    return ok


def _run_recipe(folder: str, recipe: str, out: Path, today: str | None) -> tuple[bool, str]:
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
                command(folder, recipe, target, out, read_login(folder)),
                capture_output=True, text=True, timeout=TIMEOUT, check=False,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            return False, f"timed out after {TIMEOUT}s"
        except OSError as exc:
            return False, f"could not start ebook-convert: {exc}"
        if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            tail = " | ".join((result.stderr or result.stdout).strip().splitlines()[-3:])
            return False, f"failed (exit {result.returncode}): {tail[:500]}"
        os.replace(target, final)
    cutoff = time.time() - KEEP_DAYS * 86400
    for old in directory.glob("*.epub"):
        if old != final and old.stat().st_mtime < cutoff:
            old.unlink()
    return True, f"wrote {final.name} ({final.stat().st_size} bytes)"


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


def run_requested(out: Path = OUT) -> int:
    """Run the recipes the web pages asked for (a new source, a changed login, "Fetch now")."""
    requests = out / REQUESTS
    if not requests.is_dir():
        return 0
    asked = sorted(p for p in requests.iterdir() if p.is_file())
    if not asked:
        return 0
    try:
        recipes = read_conf(out / CONF)
    except (OSError, ValueError) as exc:
        log(f"cannot read {CONF}: {exc}")
        recipes = {}
    for request in asked:
        request.unlink(missing_ok=True)  # first, so a recipe that crashes is not run in a loop
        if request.name in recipes:
            run_recipe(request.name, recipes[request.name], out)
        elif FOLDER.fullmatch(request.name):
            record(out, request.name, False, f"not listed in {CONF}")
    return len(asked)


def next_run(now: datetime, at: str) -> datetime:
    hour, minute = (int(part) for part in at.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def main(argv: list[str]) -> int:
    if argv[1:] == ["once"]:
        write_catalogue()
        return 1 if run_all() else 0
    at = os.environ.get("RECIPES_TIME", "05:00")
    while True:
        try:
            log(f"catalogue written: {write_catalogue()} recipes")
        except OSError as exc:
            log(f"cannot write {CATALOGUE}: {exc}")
        target = next_run(datetime.now().astimezone(), at)
        log(f"next run at {target.isoformat(timespec='minutes')}")
        while (wait := (target - datetime.now().astimezone()).total_seconds()) > 0:
            time.sleep(min(wait, POLL))  # short sleeps survive clock changes and suspends
            try:
                run_requested()
            except OSError as exc:
                log(f"requests not handled: {exc}")
        run_all()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
