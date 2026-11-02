#!/usr/bin/env python3
"""Submit generated WinGet manifests as a PR to microsoft/winget-pkgs.

This mirrors `wingetcreate submit` using the GitHub REST API through the `gh`
CLI, so it works on macOS/Linux and in CI with `GH_TOKEN`. It does not need a
Windows runner or the wingetcreate binary.

Flow:
    1. sync the fork's master with upstream
    2. create a branch from upstream master
    3. write manifests under manifests/<first>/<publisher>/<package>/<version>/
    4. create the commit and open the PR with the upstream template

WinGet payload bundles for multi-command packages are uploaded to the program's
release before the PR is opened.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL))
import status  # noqa: E402
import sync  # noqa: E402

UPSTREAM = "microsoft/winget-pkgs"
UPSTREAM_BRANCH = "master"


def note(message: str) -> None:
    print(f"submit-winget: {message}")


def die(message: str) -> "NoReturn":
    print(f"submit-winget: error: {message}", file=sys.stderr)
    raise SystemExit(1)


def gh_api(path: str, method: str = "GET", body: dict | None = None) -> object:
    command = ["gh", "api", path, "--method", method]
    if body is not None:
        command += ["--input", "-"]
    result = subprocess.run(
        command,
        input=json.dumps(body) if body is not None else None,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        die(f"gh api {method} {path} failed: {detail}")
    if method == "DELETE" or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        die(f"gh api {method} {path} returned non-JSON: {result.stdout[:300]}")


def get_ref(repo: str, branch: str) -> str:
    payload = gh_api(f"repos/{repo}/git/ref/heads/{branch}")
    if not isinstance(payload, dict):
        die(f"unexpected ref response for {repo}/{branch}")
    return payload["object"]["sha"]


def sync_fork(fork: str) -> None:
    note(f"syncing {fork} master with upstream")
    gh_api(
        f"repos/{fork}/merge-upstream",
        "POST",
        {"branch": UPSTREAM_BRANCH},
    )


def tree_sha(repo: str, commit_sha: str) -> str:
    payload = gh_api(f"repos/{repo}/git/commits/{commit_sha}")
    if not isinstance(payload, dict):
        die(f"unexpected commit response for {repo}@{commit_sha}")
    return payload["tree"]["sha"]


def manifest_files(program: dict, version: str) -> dict[str, str]:
    directory = sync.winget_dir(program, version)
    if not directory.is_dir():
        die(f"missing {directory.relative_to(ROOT)} — run sync.py")
    files = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(directory.glob("*.yaml"))
    }
    if len(files) != 3:
        die(f"{program['name']}: expected 3 manifests, found {len(files)}")
    return files


def pr_template() -> str:
    payload = gh_api(
        f"repos/{UPSTREAM}/contents/.github/PULL_REQUEST_TEMPLATE.md"
    )
    if not isinstance(payload, dict) or "content" not in payload:
        return ""
    return base64.b64decode(payload["content"]).decode("utf-8")


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
        die(f"{name}: rebuilt {arch} bundle does not match checksums")
    existing = remote_asset(program, tag, entry["name"])
    if existing is not None:
        if existing.get("digest") == f"sha256:{digest}" or existing.get(
            "size"
        ) == len(data):
            note(f"{name}: {arch} bundle already uploaded")
            return
        note(f"{name}: {arch} bundle exists with a different digest; replacing")

    token = (
        os.environ.get("WINGET_TOKEN")
        or os.environ.get("GH_TOKEN")
        or sync._token()
    )
    if not token:
        die(f"{name}: no GitHub token available to upload the bundle")
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


def find_open_pr(owner: str, fork: str, identifier: str) -> str | None:
    """Return an open PR URL for this package, at any version, if one exists.

    WinGet allows only one open submission per package identifier, so a PR for
    an older version still blocks a newer one.
    """
    refs = gh_api(f"repos/{fork}/git/matching-refs/heads/{identifier}-")
    if isinstance(refs, list):
        for ref in refs:
            branch = ref.get("ref", "").rsplit("/", 1)[-1]
            if not branch:
                continue
            pulls = gh_api(
                f"repos/{UPSTREAM}/pulls?head={owner}:{branch}&state=open"
            )
            if isinstance(pulls, list) and pulls:
                return pulls[0].get("html_url")
    query = urllib.parse.quote(
        f'repo:{UPSTREAM} is:pr is:open "{identifier}"'
    )
    found = gh_api(f"search/issues?q={query}&per_page=10")
    if isinstance(found, dict):
        for item in found.get("items", []):
            if identifier in item.get("title", ""):
                return item.get("html_url")
    return None


def submit(program: dict, version: str, record: dict, yes: bool) -> None:
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
    if current and status.version_key(current) > status.version_key(version):
        die(f"{name}: WinGet has newer {current}; refusing to publish {version}")

    owner = gh_api("user")
    if not isinstance(owner, dict):
        die("cannot resolve the authenticated GitHub user")
    fork = f"{owner['login']}/{UPSTREAM.split('/')[1]}"
    existing = find_open_pr(owner["login"], fork, identifier)
    if existing:
        note(f"{name}: open WinGet PR already exists: {existing}")
        return

    files = manifest_files(program, version)
    relative = sync.winget_dir(program, version).relative_to(sync.WINGET_DIR)
    title = (
        f"New package: {identifier} version {version}"
        if current is None
        else f"Update {identifier} to {version}"
    )
    branch = f"{identifier}-{version}-{uuid.uuid4().hex[:8]}"

    if not yes:
        note(f"{name}: WinGet {current or 'missing'} -> {version}")
        note(f"dry run — would open a PR with title {title!r}")
        note(f"  branch: {branch}")
        note(f"  files:  {relative.as_posix()}/*.yaml")
        if config.get("bundle"):
            note("  uploads: WinGet payload bundles for every architecture")
        return

    for arch in sync.winget_arch_map(program):
        ensure_bundle(program, version, record, arch)

    upstream_sha = get_ref(UPSTREAM, UPSTREAM_BRANCH)
    sync_fork(fork)
    fork_sha = get_ref(fork, UPSTREAM_BRANCH)
    if fork_sha != upstream_sha:
        die(
            f"{fork} master is {fork_sha[:8]}, upstream is {upstream_sha[:8]}; "
            f"sync the fork or delete local commits on master"
        )

    note(f"{name}: creating branch {branch} from {upstream_sha[:8]}")
    gh_api(
        f"repos/{fork}/git/refs",
        "POST",
        {"ref": f"refs/heads/{branch}", "sha": upstream_sha},
    )

    try:
        base_tree = tree_sha(UPSTREAM, upstream_sha)
        tree = [
            {
                "path": f"{relative.as_posix()}/{filename}",
                "mode": "100644",
                "type": "blob",
                "content": content,
            }
            for filename, content in sorted(files.items())
        ]
        new_tree = gh_api(
            f"repos/{fork}/git/trees",
            "POST",
            {"base_tree": base_tree, "tree": tree},
        )
        commit = gh_api(
            f"repos/{fork}/git/commits",
            "POST",
            {
                "message": title,
                "tree": new_tree["sha"],
                "parents": [upstream_sha],
            },
        )
        gh_api(
            f"repos/{fork}/git/refs/heads/{branch}",
            "PATCH",
            {"sha": commit["sha"]},
        )
        pull = gh_api(
            f"repos/{UPSTREAM}/pulls",
            "POST",
            {
                "title": title,
                "head": f"{owner['login']}:{branch}",
                "base": UPSTREAM_BRANCH,
                "body": pr_template(),
                "maintainer_can_modify": True,
            },
        )
        note(f"{name}: PR {pull['html_url']}")
    except SystemExit:
        note(f"{name}: cleaning up branch {branch}")
        try:
            gh_api(f"repos/{fork}/git/refs/heads/{branch}", "DELETE")
        except SystemExit:
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program", action="append", required=True)
    parser.add_argument("--version", help="require this generated version")
    parser.add_argument("--yes", action="store_true", help="actually open PRs")
    args = parser.parse_args()

    programs = sync.load_programs()
    selected = sync.select_programs(programs, args.program)
    if args.version and len(selected) != 1:
        die("--version requires exactly one --program")
    checksums = sync.load_checksums()
    for program in selected:
        record = checksums.get(program["name"])
        if record is None:
            die(f"{program['name']}: no checksums — run sync.py --refresh")
        version = record["version"]
        if args.version and version != args.version:
            die(f"{program['name']}: checksums are {version}, asked for {args.version}")
        submit(program, version, record, args.yes)


if __name__ == "__main__":
    main()