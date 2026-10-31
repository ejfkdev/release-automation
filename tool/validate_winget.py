#!/usr/bin/env python3
"""Validate generated WinGet manifests against the official 1.6.0 schemas.

Requires PyYAML and jsonschema:

    python3 -m pip install pyyaml jsonschema
    python3 tool/validate_winget.py

Schemas are fetched from aka.ms at run time so the repository does not vendor
Microsoft's schema files.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WINGET = ROOT / "winget"
MANIFEST_VERSION = "1.12.0"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync  # noqa: E402
TYPES = ("version", "installer", "defaultLocale")


def die(message: str) -> "NoReturn":
    print(f"validate-winget: error: {message}", file=sys.stderr)
    raise SystemExit(1)


def fetch_schema(name: str) -> dict:
    path = (
        "repos/microsoft/winget-cli/contents/schemas/JSON/manifests/"
        f"v{MANIFEST_VERSION}/manifest.{name}.{MANIFEST_VERSION}.json"
    )
    payload = sync.api_json(path)
    if not isinstance(payload, dict) or "content" not in payload:
        die(f"unexpected contents response for {path}")
    return json.loads(base64.b64decode(payload["content"]))


def main() -> None:
    try:
        import jsonschema
        import yaml
    except ImportError as exc:
        die(f"missing dependency: {exc}; install pyyaml and jsonschema")

    schemas = {name: fetch_schema(name) for name in TYPES}

    problems: list[str] = []
    count = 0
    for path in sorted(WINGET.rglob("*.yaml")):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            problems.append(f"{path.relative_to(ROOT)}: YAML: {exc}")
            continue
        manifest_type = document.get("ManifestType")
        schema = schemas.get(manifest_type)
        if schema is None:
            problems.append(
                f"{path.relative_to(ROOT)}: unknown ManifestType {manifest_type!r}"
            )
            continue
        validator = jsonschema.Draft7Validator(schema)
        for error in sorted(validator.iter_errors(document), key=lambda item: list(item.path)):
            location = "/".join(str(part) for part in error.path) or "<root>"
            problems.append(
                f"{path.relative_to(ROOT)}: {location}: {error.message}"
            )
        count += 1

    if problems:
        for problem in problems:
            print(f"validate-winget: {problem}", file=sys.stderr)
        die(f"{len(problems)} schema problem(s) in {count} manifests")
    print(f"validate-winget: {count} manifests match the {MANIFEST_VERSION} schemas")


if __name__ == "__main__":
    main()