#!/usr/bin/env python3
"""Fail when source/runtime/package/changelog versions disagree."""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_versions(root: Path) -> dict[str, str]:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = project.get("project", {}).get("version")

    runtime_text = (root / "wpt" / "__init__.py").read_text(encoding="utf-8")
    runtime_match = re.search(r"(?m)^__version__\s*=\s*['\"]([^'\"]+)['\"]", runtime_text)

    pkgbuild_text = (root / "PKGBUILD").read_text(encoding="utf-8")
    pkgbuild_match = re.search(r"(?m)^pkgver=([^\s#]+)\s*$", pkgbuild_text)

    changelog_text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    changelog_match = re.search(r"(?m)^##\s+(\d+\.\d+\.\d+)\b", changelog_text)

    versions = {
        "pyproject.toml": project_version,
        "wpt.__version__": runtime_match.group(1) if runtime_match else None,
        "PKGBUILD pkgver": pkgbuild_match.group(1) if pkgbuild_match else None,
        "CHANGELOG heading": changelog_match.group(1) if changelog_match else None,
    }
    return {key: value if isinstance(value, str) else "<missing>" for key, value in versions.items()}


def mismatches(versions: dict[str, str]) -> dict[str, str]:
    if len(set(versions.values())) == 1:
        return {}
    return versions


def main() -> int:
    versions = read_versions(ROOT)
    bad = mismatches(versions)
    if bad:
        print("version metadata mismatch:")
        for source, value in bad.items():
            print(f"  {source}: {value}")
        return 1
    print(f"version metadata consistent: {next(iter(versions.values()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
