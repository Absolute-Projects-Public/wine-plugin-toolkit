#!/usr/bin/env python3
"""Verify release-version fields agree and the gate rejects drift."""
from __future__ import annotations

import runpy
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = runpy.run_path(str(ROOT / "packaging" / "check_version.py"))
read_versions = HELPER["read_versions"]
mismatches = HELPER["mismatches"]


def main() -> int:
    versions = read_versions(ROOT)
    if mismatches(versions):
        print("FAIL: current metadata disagrees:", versions)
        return 1

    with tempfile.TemporaryDirectory(prefix="wpt-version-check-") as tmp:
        root = Path(tmp)
        for filename in ("pyproject.toml", "PKGBUILD", "CHANGELOG.md"):
            shutil.copy2(ROOT / filename, root / filename)
        (root / "wpt").mkdir()
        shutil.copy2(ROOT / "wpt" / "__init__.py", root / "wpt" / "__init__.py")
        pyproject = root / "pyproject.toml"
        original = pyproject.read_text(encoding="utf-8")
        version = versions["pyproject.toml"]
        tampered = original.replace(f'version = "{version}"', 'version = "99.99.99"', 1)
        if tampered == original:
            print("FAIL: could not make the temporary metadata mismatch")
            return 1
        pyproject.write_text(tampered, encoding="utf-8")
        if not mismatches(read_versions(root)):
            print("FAIL: version gate accepted a mismatched pyproject version")
            return 1

    print(f"version consistency check: current {next(iter(versions.values()))}; mismatch rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
