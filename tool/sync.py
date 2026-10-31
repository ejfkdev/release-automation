#!/usr/bin/env python3
"""Generate WinGet manifests and npm wrapper packages from a central catalog.

The catalog is the only hand-maintained input:

    programs/<name>.toml     one program per file
    checksums.toml           generated release digests (committed)
    winget/manifests/...     generated WinGet multi-file manifests (committed)
    npm/<name>/              generated npm wrapper package (committed)

`--refresh` reads the latest GitHub release for every program (or a pinned
version), records the SHA-256 of each platform asset, and re-renders every
channel. Without `--refresh` the committed checksums are used, so `--check`
needs no network and can run on every pull request.

Usage:
    python3 tool/sync.py                      # render from checksums.toml
    python3 tool/sync.py --refresh            # fetch latest releases, render
    python3 tool/sync.py --check              # verify committed output
    python3 tool/sync.py --refresh --program ddc
    python3 tool/sync.py --refresh --program ddc --version 0.1.34
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parent.parent
PROGRAMS_DIR = ROOT / "programs"
CHECKSUMS = ROOT / "checksums.toml"
NPM_DIR = ROOT / "npm"
WINGET_DIR = ROOT / "winget"
SCHEMA = 1
WINGET_MANIFEST_VERSION = "1.12.0"

# Canonical target triples used as asset-map keys. They keep the catalog
# independent of each project's release naming convention.
CANONICAL_TARGETS = {
    "x86_64-apple-darwin",
    "aarch64-apple-darwin",
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
    "x86_64-pc-windows-msvc",
    "aarch64-pc-windows-msvc",
}

NODE_KEYS = {
    "x86_64-apple-darwin": "darwin-x64",
    "aarch64-apple-darwin": "darwin-arm64",
    "x86_64-unknown-linux-gnu": "linux-x64",
    "aarch64-unknown-linux-gnu": "linux-arm64",
    "x86_64-pc-windows-msvc": "win32-x64",
    "aarch64-pc-windows-msvc": "win32-arm64",
}

RESERVED_SPEC_KEYS = {"file", "archive", "member"}


def die(message: str) -> NoReturn:
    print(f"sync: error: {message}", file=sys.stderr)
    raise SystemExit(1)


def note(message: str) -> None:
    print(f"sync: {message}")


# --------------------------------------------------------------------------
# Catalog


def _quoted(map_name: str, key: str) -> str:
    return f"{map_name}.{json.dumps(key)}"


def load_programs() -> list[dict]:
    if not PROGRAMS_DIR.is_dir():
        die(f"missing catalog directory: {PROGRAMS_DIR}")
    programs: list[dict] = []
    seen_names: set[str] = set()
    seen_npm: set[str] = set()
    seen_winget: set[str] = set()
    for path in sorted(PROGRAMS_DIR.glob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != SCHEMA:
            die(f"{path.name}: schema {data.get('schema')!r}, expected {SCHEMA}")
        name = data.get("name")
        if not isinstance(name, str) or not name:
            die(f"{path.name}: missing name")
        if path.stem != name:
            die(f"{path.name}: file name must match program name {name!r}")
        if name in seen_names:
            die(f"{path.name}: duplicate program name {name!r}")
        seen_names.add(name)
        for key in ("repo", "summary", "license", "assets"):
            if not data.get(key):
                die(f"{path.name}: missing {key}")
        data.setdefault("bins", [name])
        if not isinstance(data["bins"], list) or not data["bins"]:
            die(f"{path.name}: bins must be a non-empty list")
        if len(set(data["bins"])) != len(data["bins"]):
            die(f"{path.name}: duplicate bins")
        for target in data["assets"]:
            if target not in CANONICAL_TARGETS:
                die(f"{path.name}: unknown target {target!r}")
        npm = data.get("npm")
        if npm:
            package = npm.get("package")
            if not isinstance(package, str) or not package.startswith("@ejfkdev/"):
                die(f"{path.name}: npm.package must be an @ejfkdev/ scoped name")
            if package in seen_npm:
                die(f"{path.name}: duplicate npm package {package}")
            seen_npm.add(package)
            bin_map = npm.get("bin_map") or {}
            for command, bin_name in bin_map.items():
                if bin_name not in data["bins"]:
                    die(f"{path.name}: npm.bin_map.{command} references unknown bin {bin_name}")
        winget = data.get("winget")
        if winget:
            identifier = winget.get("id")
            if not isinstance(identifier, str) or "." not in identifier:
                die(f"{path.name}: winget.id must contain a dot")
            if identifier in seen_winget:
                die(f"{path.name}: duplicate winget id {identifier}")
            seen_winget.add(identifier)
            for arch, target in (winget.get("arch") or {}).items():
                if arch not in ("x64", "arm64"):
                    die(f"{path.name}: winget.arch.{arch} is not x64/arm64")
                if target not in data["assets"]:
                    die(f"{path.name}: winget.arch.{arch} references missing target {target}")
        programs.append(data)
    if not programs:
        die(f"no programs found in {PROGRAMS_DIR}")
    return programs


def select_programs(programs: list[dict], wanted: list[str]) -> list[dict]:
    if not wanted:
        return programs
    known = {program["name"] for program in programs}
    unknown = set(wanted) - known
    if unknown:
        die(f"unknown program(s): {', '.join(sorted(unknown))}")
    return [program for program in programs if program["name"] in set(wanted)]


# --------------------------------------------------------------------------
# Version and asset resolution


def tag_for(program: dict, version: str) -> str:
    template = program.get("tag", "v{version}")
    try:
        return template.format(version=version)
    except KeyError as exc:
        die(f"{program['name']}: unknown tag placeholder {exc}")


def version_from_tag(program: dict, tag: str) -> str:
    match = re.match(r"^v?(\d[^/]*)$", tag)
    if not match:
        die(
            f"{program['name']}: cannot derive a version from tag {tag!r}; "
            f"set `version` explicitly in programs/{program['name']}.toml"
        )
    return match.group(1)


def detect_archive(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".zip"):
        return "zip"
    if lower.endswith(".tar.gz") or lower.endswith(".tgz"):
        return "tar.gz"
    return "raw"


def default_member(bin_name: str, target: str, archive: str) -> str | None:
    if archive == "raw":
        return None
    suffix = ".exe" if target.endswith("-pc-windows-msvc") else ""
    return f"{bin_name}{suffix}"


def normalize_spec(value: object, bin_name: str, target: str, context: str) -> dict:
    if isinstance(value, str):
        name = value
        archive = detect_archive(name)
        member = default_member(bin_name, target, archive)
    elif isinstance(value, dict) and "file" in value:
        name = value["file"]
        if not isinstance(name, str) or not name:
            die(f"{context}: file must be a non-empty string")
        archive = value.get("archive") or detect_archive(name)
        if archive not in ("raw", "zip", "tar.gz"):
            die(f"{context}: archive must be raw, zip or tar.gz")
        member = value.get("member")
        if archive != "raw" and not member:
            member = default_member(bin_name, target, archive)
        if archive == "raw":
            member = None
    else:
        die(f"{context}: expected an asset name or a table with `file`")
    if archive != "raw" and not isinstance(member, str):
        die(f"{context}: archive needs a member name")
    return {"name": name, "archive": archive, "member": member}


def asset_specs(program: dict) -> dict[str, dict[str, dict]]:
    """Return target -> bin -> {name, archive, member}."""
    bins = program["bins"]
    specs: dict[str, dict[str, dict]] = {}
    for target, value in program["assets"].items():
        if isinstance(value, str):
            specs[target] = {
                bins[0]: normalize_spec(value, bins[0], target, f"{program['name']}.{target}")
            }
            continue
        if not isinstance(value, dict):
            die(f"{program['name']}.{target}: expected string or table")
        if RESERVED_SPEC_KEYS & set(value):
            specs[target] = {
                bins[0]: normalize_spec(value, bins[0], target, f"{program['name']}.{target}")
            }
            continue
        per_bin: dict[str, dict] = {}
        for bin_name in bins:
            if bin_name not in value:
                die(f"{program['name']}.{target}: missing bin {bin_name!r}")
            per_bin[bin_name] = normalize_spec(
                value[bin_name], bin_name, target, f"{program['name']}.{target}.{bin_name}"
            )
        unknown = set(value) - set(bins)
        if unknown:
            die(f"{program['name']}.{target}: unknown bins: {', '.join(sorted(unknown))}")
        specs[target] = per_bin
    for bin_name in bins:
        for target in specs:
            if bin_name not in specs[target]:
                die(f"{program['name']}: {bin_name} has no asset for {target}")
    return specs


def format_asset(program: dict, version: str, tag: str, target: str, spec: dict) -> str:
    exe = ".exe" if target.endswith("-pc-windows-msvc") else ""
    try:
        return spec["name"].format(version=version, tag=tag, exe=exe)
    except KeyError as exc:
        die(f"{program['name']}: unknown placeholder {exc} in asset {spec['name']!r}")


def format_member(member: str, version: str, tag: str) -> str:
    try:
        return member.format(version=version, tag=tag)
    except KeyError as exc:
        die(f"unknown placeholder {exc} in archive member {member!r}")


def asset_url(program: dict, tag: str, asset_name: str) -> str:
    return f"https://github.com/{program['repo']}/releases/download/{tag}/{asset_name}"


# --------------------------------------------------------------------------
# GitHub API


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def api_json(path: str, allow_404: bool = False) -> object:
    url = f"https://api.github.com/{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ejfkdev-release-automation",
    }
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and allow_404:
            return None
        if exc.code in (403, 429):
            gh = shutil.which("gh")
            if gh:
                result = subprocess.run(
                    [gh, "api", path], capture_output=True, text=True
                )
                if result.returncode == 0:
                    return json.loads(result.stdout)
        die(f"GitHub API error {exc.code} for {url}: {exc.reason}")
    except urllib.error.URLError as exc:
        die(f"cannot reach {url}: {exc.reason}")


def fetch_release(program: dict, tag: str | None = None) -> dict:
    if tag is None:
        path = f"repos/{program['repo']}/releases/latest"
    else:
        path = f"repos/{program['repo']}/releases/tags/{urllib.parse.quote(tag)}"
    release = api_json(path)
    if not isinstance(release, dict) or not release.get("tag_name"):
        die(f"{program['name']}: unexpected release response for {path}")
    return release


def download_bytes(url: str) -> bytes:
    # Optional mirror for networks where objects.githubusercontent.com is slow.
    # The caller always verifies the SHA-256 from the GitHub API, so a mirror
    # cannot change the bytes that end up in a manifest.
    proxy = os.environ.get("GITHUB_DOWNLOAD_PROXY")
    if proxy:
        url = f"{proxy.rstrip('/')}/{url}"
    request = urllib.request.Request(
        url, headers={"User-Agent": "ejfkdev-release-automation"}
    )
    last: Exception | None = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return response.read()
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            OSError,
        ) as exc:
            last = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (404, 401, 403):
                break
            if attempt < 4:
                delay = 2 * (attempt + 1)
                note(f"download failed ({exc}); retrying in {delay}s")
                time.sleep(delay)
                request = urllib.request.Request(
                    url, headers={"User-Agent": "ejfkdev-release-automation"}
                )
    die(f"cannot download {url}: {last}")


def asset_digest(asset: dict) -> tuple[str, int]:
    digest = asset.get("digest")
    if isinstance(digest, str) and digest.startswith("sha256:"):
        sha = digest.removeprefix("sha256:").lower()
        if re.fullmatch(r"[0-9a-f]{64}", sha):
            return sha, int(asset.get("size", 0))
    data = download_bytes(asset["browser_download_url"])
    return hashlib.sha256(data).hexdigest(), len(data)


# --------------------------------------------------------------------------
# Checksums


def load_checksums() -> dict:
    if not CHECKSUMS.is_file():
        return {}
    data = tomllib.loads(CHECKSUMS.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        die(f"{CHECKSUMS} has schema {data.get('schema')!r}, expected {SCHEMA}")
    return {key: value for key, value in data.items() if key != "schema"}


def dump_checksums(records: dict) -> str:
    lines = [
        "# Generated by tool/sync.py — do not edit by hand.",
        f"schema = {SCHEMA}",
        "",
    ]
    for name in sorted(records):
        record = records[name]
        lines.append(f"[{json.dumps(name)}]")
        lines.append(f"version = {json.dumps(record['version'])}")
        lines.append(f"tag = {json.dumps(record['tag'])}")
        for target in sorted(record.get("targets", {})):
            for bin_name in sorted(record["targets"][target]):
                entry = record["targets"][target][bin_name]
                lines.append("")
                lines.append(
                    f"[{name}.targets.{json.dumps(target)}.{json.dumps(bin_name)}]"
                )
                lines.append(f"name = {json.dumps(entry['name'])}")
                lines.append(f"sha256 = {json.dumps(entry['sha256'])}")
                lines.append(f"size = {entry['size']}")
        for arch in sorted(record.get("bundles", {})):
            entry = record["bundles"][arch]
            lines.append("")
            lines.append(f"[{name}.bundles.{json.dumps(arch)}]")
            lines.append(f"name = {json.dumps(entry['name'])}")
            lines.append(f"sha256 = {json.dumps(entry['sha256'])}")
            lines.append(f"size = {entry['size']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def check_record(program: dict, version: str, record: dict | None) -> dict:
    name = program["name"]
    if record is None:
        die(f"{name}: no checksums — run `python3 tool/sync.py --refresh`")
    if record.get("version") != version:
        die(
            f"{name}: checksums are for {record.get('version')!r}, catalog resolves "
            f"{version!r} — run `python3 tool/sync.py --refresh`"
        )
    tag = record.get("tag")
    if not isinstance(tag, str) or not tag:
        die(f"{name}: checksums.toml has no tag")
    specs = asset_specs(program)
    for target in specs:
        for bin_name in specs[target]:
            entry = (record.get("targets") or {}).get(target, {}).get(bin_name)
            if not isinstance(entry, dict):
                die(f"{name}: checksums.toml is missing {target}/{bin_name}")
            if not isinstance(entry.get("name"), str):
                die(f"{name}: checksums.toml has no asset name for {target}/{bin_name}")
            sha = entry.get("sha256")
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
                die(f"{name}: invalid sha256 for {target}/{bin_name}")
            if not isinstance(entry.get("size"), int) or entry["size"] <= 0:
                die(f"{name}: invalid size for {target}/{bin_name}")
    if program.get("winget", {}).get("bundle"):
        for arch in winget_arch_map(program):
            entry = (record.get("bundles") or {}).get(arch)
            if not isinstance(entry, dict):
                die(f"{name}: checksums.toml is missing the {arch} WinGet bundle")
            if not isinstance(entry.get("name"), str):
                die(f"{name}: checksums.toml has no bundle name for {arch}")
            sha = entry.get("sha256")
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
                die(f"{name}: invalid bundle sha256 for {arch}")
    return record


# --------------------------------------------------------------------------
# Deterministic WinGet payload bundles


def bundle_name(program: dict, version: str, arch: str) -> str:
    identifier = program["winget"]["id"]
    return f"{identifier}-{version}-winget-{arch}.zip"


def build_bundle_bytes(
    program: dict, version: str, tag: str, record: dict, arch: str
) -> bytes:
    """Build a ZIP_STORED bundle of the Windows binaries for one architecture.

    ZIP_STORED (no compression) keeps the bytes independent of the local zlib
    build, so the SHA-256 recorded in CI matches the one recorded locally.
    """
    target = winget_arch_map(program)[arch]
    specs = asset_specs(program)[target]
    members: list[tuple[str, bytes]] = []
    for bin_name in program["bins"]:
        spec = specs[bin_name]
        if spec["archive"] != "raw":
            die(
                f"{program['name']}: bundle programs must publish raw Windows "
                f"binaries; {target}/{bin_name} is {spec['archive']}"
            )
        name = format_asset(program, version, tag, target, spec)
        expected = record["targets"][target][bin_name]["sha256"]
        data = download_bytes(asset_url(program, tag, name))
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            die(f"{program['name']}: sha256 mismatch for {name}")
        members.append((f"{bin_name}.exe", data))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in members:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o755 << 16
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, data)
    return buffer.getvalue()


def record_complete(program: dict, record: dict, version: str) -> bool:
    if record.get("version") != version:
        return False
    specs = asset_specs(program)
    for target, bins in specs.items():
        for bin_name in bins:
            entry = (record.get("targets") or {}).get(target, {}).get(bin_name)
            if not isinstance(entry, dict) or not entry.get("sha256"):
                return False
    if program.get("winget", {}).get("bundle"):
        for arch in winget_arch_map(program):
            entry = (record.get("bundles") or {}).get(arch)
            if not isinstance(entry, dict) or not entry.get("sha256"):
                return False
    return True


def refresh_program(
    program: dict, version: str, tag: str, release: dict | None = None
) -> dict:
    release = release or fetch_release(program, tag)
    assets = {asset["name"]: asset for asset in release.get("assets", [])}
    specs = asset_specs(program)
    record: dict = {
        "version": version,
        "tag": tag,
        "targets": {},
        "bundles": {},
    }
    missing: list[str] = []
    for target in sorted(specs):
        record["targets"][target] = {}
        for bin_name in sorted(specs[target]):
            name = format_asset(program, version, tag, target, specs[target][bin_name])
            asset = assets.get(name)
            if asset is None:
                missing.append(name)
                continue
            sha, size = asset_digest(asset)
            record["targets"][target][bin_name] = {
                "name": name,
                "sha256": sha,
                "size": size,
            }
    if missing:
        available = ", ".join(sorted(assets)) or "(none)"
        die(
            f"{program['name']}: release {tag} is missing assets: "
            f"{', '.join(missing)}\n  available: {available}"
        )
    if program.get("winget", {}).get("bundle"):
        for arch in sorted(winget_arch_map(program)):
            data = build_bundle_bytes(program, version, tag, record, arch)
            record["bundles"][arch] = {
                "name": bundle_name(program, version, arch),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
    return record


# --------------------------------------------------------------------------
# Rendering helpers


def one_line(text: str) -> str:
    return " ".join(text.split())


def yq(value: str) -> str:
    """Quote a YAML scalar only when it would not survive as a plain scalar."""
    if (
        value
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+()/,'-]*", value)
        and ": " not in value
        and not value.endswith(":")
    ):
        return value
    return json.dumps(value, ensure_ascii=False)


def yaml_list(key: str, values: list[str]) -> list[str]:
    lines = [f"{key}:"]
    for value in values:
        lines.append(f"  - {yq(value)}")
    return lines


def winget_dir(program: dict, version: str) -> Path:
    identifier = program["winget"]["id"]
    parts = identifier.split(".")
    return (
        WINGET_DIR
        / "manifests"
        / identifier[0].lower()
        / parts[0]
        / ".".join(parts[1:])
        / version
    )


def winget_arch_map(program: dict) -> dict[str, str]:
    config = program.get("winget") or {}
    arch = config.get("arch") or {}
    if not arch:
        arch = {
            "x64": "x86_64-pc-windows-msvc",
            "arm64": "aarch64-pc-windows-msvc",
        }
        arch = {
            name: target
            for name, target in arch.items()
            if target in program["assets"]
        }
    if not arch:
        die(f"{program['name']}: no Windows targets for WinGet")
    return arch


# --------------------------------------------------------------------------
# WinGet


def render_winget(program: dict, version: str, record: dict) -> dict[Path, bytes]:
    config = program.get("winget")
    if not config:
        return {}
    name = program["name"]
    identifier = config["id"]
    arch_map = winget_arch_map(program)
    specs = asset_specs(program)
    bins = program["bins"]
    bundle = bool(config.get("bundle"))
    repo = program["repo"]
    page = f"https://github.com/{repo}"
    tag = record["tag"]
    publisher = program.get("publisher", repo.split("/")[0])
    summary = one_line(program["summary"])
    description = one_line(program.get("description") or program["summary"])
    tags = config.get("tags") or program.get("keywords") or []
    license_name = program["license"]
    license_url = None
    if license_name != "Proprietary":
        license_file = program.get("license_file", "LICENSE")
        license_url = f"{page}/blob/{tag}/{license_file}"

    base = winget_dir(program, version)

    version_yaml = "\n".join(
        [
            f"# yaml-language-server: $schema=https://aka.ms/winget-manifest.version.{WINGET_MANIFEST_VERSION}.schema.json",
            f"PackageIdentifier: {yq(identifier)}",
            f"PackageVersion: {yq(version)}",
            "DefaultLocale: en-US",
            "ManifestType: version",
            f"ManifestVersion: {WINGET_MANIFEST_VERSION}",
            "",
        ]
    )

    locale_lines = [
        f"# yaml-language-server: $schema=https://aka.ms/winget-manifest.defaultLocale.{WINGET_MANIFEST_VERSION}.schema.json",
        f"PackageIdentifier: {yq(identifier)}",
        f"PackageVersion: {yq(version)}",
        "PackageLocale: en-US",
        f"Publisher: {yq(publisher)}",
        f"PublisherUrl: {yq(f'https://github.com/{publisher}')}",
        f"PublisherSupportUrl: {yq(f'{page}/issues')}",
        f"PackageName: {yq(program.get('display_name', name))}",
        f"PackageUrl: {yq(page)}",
        f"License: {yq(license_name)}",
    ]
    if license_url:
        locale_lines.append(f"LicenseUrl: {yq(license_url)}")
    locale_lines += [
        f"ShortDescription: {yq(summary)}",
        "Description: |-",
        f"  {description}",
    ]
    locale_lines += yaml_list("Tags", tags)
    locale_lines += [
        f"ReleaseNotesUrl: {yq(f'{page}/releases/tag/{tag}')}",
        "ManifestType: defaultLocale",
        f"ManifestVersion: {WINGET_MANIFEST_VERSION}",
        "",
    ]
    locale_yaml = "\n".join(locale_lines)

    installer_entries: list[list[str]] = []
    installer_types: set[str] = set()
    for arch in sorted(arch_map):
        target = arch_map[arch]
        if bundle:
            entry = (record.get("bundles") or {}).get(arch)
            if not isinstance(entry, dict):
                die(f"{name}: no {arch} bundle in checksums")
            lines = [
                f"  - Architecture: {arch}",
                f"    InstallerUrl: {yq(asset_url(program, tag, entry['name']))}",
                f"    InstallerSha256: {entry['sha256'].upper()}",
                "    NestedInstallerFiles:",
            ]
            for bin_name in bins:
                lines.append(f"      - RelativeFilePath: {yq(bin_name + '.exe')}")
                lines.append(f"        PortableCommandAlias: {yq(bin_name)}")
            installer_types.add("zip")
            installer_entries.append(lines)
            continue

        spec = specs[target][bins[0]]
        entry_sha = record["targets"][target][bins[0]]["sha256"]
        entry_name = format_asset(program, version, tag, target, spec)
        lines = [
            f"  - Architecture: {arch}",
            f"    InstallerUrl: {yq(asset_url(program, tag, entry_name))}",
            f"    InstallerSha256: {entry_sha.upper()}",
        ]
        if spec["archive"] == "raw":
            installer_types.add("portable")
        elif spec["archive"] == "zip":
            if not spec["member"]:
                die(f"{name}: zip asset for {target} has no member")
            lines += [
                "    NestedInstallerFiles:",
                f"      - RelativeFilePath: {yq(format_member(spec['member'], version, tag))}",
                f"        PortableCommandAlias: {yq(bins[0])}",
            ]
            installer_types.add("zip")
        else:
            die(
                f"{name}: WinGet cannot install {spec['archive']} directly for "
                f"{target}; publish a zip or raw binary"
            )
        installer_entries.append(lines)

    if len(installer_types) != 1:
        die(f"{name}: mixed WinGet installer types: {sorted(installer_types)}")
    installer_type = installer_types.pop()

    installer_lines = [
        f"# yaml-language-server: $schema=https://aka.ms/winget-manifest.installer.{WINGET_MANIFEST_VERSION}.schema.json",
        f"PackageIdentifier: {yq(identifier)}",
        f"PackageVersion: {yq(version)}",
        f"InstallerType: {installer_type}",
    ]
    if installer_type == "zip":
        installer_lines.append("NestedInstallerType: portable")
    else:
        installer_lines.append("Commands:")
        for bin_name in bins:
            installer_lines.append(f"  - {yq(bin_name)}")
    installer_lines += ["UpgradeBehavior: install", "Installers:"]
    for entry in installer_entries:
        installer_lines += entry
    installer_lines += [
        "ManifestType: installer",
        f"ManifestVersion: {WINGET_MANIFEST_VERSION}",
        "",
    ]
    installer_yaml = "\n".join(installer_lines)

    return {
        base / f"{identifier}.yaml": version_yaml.encode(),
        base / f"{identifier}.locale.en-US.yaml": locale_yaml.encode(),
        base / f"{identifier}.installer.yaml": installer_yaml.encode(),
    }


# --------------------------------------------------------------------------
# npm


NPM_INSTALL_JS = r'''#!/usr/bin/env node
"use strict";

// Generated by tool/sync.py. Downloads the matching GitHub release binary
// (raw or archived), verifies its SHA-256, and installs it next to this file.
const fs = require("node:fs");
const path = require("node:path");
const https = require("node:https");
const zlib = require("node:zlib");
const crypto = require("node:crypto");
const { version } = require("./package.json");

const ASSETS = __ASSETS__;
const PROXY = process.env.EJFKDEV_DOWNLOAD_PROXY || "";

function requestUrl(url) {
  if (!PROXY) return url;
  return `${PROXY.replace(/\/+$/, "")}/${url}`;
}

function fail(message) {
  console.error(`${message}`);
  process.exit(1);
}

function isMusl() {
  if (process.platform !== "linux") return false;
  try {
    return !process.report.getReport().header.glibcVersionRuntime;
  } catch (_) {
    return (
      fs.existsSync("/lib/ld-musl-x86_64.so.1") ||
      fs.existsSync("/lib/ld-musl-aarch64.so.1")
    );
  }
}

function download(url, dest, redirects) {
  if (redirects === undefined) redirects = 0;
  return new Promise((resolve, reject) => {
    const request = https.get(
      requestUrl(url),
      {
        headers: { "User-Agent": `ejfkdev-npm-installer/${version}` },
        timeout: 120000,
      },
      (response) => {
        const status = response.statusCode || 0;
        if (status >= 300 && status < 400 && response.headers.location) {
          response.resume();
          if (redirects >= 10) {
            reject(new Error(`too many redirects for ${url}`));
            return;
          }
          const next = new URL(response.headers.location, url).toString();
          download(next, dest, redirects + 1).then(resolve, reject);
          return;
        }
        if (status !== 200) {
          response.resume();
          reject(new Error(`HTTP ${status} for ${url}`));
          return;
        }
        const hash = crypto.createHash("sha256");
        const out = fs.createWriteStream(dest, { mode: 0o755 });
        response.on("data", (chunk) => hash.update(chunk));
        response.on("error", reject);
        out.on("error", reject);
        out.on("finish", () => resolve(hash.digest("hex")));
        response.pipe(out);
      }
    );
    request.on("error", reject);
    request.on("timeout", () => {
      request.destroy(new Error(`download timed out for ${url}`));
    });
  });
}

async function downloadWithRetry(url, dest) {
  let last;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      return await download(url, dest, 0);
    } catch (error) {
      last = error;
      fs.rmSync(dest, { force: true });
      if (attempt < 3) {
        console.error(`download failed (${error.message}); retrying (${attempt}/3)...`);
        await new Promise((resolve) => setTimeout(resolve, attempt * 2000));
      }
    }
  }
  throw last;
}

// Minimal tar.gz reader: enough for release archives with regular files.
function extractTarGz(buffer, member) {
  const tar = zlib.gunzipSync(buffer);
  let offset = 0;
  while (offset + 512 <= tar.length) {
    const header = tar.subarray(offset, offset + 512);
    let empty = true;
    for (const byte of header) {
      if (byte !== 0) {
        empty = false;
        break;
      }
    }
    if (empty) break;
    let name = header.subarray(0, 100).toString("utf8").replace(/\0.*$/, "");
    const prefix = header.subarray(345, 500).toString("utf8").replace(/\0.*$/, "");
    if (prefix) name = `${prefix}/${name}`;
    const sizeText = header.subarray(124, 136).toString("ascii").replace(/\0.*$/, "").trim();
    const size = parseInt(sizeText, 8) || 0;
    const type = String.fromCharCode(header[156] || 48);
    if ((type === "0" || type === "\0" || type === "7") &&
        path.posix.basename(name) === member) {
      const start = offset + 512;
      return tar.subarray(start, start + size);
    }
    offset += 512 + Math.ceil(size / 512) * 512;
  }
  throw new Error(`archive does not contain ${member}`);
}

// Minimal zip reader: central-directory walk, stored and deflated entries.
function extractZip(buffer, member) {
  let eocd = -1;
  const stop = Math.max(0, buffer.length - 65557);
  for (let i = buffer.length - 22; i >= stop; i -= 1) {
    if (buffer.readUInt32LE(i) === 0x06054b50) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) throw new Error("not a zip archive (no end record)");
  const count = buffer.readUInt16LE(eocd + 10);
  let offset = buffer.readUInt32LE(eocd + 16);
  for (let i = 0; i < count; i += 1) {
    if (buffer.readUInt32LE(offset) !== 0x02014b50) {
      throw new Error("corrupt zip central directory");
    }
    const method = buffer.readUInt16LE(offset + 10);
    const compressedSize = buffer.readUInt32LE(offset + 20);
    const nameLength = buffer.readUInt16LE(offset + 28);
    const extraLength = buffer.readUInt16LE(offset + 30);
    const commentLength = buffer.readUInt16LE(offset + 32);
    const localOffset = buffer.readUInt32LE(offset + 42);
    const name = buffer.subarray(offset + 46, offset + 46 + nameLength).toString("utf8");
    if (path.posix.basename(name) === member) {
      if (buffer.readUInt32LE(localOffset) !== 0x04034b50) {
        throw new Error("corrupt zip local header");
      }
      const localNameLength = buffer.readUInt16LE(localOffset + 26);
      const localExtraLength = buffer.readUInt16LE(localOffset + 28);
      const start = localOffset + 30 + localNameLength + localExtraLength;
      const data = buffer.subarray(start, start + compressedSize);
      if (method === 0) return data;
      if (method === 8) return zlib.inflateRawSync(data);
      throw new Error(`unsupported zip compression method ${method}`);
    }
    offset += 46 + nameLength + extraLength + commentLength;
  }
  throw new Error(`archive does not contain ${member}`);
}

async function installBin(name, meta, binDir) {
  const ext = process.platform === "win32" ? ".exe" : "";
  const dest = path.join(binDir, `${name}${ext}`);
  const tmp = path.join(binDir, `.${name}-download-${process.pid}`);
  fs.mkdirSync(binDir, { recursive: true });

  const local = process.env[`${meta.env_prefix}_BINARY_PATH`];
  if (local) {
    fs.copyFileSync(local, dest);
    if (process.platform !== "win32") fs.chmodSync(dest, 0o755);
    console.log(`${name}: installed from ${local}`);
    return;
  }

  try {
    const actual = await downloadWithRetry(meta.url, tmp);
    if (actual !== meta.sha256) {
      throw new Error(
        `sha256 mismatch for ${meta.url}: expected ${meta.sha256}, got ${actual}`
      );
    }
    if (meta.archive === "raw") {
      fs.rmSync(dest, { force: true });
      fs.renameSync(tmp, dest);
    } else {
      const buffer = fs.readFileSync(tmp);
      const extracted =
        meta.archive === "zip"
          ? extractZip(buffer, meta.member)
          : extractTarGz(buffer, meta.member);
      fs.rmSync(dest, { force: true });
      fs.writeFileSync(dest, extracted, { mode: 0o755 });
    }
    if (process.platform !== "win32") fs.chmodSync(dest, 0o755);
    console.log(`${name} ${version} installed`);
  } catch (error) {
    fs.rmSync(tmp, { force: true });
    fail(
      `${name}: ${error.message}\n  Download it manually from ${meta.url} and set ` +
        `${meta.env_prefix}_BINARY_PATH.`
    );
  }
}

async function main() {
  const key = `${process.platform}-${process.arch}`;
  const entry = ASSETS[key];
  if (!entry) {
    fail(
      `no prebuilt binary for ${key}; supported: ${Object.keys(ASSETS).join(", ")}.`
    );
  }
  if (isMusl()) {
    fail(
      `this package ships glibc builds and Alpine/musl is not supported. ` +
        `Install from source: __INSTALL_HINT__`
    );
  }
  for (const [name, meta] of Object.entries(entry.bins)) {
    await installBin(name, meta, path.join(__dirname, "bin"));
  }
}

main().catch((error) => fail(error && error.stack ? error.stack : String(error)));
'''


NPM_LAUNCHER_JS = r'''#!/usr/bin/env node
"use strict";

// Generated by tool/sync.py. Keeps the npm shim portable while the real
// binary stays a native executable.
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { spawn, spawnSync } = require("node:child_process");

const NAME = "__NAME__";
const ENV_PREFIX = "__ENV_PREFIX__";
const exe = process.platform === "win32" ? `${NAME}.exe` : NAME;
let bin = process.env[`${ENV_PREFIX}_BINARY_PATH`] || path.join(__dirname, exe);

if (!process.env[`${ENV_PREFIX}_BINARY_PATH`] && !fs.existsSync(bin)) {
  console.error(`${NAME}: binary not installed; fetching it now...`);
  const installer = spawnSync(
    process.execPath,
    [path.join(__dirname, "..", "install.js")],
    { stdio: "inherit" }
  );
  if (installer.status !== 0 || !fs.existsSync(bin)) {
    console.error(
      `${NAME}: automatic install failed. Reinstall without --ignore-scripts, ` +
        `or set ${ENV_PREFIX}_BINARY_PATH to a ${NAME} binary.`
    );
    process.exit(127);
  }
}

if (!fs.existsSync(bin)) {
  console.error(
    `${NAME}: binary not found at ${bin}. ` +
      `Check ${ENV_PREFIX}_BINARY_PATH or reinstall the package.`
  );
  process.exit(127);
}

const child = spawn(bin, process.argv.slice(2), { stdio: "inherit" });

child.on("error", (error) => {
  console.error(`${NAME}: failed to start ${bin}: ${error.message}`);
  process.exit(127);
});

child.on("exit", (code, signal) => {
  if (signal) {
    const number = os.constants.signals[signal] || 0;
    process.exit(128 + number);
  }
  process.exit(code === null ? 1 : code);
});
'''


def env_prefix(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def render_npm(program: dict, version: str, record: dict) -> dict[Path, bytes]:
    config = program.get("npm")
    if not config:
        return {}
    name = program["name"]
    package_name = config["package"]
    tag = record["tag"]
    bins = list(program["bins"])
    bin_map = config.get("bin_map") or {command: command for command in bins}
    if set(bin_map.values()) - set(bins):
        die(f"{name}: npm.bin_map references an unknown bin")
    specs = asset_specs(program)

    assets: dict[str, dict] = {}
    for target, bin_specs in specs.items():
        key = NODE_KEYS.get(target)
        if key is None:
            continue
        entry = assets.setdefault(key, {"bins": {}})
        for bin_name in bins:
            spec = bin_specs[bin_name]
            entry["bins"][bin_name] = {
                "url": asset_url(
                    program,
                    tag,
                    format_asset(program, version, tag, target, spec),
                ),
                "sha256": record["targets"][target][bin_name]["sha256"],
                "archive": spec["archive"],
                "member": (
                    format_member(spec["member"], version, tag)
                    if spec["member"]
                    else None
                ),
                "env_prefix": env_prefix(bin_name),
            }
    if not assets:
        die(f"{name}: no npm-compatible targets")
    for key, entry in assets.items():
        missing = set(bins) - set(entry["bins"])
        if missing:
            die(f"{name}: npm target {key} is missing {', '.join(sorted(missing))}")

    assets_js = "{\n" + "".join(
        f"  {json.dumps(key)}: {{\n"
        f"    bins: {{\n"
        + "".join(
            f"      {json.dumps(bin_name)}: {{\n"
            f"        url: {json.dumps(meta['url'])},\n"
            f"        sha256: {json.dumps(meta['sha256'])},\n"
            f"        archive: {json.dumps(meta['archive'])},\n"
            f"        member: {json.dumps(meta['member'])},\n"
            f"        env_prefix: {json.dumps(meta['env_prefix'])},\n"
            f"      }},\n"
            for bin_name, meta in sorted(entry["bins"].items())
        )
        + "    },\n"
        f"  }},\n"
        for key, entry in sorted(assets.items())
    ) + "}"

    install_hint = program.get(
        "install_hint", f"https://github.com/{program['repo']}"
    )
    install_js = NPM_INSTALL_JS.replace("__ASSETS__", assets_js).replace(
        "__INSTALL_HINT__", install_hint
    )
    launchers = {
        command: NPM_LAUNCHER_JS.replace("__NAME__", bin_name).replace(
            "__ENV_PREFIX__", env_prefix(bin_name)
        )
        for command, bin_name in bin_map.items()
    }

    package = {
        "name": package_name,
        "version": version,
        "description": program["summary"],
        "license": "UNLICENSED" if program["license"] == "Proprietary" else program["license"],
        "author": program.get("publisher", "ejfkdev"),
        "homepage": program.get("homepage", f"https://github.com/{program['repo']}"),
        "repository": {
            "type": "git",
            "url": f"git+https://github.com/{program['repo']}.git",
        },
        "bugs": {"url": f"https://github.com/{program['repo']}/issues"},
        "bin": {
            command: f"bin/{command}.js" for command in bin_map
        },
        "files": [f"bin/{command}.js" for command in bin_map] + ["install.js"],
        "scripts": {"postinstall": "node install.js"},
        "engines": {"node": ">=18"},
        "os": sorted({key.split("-")[0] for key in assets}),
        "cpu": sorted({key.split("-")[1] for key in assets}),
        "publishConfig": {"access": "public"},
        "keywords": program.get("keywords", []),
    }
    package_json = json.dumps(package, indent=2, ensure_ascii=False) + "\n"

    commands = "\n".join(
        f"- `{command}`" for command in bin_map
    )
    readme = f"""# {package_name}

