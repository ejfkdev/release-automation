#!/usr/bin/env python3
"""Report channel drift and emit publish matrices.

Compares, for every catalog program:

    GitHub latest release  vs  npm latest  vs  WinGet latest manifest

`--plan --json` prints the two matrices consumed by the publish workflow, so a
scheduled/manual run only touches programs that are actually behind.

Usage:
    python3 tool/status.py
    python3 tool/status.py --json
    python3 tool/status.py --plan --json
    python3 tool/status.py --fail-on-drift
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync  # noqa: E402


def version_key(value: str) -> tuple:
    core, _, pre = value.partition("-")
    core = core.split("+")[0]
    numbers = tuple(int(part) for part in re.findall(r"\d+", core))
    return (numbers, 1 if pre else 0, pre)


def npm_latest(package: str) -> str | None:
    url = f"https://registry.npmjs.org/{urllib.parse.quote(package, safe='')}"
    request = urllib.request.Request(
        url, headers={"User-Agent": "ejfkdev-release-automation"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    except urllib.error.URLError:
        return None
    tags = data.get("dist-tags") or {}
    version = tags.get("latest")
    return version if isinstance(version, str) else None


def npm_version_exists(package: str, version: str) -> bool:
    url = f"https://registry.npmjs.org/{urllib.parse.quote(package, safe='')}"
    request = urllib.request.Request(
        url, headers={"User-Agent": "ejfkdev-release-automation"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise
    except urllib.error.URLError:
        return False
    return version in (data.get("versions") or {})


def winget_latest(program: dict) -> str | None:
    identifier = program["winget"]["id"]
    parts = identifier.split(".")
    path = (
        f"repos/microsoft/winget-pkgs/contents/manifests/"
        f"{identifier[0].lower()}/{parts[0]}/{'.'.join(parts[1:])}"
    )
    data = sync.api_json(path, allow_404=True)
    if not isinstance(data, list):
        return None
    versions = [
        entry["name"]
        for entry in data
        if entry.get("type") == "dir" and re.match(r"^\d", entry["name"])
    ]
    if not versions:
        return None
    return max(versions, key=version_key)


def release_latest(program: dict) -> str | None:
    try:
        release = sync.fetch_release(program)
    except SystemExit:
        return None
    tag = release["tag_name"]
    try:
        return sync.version_from_tag(program, tag)
    except SystemExit:
        return tag.lstrip("v")


def collect(programs: list[dict], checksums: dict, live: bool) -> list[dict]:
    rows: list[dict] = []
    for program in programs:
        name = program["name"]
        record = checksums.get(name) or {}
        target = record.get("version")
        if live:
            release = release_latest(program)
        else:
            release = target
        row = {
            "program": name,
            "release": release,
            "target": target,
            "npm": None,
            "winget": None,
        }
        if program.get("npm"):
            row["npm"] = npm_latest(program["npm"]["package"])
        if program.get("winget"):
            row["winget"] = winget_latest(program)
        row["npm_ok"] = (
            None if not program.get("npm") else row["npm"] == target
        )
        row["winget_ok"] = (
            None if not program.get("winget") else row["winget"] == target
        )
        row["ok"] = all(
            value for value in (row["npm_ok"], row["winget_ok"]) if value is not None
        )
        rows.append(row)
    return rows


def print_table(rows: list[dict]) -> None:
    headers = ("program", "release", "target", "npm", "winget", "state")
    values = []
    for row in rows:
        state = "ok" if row["ok"] else "drift"
        values.append(
            (
                row["program"],
                row["release"] or "?",
                row["target"] or "?",
                row["npm"] or ("-" if row["npm_ok"] is None else "missing"),
                row["winget"] or ("-" if row["winget_ok"] is None else "missing"),
                state,
            )
        )
    widths = [
        max(len(headers[index]), *(len(line[index]) for line in values))
        for index in range(len(headers))
    ]
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for line in values:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(line)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print JSON rows")
    parser.add_argument(
        "--plan", action="store_true", help="print publish matrices as JSON"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="do not query GitHub releases; compare checksums only",
    )
    parser.add_argument(
        "--fail-on-drift",
        action="store_true",
        help="exit 1 when any configured channel is behind",
    )
    parser.add_argument(
        "--program", action="append", default=[], help="limit to a program (repeatable)"
    )
    args = parser.parse_args()

    programs = sync.load_programs()
    if args.program:
        programs = sync.select_programs(programs, args.program)
    checksums = sync.load_checksums()
    rows = collect(programs, checksums, live=not args.offline)

    if args.plan:
        npm = [
            {"program": row["program"], "version": row["target"]}
            for row in rows
            if row["npm_ok"] is False and row["target"]
        ]
        winget = [
            {"program": row["program"], "version": row["target"]}
            for row in rows
            if row["winget_ok"] is False and row["target"]
        ]
        print(json.dumps({"npm": npm, "winget": winget}, separators=(",", ":")))
        return

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        print_table(rows)

    if args.fail_on_drift and any(not row["ok"] for row in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()