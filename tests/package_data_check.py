#!/usr/bin/env python3
"""Guard the wheel's bundled catalogue and launcher icon data configuration."""
from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def main() -> int:
    setuptools = CONFIG.get("tool", {}).get("setuptools", {})
    patterns = setuptools.get("package-data", {}).get("wpt", [])
    discovery = setuptools.get("packages", {}).get("find", {})
    includes = discovery.get("include", [])
    if not any(fnmatch.fnmatchcase("wpt", pattern) for pattern in includes):
        print(f"FAIL: setuptools package discovery does not include wpt: {includes}")
        return 1

    required = [ROOT / "wpt/data/neuraldsp-catalogue.json"]
    required.extend(ROOT / f"wpt/data/icons/wpt-{size}.png" for size in (16, 32, 48, 64, 128, 256, 512))
    missing: list[str] = []
    for path in required:
        relative = path.relative_to(ROOT / "wpt").as_posix()
        if not path.is_file() or not any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns):
            missing.append(relative)
    if missing:
        print(f"FAIL: wheel package-data patterns omit: {missing}; configured={patterns}")
        return 1
    print(f"package-data check: {len(required)} runtime files covered by {patterns}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
