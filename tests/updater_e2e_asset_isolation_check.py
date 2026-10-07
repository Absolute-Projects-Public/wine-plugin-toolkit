"""The updater E2E regression must not mutate its external release-asset archive."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def file_hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def main() -> int:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(r"(?m)^## (\d+\.\d+\.\d+)", changelog)
    assert match, "CHANGELOG.md has no semantic version heading"
    version = match.group(1)
    pkgbuild = (ROOT / "PKGBUILD").read_text(encoding="utf-8")
    pkgrel = next(line.removeprefix("pkgrel=") for line in pkgbuild.splitlines()
                  if line.startswith("pkgrel="))
    tarball = f"wpt-{version}.tar.gz"
    package = f"wine-plugin-toolkit-{version}-{pkgrel}-any.pkg.tar.zst"

    with tempfile.TemporaryDirectory(prefix="wpt-updater-asset-isolation-") as temp:
        root = Path(temp)
        assets = root / "assets"
        assets.mkdir()
        source_bytes = b"\x1f\x8bsynthetic source archive"
        package_bytes = b"\x28\xb5\x2f\xfdsynthetic zstd package"
        (assets / tarball).write_bytes(source_bytes)
        (assets / package).write_bytes(package_bytes)
        (assets / f"{tarball}.sha256").write_text(
            f"{hashlib.sha256(source_bytes).hexdigest()}  {tarball}\n", encoding="utf-8"
        )
        (assets / f"{package}.sha256").write_text(
            f"{hashlib.sha256(package_bytes).hexdigest()}  {package}\n", encoding="utf-8"
        )
        sentinel = b"existing archive metadata that must survive updater E2E\n"
        sums_path = assets / "sha256sums.txt"
        sums_path.write_bytes(sentinel)
        before = {p.name for p in assets.iterdir()}
        before_hashes = file_hashes(assets)

        home = root / "home"
        home.mkdir()
        tmp = root / "tmp"
        tmp.mkdir()
        env = dict(
            os.environ,
            HOME=str(home),
            XDG_CACHE_HOME=str(home / ".cache"),
            XDG_CONFIG_HOME=str(home / ".config"),
            TMPDIR=str(tmp),
            WPT_RELEASE_DIR=str(ROOT),
            WPT_RELEASE_ASSET_DIR=str(assets),
            WPT_UPDATER_E2E_ASSUME_ARCH="1",
        )
        env.pop("WPT_NO_UPDATE_CHECK", None)
        result = subprocess.run(
            [sys.executable, str(ROOT / "tests" / "updater_e2e_check.py")],
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert sums_path.read_bytes() == sentinel, (
            "updater E2E changed/deleted pre-existing sha256sums.txt in the external asset archive"
        )
        assert {p.name for p in assets.iterdir()} == before, (
            "updater E2E left synthetic files in the external release archive"
        )
        assert file_hashes(assets) == before_hashes, (
            "updater E2E modified a pre-existing file in the external release archive"
        )
        leftovers = list(tmp.glob("wpt-update-*"))
        assert not leftovers, f"updater E2E left temporary work directories in TMPDIR: {leftovers}"
        assert result.returncode == 0, result.stdout + result.stderr

    print("updater E2E external asset isolation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
