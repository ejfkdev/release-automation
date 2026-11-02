# release-automation

Central catalog and publishing pipeline for [ejfkdev](https://github.com/ejfkdev)
command-line binaries. One catalog entry per program drives WinGet manifests and
npm wrapper packages; Homebrew and Scoop keep their existing tap/bucket as
distribution endpoints.

This repository does **not** build binaries. It consumes the release assets that
each project already publishes, records their SHA-256 digests, and renders the
two registries that need per-version metadata.

## Why a central repository

WinGet and npm do not require one GitHub repository per program:

- WinGet manifests live in [`microsoft/winget-pkgs`](https://github.com/microsoft/winget-pkgs).
  A release repository only needs to expose the installer URL and its hash.
- npm packages live in the npm registry. A package is a tiny wrapper around the
  release binary, not a copy of the project.

Keeping the catalog here means adding a program is one TOML file, not a new
copy of the release scripts. The generated manifests and npm packages are
committed so every change is reviewable and `sync.py --check` can verify them
without network access.

## Layout

```text
programs/<name>.toml        hand-maintained catalog (one program per file)
checksums.toml              generated: release version + per-asset SHA-256
winget/manifests/...        generated: WinGet version/installer/locale YAML
npm/<name>/                 generated: package.json, install.js, bin shims
tool/sync.py                catalog -> checksums + manifests + npm packages
tool/status.py              release vs npm vs WinGet drift report
tool/publish.py             npm publish / WinGet PR / payload bundles
```

Pipeline:

```text
programs/*.toml
      |
      v
tool/sync.py --refresh  ---> checksums.toml + winget/ + npm/
      |
      v
tool/publish.py         ---> npm registry / PR to microsoft/winget-pkgs
```

## Coverage

| Program | WinGet ID | npm | Notes |
| --- | --- | --- | --- |
| `ddc` | `ejfkdev.ddc` | `@ejfkdev/ddc` | Already published at 0.1.33 |
| `dj` | `ejfkdev.dj` | `@ejfkdev/dj` | |
| `udf` | `ejfkdev.udf` | `@ejfkdev/udf` | zip/tar.gz release assets |
| `apix` | `ejfkdev.apix` | `@ejfkdev/apix` | x64 Windows only |
| `tspc` | `ejfkdev.tspc` | `@ejfkdev/tspc` | from `typespec-rs` |
| `vcc` | `ejfkdev.vcc` | `@ejfkdev/vcc` | x64 Windows only |
| `zaip` | `ejfkdev.zaip` | `@ejfkdev/zaip` | client + server; proprietary |
| `saw` | `ejfkdev.saw` | `@ejfkdev/saw` | client + server + shell |
| `dns` | `ejfkdev.dns` | `@ejfkdev/dns` | x64 Windows only |
| `oss` | `ejfkdev.oss` | `@ejfkdev/oss` | zip/tar.gz; proprietary |
| `jd` | `ejfkdev.jd` | `@ejfkdev/jd` | |
| `dae` | `ejfkdev.dae` | `@ejfkdev/dae` | |
| `ov` | `ejfkdev.ov` | `@ejfkdev/ov` | |
| `jcdc` | `ejfkdev.jcdc` | `@ejfkdev/jcdc` | |
| `pycdc` | `ejfkdev.pycdc` | `@ejfkdev/pycdc` | `pycdc` + `pycdas` |
| `avdroot` | `ejfkdev.avdroot` | `@ejfkdev/avdroot` | |
| `jscd` | `ejfkdev.jscd` | `@ejfkdev/jscd` | |

"x64 Windows only" means the upstream release has no `aarch64-pc-windows-msvc`
asset; the WinGet manifest and the npm package simply omit that platform.
`zaip` and `oss` have no LICENSE file, so their manifests declare
`License: Proprietary` / `UNLICENSED`. Adding a license upstream and updating
the catalog is enough to switch.

## Catalog schema

```toml
schema = 1
name = "foo"
repo = "ejfkdev/foo"
display_name = "Foo"
summary = "One-line description used by npm and WinGet"
description = "Longer WinGet description."
license = "MIT"
homepage = "https://github.com/ejfkdev/foo"
keywords = ["foo", "cli"]
bins = ["foo"]                 # one or more commands

[npm]
package = "@ejfkdev/foo"       # must be @ejfkdev/ scoped

[winget]
id = "ejfkdev.foo"             # permanent once published
tags = ["foo", "cli"]
# bundle = true                # multi-command zip payload (see below)

[assets]
"x86_64-apple-darwin" = "foo-v{version}-x86_64-apple-darwin"
"aarch64-apple-darwin" = "foo-v{version}-aarch64-apple-darwin"
"x86_64-unknown-linux-gnu" = "foo-v{version}-x86_64-unknown-linux-gnu"
"aarch64-unknown-linux-gnu" = "foo-v{version}-aarch64-unknown-linux-gnu"
"x86_64-pc-windows-msvc" = "foo-v{version}-x86_64-pc-windows-msvc.exe"
"aarch64-pc-windows-msvc" = "foo-v{version}-aarch64-pc-windows-msvc.exe"
```

Target keys are fixed by the tool:

```text
x86_64-apple-darwin          aarch64-apple-darwin
x86_64-unknown-linux-gnu     aarch64-unknown-linux-gnu
x86_64-pc-windows-msvc       aarch64-pc-windows-msvc
```

Asset names accept `{version}`, `{tag}` and `{exe}`. Archives are described with
a table; `member` is the file inside the archive and may also use `{version}`:

```toml
[assets."x86_64-apple-darwin"]
file = "foo-v{version}-darwin-amd64.tar.gz"
member = "foo"                 # archive = "tar.gz" is inferred from the suffix

[assets."x86_64-pc-windows-msvc"]
file = "foo-v{version}-windows-amd64.zip"
member = "foo-v{version}-windows-amd64.exe"
```

Multi-command programs use a table of commands per target:

```toml
bins = ["pycdc", "pycdas"]

[assets."x86_64-pc-windows-msvc"]
pycdc = "pycdc-x86_64-windows.exe"
pycdas = "pycdas-x86_64-windows.exe"
```

For WinGet, a single portable EXE cannot expose multiple commands. Set
`bundle = true` and the tool builds one deterministic `ZIP_STORED` payload per
architecture, containing every command as `<command>.exe`. The manifest uses
`InstallerType: zip` + `NestedInstallerType: portable` +
`NestedInstallerFiles`. `publish.py` uploads the payload to the program's
release before opening the PR. npm does not need this: its installer downloads
each command and writes one launcher per command.

Version resolution: by default the latest GitHub release tag is used
(`v0.6.4` -> `0.6.4`). A catalog may pin `version = "..."` and optionally
`tag = "v{version}"`. Use `--version` on a single program to render a specific
tag without changing the catalog.

## Commands

```bash
# Render from the committed checksums (no network)
python3 tool/sync.py

# Fetch the latest release of every program, record hashes, render
python3 tool/sync.py --refresh

# Verify committed output exactly matches the catalog (CI)
python3 tool/sync.py --check

# Drift report: release vs npm vs WinGet
python3 tool/status.py
python3 tool/status.py --plan --json      # matrices for publish.yml

# Dry runs by default; add --yes to actually publish
python3 tool/publish.py npm    --program ddc
python3 tool/publish.py winget --program ddc --yes
python3 tool/publish.py npm    --all --yes

# Build the multi-command WinGet payload without publishing
python3 tool/publish.py bundle --program pycdc --out dist
```

`--refresh` skips programs whose recorded version/tag and all hashes are
complete, so the daily run costs one release lookup per program and no asset
downloads. Pass `--force` to recompute a version whose assets were replaced.

## Adding a program

1. Make sure every target listed in the catalog has an asset in the release.
   `sync.py --refresh` prints the missing names and the available assets.
2. Copy an existing `programs/*.toml` and edit the fields.
3. Run `python3 tool/sync.py --refresh --program <name>`.
4. Run `python3 tool/sync.py --check`.
5. Publish the first version:
   `python3 tool/publish.py npm --program <name> --yes` and
   `python3 tool/publish.py winget --program <name> --yes`.

No workflow edit is required: the daily reconcile picks up the new catalog file.
If the program lives in a different GitHub account, use `repo = "owner/name"`;
the tool only assumes public release assets.

## Publishing and credentials

| Secret | Used by | Notes |
| --- | --- | --- |
| `NPM_TOKEN` | npm job | Granular token with publish permission and 2FA bypass, or a logged-in `npm` session locally |
| `WINGET_TOKEN` | WinGet job | Classic PAT with `public_repo`; used for the fork/PR and for uploading payload bundles to the program release |
| `GITHUB_TOKEN` | reconcile/plan | Automatic; reads public releases and commits generated files |

WinGet submission uses the GitHub REST API through the `gh` CLI
(`tool/submit_winget.py`): it syncs the fork's `master`, creates a branch from
upstream, writes the three YAML files under `manifests/...`, and opens the PR
with the upstream template. No Windows runner or `wingetcreate` binary is
needed; the same command works locally with an authenticated `gh` session.

Locally, an authenticated `gh` session is enough to submit: `submit_winget.py`
calls `gh api`, so no `WINGET_TOKEN` is needed on your machine. The secret is
only for the `publish.yml` workflow, where a token must be passed explicitly.

The first WinGet package from an account needs a one-time Microsoft CLA reply on
the PR. This account already has a passing `license/cla` check (`#466817`), so
new PRs only need the label to settle; no new signature is required.

npm's first publication of a brand-new package may be held for review and show
a `0.0.0-stage` placeholder before the real version becomes installable. Later
versions publish normally.

### Workflows

- `ci.yml` runs on push/PR: Python syntax, `sync.py --check`, YAML parse, and
  `npm pack --dry-run` for every package.
- `reconcile.yml` runs daily and on demand: refreshes hashes/manifests, verifies
  them, prints the drift table, and commits changes.
- `auto-publish-npm.yml` runs after every successful reconcile (and on demand):
  publishes npm packages whose registry version is behind the generated catalog.
  It skips up-to-date packages and packages still in npm's first-publish review
  (`0.0.0-stage`). Authentication currently uses `NPM_TOKEN`; OIDC trusted
  publishing is the planned replacement when that token expires.
- `publish.yml` is manual. It refreshes and commits first, then publishes the
  selected channels. `dry_run` defaults to `true`; set it to `false` to publish.
  Inputs: `programs` (`all` or comma-separated names), `channels`
  (`npm`, `winget`), `dry_run`.

## Notes and limitations

- The catalog is intentionally verbose about per-target asset names. A single
  naming template would not survive the five different conventions already used
  by these projects.
- npm packages download at install time and verify the SHA-256 from the release
  metadata. If install scripts are disabled, the launcher fetches the binary on
  first use. npm 11 warns about unapproved `postinstall` scripts but still runs
  them; a future allow-list change is covered by the launcher fallback.
- `EJFKDEV_DOWNLOAD_PROXY=https://...` makes npm installers prefix download
  URLs with a mirror; `GITHUB_DOWNLOAD_PROXY` does the same for `sync.py`.
  Every download is still hashed and compared with the GitHub API digest, so a
  mirror cannot change what is installed.
- Multi-command WinGet payloads use `ZIP_STORED` and fixed timestamps so the
  same inputs produce the same SHA-256 on macOS and Linux runners.
- WinGet package IDs are permanent. Do not rename `ejfkdev.<program>` after the
  first merged manifest; a rename is a new package and a new review.
- `proprietary` entries (`zaip`, `oss`) have no upstream license file. This is a
  catalog value, not a legal review; add a LICENSE upstream if that changes.

## License

The tooling and generated metadata in this repository are MIT licensed. The
binaries downloaded by the generated packages remain under each project's own
license.