Prebuilt [{name}]({package['homepage']}) binaries. The postinstall script
downloads the matching release asset and verifies its SHA-256; tar.gz and zip
assets are unpacked locally. If install scripts are disabled, a launcher
fetches the binary on first use instead.

```bash
npm install -g {package_name}
{next(iter(bin_map))} --help
```

Commands:
{commands}

Supported platforms: {", ".join(sorted(assets))}. Linux builds target glibc;
Alpine/musl users should install from source instead.

Each command accepts `<COMMAND>_BINARY_PATH` (for example
`{env_prefix(next(iter(bin_map.values())))}_BINARY_PATH`) to use a local
binary and skip the download.
"""

    gitignore_lines = []
    for bin_name in bins:
        gitignore_lines.append(f"/bin/{bin_name}")
        gitignore_lines.append(f"/bin/{bin_name}.exe")
        gitignore_lines.append(f"/bin/.{bin_name}-download-*")
    gitignore_lines += ["*.tgz", "node_modules/"]
    gitignore = "\n".join(gitignore_lines) + "\n"

    base = NPM_DIR / package_name.split("/")[-1]
    files: dict[Path, bytes] = {
        base / "package.json": package_json.encode(),
        base / "install.js": install_js.encode(),
        base / "README.md": readme.encode(),
        base / ".gitignore": gitignore.encode(),
    }
    for command, text in launchers.items():
        files[base / "bin" / f"{command}.js"] = text.encode()
    return files


# --------------------------------------------------------------------------
# Output validation


def render_all(
    programs: list[dict], versions: dict[str, str], checksums: dict
) -> tuple[dict[Path, bytes], dict[str, Path]]:
    files: dict[Path, bytes] = {}
    winget_roots: dict[str, Path] = {}
    for program in programs:
        name = program["name"]
        version = versions[name]
        record = check_record(program, version, checksums.get(name))
        for render in (render_winget, render_npm):
            rendered = render(program, version, record)
            for path, content in rendered.items():
                if path in files:
                    die(f"two programs render the same path: {path}")
                files[path] = content
        if program.get("winget"):
            winget_roots[name] = winget_dir(program, version).parent
    return files, winget_roots


def stale_winget_versions(program: dict, version: str) -> list[Path]:
    root = winget_dir(program, version).parent
    if not root.is_dir():
        return []
    return sorted(
        child for child in root.iterdir() if child.is_dir() and child.name != version
    )


def validate_outputs(files: dict[Path, bytes]) -> None:
    for path, content in files.items():
        text = content.decode("utf-8")
        leftover = re.search(r"__[A-Z][A-Z0-9_]*__", text)
        if leftover:
            die(f"{path}: unreplaced template placeholder {leftover.group(0)}")

    with tempfile.TemporaryDirectory(prefix="release-automation-") as tmp:
        tmpdir = Path(tmp)
        materialized: dict[Path, Path] = {}
        for index, (path, content) in enumerate(sorted(files.items())):
            suffix = path.suffix or ""
            target = tmpdir / f"{index:03d}-{path.name}"
            if suffix and not target.name.endswith(suffix):
                target = tmpdir / f"{index:03d}-{path.stem}{suffix}"
            target.write_bytes(content)
            materialized[path] = target

        yaml_files = sorted(
            path for path in files if path.suffix in (".yaml", ".yml")
        )
        if yaml_files and shutil.which("ruby"):
            result = subprocess.run(
                [
                    "ruby",
                    "-ryaml",
                    "-e",
                    "ARGV.each { |f| YAML.load_file(f) }",
                    *[str(materialized[path]) for path in yaml_files],
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                die(f"WinGet YAML validation failed: {result.stderr.strip()}")

        for path in sorted(files):
            if path.name == "package.json":
                try:
                    json.loads(files[path])
                except json.JSONDecodeError as exc:
                    die(f"{path}: invalid JSON: {exc}")

        js_files = sorted(path for path in files if path.suffix == ".js")
        if js_files and shutil.which("node"):
            for path in js_files:
                result = subprocess.run(
                    ["node", "--check", str(materialized[path])],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    die(f"{path}: JavaScript syntax error: {result.stderr.strip()}")


def compare_outputs(
    files: dict[Path, bytes], programs: list[dict], versions: dict[str, str]
) -> list[str]:
    problems: list[str] = []
    for path in sorted(files):
        if not path.is_file():
            problems.append(f"missing: {path.relative_to(ROOT)}")
            continue
        if path.read_bytes() != files[path]:
            problems.append(f"out of date: {path.relative_to(ROOT)}")
    for program in programs:
        if not program.get("npm"):
            continue
        base = NPM_DIR / program["npm"]["package"].split("/")[-1]
        expected = {path for path in files if base in path.parents}
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                if path.is_file() and path not in expected:
                    problems.append(f"unexpected: {path.relative_to(ROOT)}")
        if program.get("winget"):
            version = versions[program["name"]]
            for stale in stale_winget_versions(program, version):
                problems.append(f"stale WinGet version: {stale.relative_to(ROOT)}")
    return problems


# --------------------------------------------------------------------------
# Main


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without writing")
    parser.add_argument(
        "--refresh", action="store_true", help="fetch releases and update checksums"
    )
    parser.add_argument(
        "--program", action="append", default=[], help="limit to a program (repeatable)"
    )
    parser.add_argument(
        "--version", help="version override for a single selected program"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-fetch assets even when the recorded version is complete",
    )
    args = parser.parse_args()

    if args.version and not args.program:
        die("--version requires --program")

    catalog = load_programs()
    selected = select_programs(catalog, args.program)
    if args.version and len(selected) != 1:
        die("--version requires exactly one --program")

    checksums = load_checksums()
    versions: dict[str, str] = {}

    if args.refresh:
        updated: dict[str, dict] = {}
        for program in selected:
            name = program["name"]
            if args.version:
                version = args.version
                tag = tag_for(program, version)
                release = fetch_release(program, tag)
            elif program.get("version"):
                version = program["version"]
                tag = tag_for(program, version)
                release = fetch_release(program, tag)
            else:
                release = fetch_release(program)
                tag = release["tag_name"]
                version = version_from_tag(program, tag)
            existing = checksums.get(name)
            if (
                not args.force
                and existing
                and existing.get("tag") == tag
                and record_complete(program, existing, version)
            ):
                note(f"{name} {tag} is already recorded")
                versions[name] = version
                continue
            note(f"fetching {name} {tag}")
            updated[name] = refresh_program(program, version, tag, release)
            versions[name] = version
        checksums.update(updated)
        payload = dump_checksums(checksums).encode()
        if not CHECKSUMS.is_file() or CHECKSUMS.read_bytes() != payload:
            CHECKSUMS.write_bytes(payload)
            note(f"wrote {CHECKSUMS.relative_to(ROOT)}")
        else:
            note(f"{CHECKSUMS.relative_to(ROOT)} is unchanged")
    else:
        for program in selected:
            name = program["name"]
            record = checksums.get(name)
            if record is None:
                die(f"{name}: no checksums — run `python3 tool/sync.py --refresh`")
            if program.get("version"):
                version = program["version"]
                if record.get("version") != version:
                    die(
                        f"{name}: catalog pins {version}, checksums say "
                        f"{record.get('version')} — run `--refresh`"
                    )
            else:
                version = record["version"]
            versions[name] = version

    files, _ = render_all(selected, versions, checksums)
    validate_outputs(files)

    if args.check:
        problems = compare_outputs(files, selected, versions)
        if problems:
            for problem in problems:
                print(f"sync: {problem}", file=sys.stderr)
            die("generated manifests do not match the catalog")
        note(f"all {len(files)} files match ({', '.join(versions.values())})")
        return

    changed = 0
    for path, content in sorted(files.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.read_bytes() == content:
            continue
        path.write_bytes(content)
        changed += 1

    # Remove files that used to be generated but are no longer in the catalog
    # (for example a dropped command launcher).
    for program in selected:
        if not program.get("npm"):
            continue
        base = NPM_DIR / program["npm"]["package"].split("/")[-1]
        expected = {path for path in files if base in path.parents}
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path not in expected:
                path.unlink()
                note(f"removed stale {path.relative_to(ROOT)}")
    for program in selected:
        if program.get("winget"):
            version = versions[program["name"]]
            for stale in stale_winget_versions(program, version):
                shutil.rmtree(stale)
                note(f"removed stale {stale.relative_to(ROOT)}")
    note(
        f"{'wrote' if changed else 'unchanged'}: {changed} file(s), "
        f"{len(files)} generated ({', '.join(versions.values())})"
    )


if __name__ == "__main__":
    main()