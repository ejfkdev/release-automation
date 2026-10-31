#!/usr/bin/env python3
"""Publish generated npm packages and WinGet manifests.

    python3 tool/publish.py npm    --program ddc [--yes]
    python3 tool/publish.py winget --program ddc [--yes]
    python3 tool/publish.py bundle --program zaip [--out dist]

Without `--yes` npm and WinGet are dry runs: the exact command and payload are
printed, nothing is sent. WinGet submission runs `wingetcreate submit`, which
creates the fork branch and the pull request against microsoft/winget-pkgs.
WinGet payload bundles (multi-command packages) are built deterministically and
uploaded to the program's GitHub release before the PR is opened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL))
import status  # noqa: E402
import sync  # noqa: E402


def note(message: str) -> None:
    print(f"publish: {message}")


def die(message: str) -> "NoReturn":
    print(f"publish: error: {message}", file=sys.stderr)
    raise SystemExit(1)


def selected_programs(wanted: list[str], all_programs: bool) -> list[dict]:
    programs = sync.load_programs()
    if all_programs:
        return programs
    if not wanted:
        die("pass --program NAME (repeatable) or --all")
    known = {program["name"] for program in programs}
    unknown = set(wanted) - known
    if unknown:
        die(f"unknown program(s): {', '.join(sorted(unknown))}")
    return [program for program in programs if program["name"] in set(wanted)]


def record_for(program: dict, checksums: dict, version: str | None) -> dict:
    name = program["name"]
    record = checksums.get(name)
    if record is None:
        die(f"{name}: no checksums — run `python3 tool/sync.py --refresh`")
    if version and record.get("version") != version:
        die(
            f"{name}: checksums are for {record.get('version')}, asked for {version} "
            f"— run `python3 tool/sync.py --refresh --program {name} --version {version}`"
        )
    return record


def npm_publish(program: dict, version: str, yes: bool) -> None:
    name = program["name"]
    config = program.get("npm")
    if not config:
        note(f"{name}: no npm channel, skipping")
        return
    package = config["package"]
    package_dir = ROOT / "npm" / package.split("/")[-1]
    if not package_dir.is_dir():
        die(f"{name}: missing {package_dir.relative_to(ROOT)} — run sync.py")

    current = status.npm_latest(package)
    if current == version:
        note(f"{name}: npm already has {package}@{version}, skipping")
        return

    note(f"{name}: npm {current or 'missing'} -> {version}")
    if not yes:
        result = subprocess.run(
            ["npm", "pack", "--dry-run"],
            cwd=package_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            die(f"{name}: npm pack --dry-run failed: {result.stderr.strip()}")
        print(result.stdout)
        note(
            f"dry run — would run `npm publish --access public "
            f"--registry=https://registry.npmjs.org/` in {package_dir.relative_to(ROOT)}"
        )
        return

    env = os.environ.copy()
    token = env.get("NPM_TOKEN") or env.get("NODE_AUTH_TOKEN")
    temporary = None
    if token and not env.get("NODE_AUTH_TOKEN"):
        handle = tempfile.NamedTemporaryFile(
            "w", prefix="npmrc-", delete=False, encoding="utf-8"
        )
        handle.write("//registry.npmjs.org/:_authToken=${NPM_TOKEN}\n")
        handle.close()
        temporary = handle.name
        env["NPM_TOKEN"] = token
        env["NPM_CONFIG_USERCONFIG"] = temporary
    try:
        if not token:
            check = subprocess.run(
                ["npm", "whoami", "--registry=https://registry.npmjs.org/"],
                capture_output=True,
                text=True,
            )
            if check.returncode != 0:
                die(
                    f"{name}: not logged in to npmjs. Set NPM_TOKEN or run "
                    f"`npm login --registry=https://registry.npmjs.org/`"
                )
        result = subprocess.run(
            [
                "npm",
                "publish",
                "--access",
                "public",
                "--registry=https://registry.npmjs.org/",
            ],
            cwd=package_dir,
            env=env,
        )
        if result.returncode != 0:
            die(f"{name}: npm publish failed")
    finally:
        if temporary:
            os.unlink(temporary)
    note(f"{name}: published {package}@{version}")


def remote_asset(program: dict, tag: str, name: str) -> dict | None:
    release = sync.api_json(
        f"repos/{program['repo']}/releases/tags/{tag}", allow_404=True
    )
    if not isinstance(release, dict):
        die(f"{program['name']}: release {tag} not found")
    for asset in release.get("assets", []):
        if asset.get("name") == name:
            return asset
    return None


def ensure_bundle(program: dict, version: str, record: dict, arch: str) -> None:
    name = program["name"]
    if not program.get("winget", {}).get("bundle"):
        return
    entry = record["bundles"][arch]
    tag = record["tag"]
    data = sync.build_bundle_bytes(program, version, tag, record, arch)
    digest = hashlib.sha256(data).hexdigest()
    if digest != entry["sha256"]:
        die(f"{name}: locally rebuilt {arch} bundle does not match checksums")
    existing = remote_asset(program, tag, entry["name"])
    if existing is not None:
        remote_digest = existing.get("digest")
        if remote_digest == f"sha256:{digest}" or existing.get("size") == len(data):
            note(f"{name}: {arch} bundle already uploaded")
            return
        note(f"{name}: {arch} bundle exists with a different digest; replacing")

    token = os.environ.get("WINGET_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        die(f"{name}: WINGET_TOKEN (or GH_TOKEN) is required to upload the bundle")
    with tempfile.TemporaryDirectory(prefix="winget-bundle-") as tmp:
        path = Path(tmp) / entry["name"]
        path.write_bytes(data)
        env = os.environ.copy()
        env["GH_TOKEN"] = token
        result = subprocess.run(
            [
                "gh",
                "release",
                "upload",
                tag,
                str(path),
                "--repo",
                program["repo"],
                "--clobber",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            die(
                f"{name}: failed to upload {entry['name']}: "
                f"{(result.stderr or result.stdout).strip()}"
            )
    note(f"{name}: uploaded {entry['name']}")


def winget_publish(program: dict, version: str, yes: bool) -> None:
    name = program["name"]
    config = program.get("winget")
    if not config:
        note(f"{name}: no winget channel, skipping")
        return
    identifier = config["id"]
    current = status.winget_latest(program)
    if current == version:
        note(f"{name}: WinGet already has {identifier} {version}, skipping")
        return
    note(f"{name}: WinGet {current or 'missing'} -> {version}")

    manifest_dir = sync.winget_dir(program, version)
    if not manifest_dir.is_dir():
        die(f"{name}: missing {manifest_dir.relative_to(ROOT)} — run sync.py")

    if not yes:
        bundles = "upload payload bundles and " if config.get("bundle") else ""
        note(
            f"dry run — would {bundles}run "
            f"`wingetcreate submit {manifest_dir.relative_to(ROOT)} --token <PAT>`"
        )
        return

    checksums = sync.load_checksums()
    record = record_for(program, checksums, version)
    for arch in sync.winget_arch_map(program):
        ensure_bundle(program, version, record, arch)

    token = os.environ.get("WINGET_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        die(f"{name}: WINGET_TOKEN (a PAT with public_repo) is required")
    tool = os.environ.get("WINGETCREATE") or shutil.which("wingetcreate")
    if tool is None:
        die(
            f"{name}: wingetcreate is not available. Set WINGETCREATE to the "
            f"standalone exe, or install it with `winget install wingetcreate`. "
            f"Manifest: {manifest_dir.relative_to(ROOT)}"
        )
    title = (
        f"New package: {identifier} version {version}"
        if current is None
        else f"Update {identifier} to {version}"
    )
    result = subprocess.run(
        [
            tool,
            "submit",
            "--prtitle",
            title,
            "--token",
            token,
            "--no-open",
            str(manifest_dir),
        ],
        capture_output=True,
        text=True,
    )
    output = (result.stdout or "") + (result.stderr or "")
    if output.strip():
        print(output.strip())
    if result.returncode != 0:
        die(f"{name}: wingetcreate submit failed")
    note(f"{name}: submitted {identifier} {version}")


def bundle_only(program: dict, version: str, out: Path) -> None:
    name = program["name"]
    if not program.get("winget", {}).get("bundle"):
        note(f"{name}: no WinGet payload bundle configured")
        return
    checksums = sync.load_checksums()
    record = record_for(program, checksums, version)
    out.mkdir(parents=True, exist_ok=True)
    for arch in sorted(record.get("bundles", {})):
        entry = record["bundles"][arch]
        data = sync.build_bundle_bytes(program, version, record["tag"], record, arch)
        digest = hashlib.sha256(data).hexdigest()
        if digest != entry["sha256"]:
            die(f"{name}: rebuilt {arch} bundle does not match checksums")
        path = out / entry["name"]
        path.write_bytes(data)
        note(f"{name}: wrote {path} ({len(data)} bytes, sha256 {digest})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel", choices=("npm", "winget", "bundle"))
    parser.add_argument("--program", action="append", default=[])
    parser.add_argument("--all", action="store_true", help="all catalog programs")
    parser.add_argument("--version", help="require this generated version")
    parser.add_argument("--yes", action="store_true", help="actually publish")
    parser.add_argument("--out", default="dist", help="bundle output directory")
    args = parser.parse_args()

    programs = selected_programs(args.program, args.all)
    if args.version and len(programs) != 1:
        die("--version requires exactly one --program")

    checksums = sync.load_checksums()
    for program in programs:
        name = program["name"]
        if not (
            (args.channel == "npm" and program.get("npm"))
            or (args.channel == "winget" and program.get("winget"))
            or args.channel == "bundle"
        ):
            note(f"{name}: no {args.channel} configuration, skipping")
            continue
        record = record_for(program, checksums, args.version)
        version = record["version"]
        if args.channel == "npm":
            npm_publish(program, version, args.yes)
        elif args.channel == "winget":
            winget_publish(program, version, args.yes)
        else:
            bundle_only(program, version, Path(args.out).resolve())


if __name__ == "__main__":
    main()