#!/usr/bin/env python3
"""Regression checks for the public-surface identifier scanner."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
script = ROOT / "packaging" / "scan-identifiers.py"
spec = importlib.util.spec_from_file_location("scan_identifiers", script)
assert spec and spec.loader
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


def tracked_path_with_spaces() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-scan-") as tmp:
        repo = Path(tmp)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "with spaces.txt").write_text("Contact: private" + "@" + "example.com\n")
        subprocess.run(["git", "-C", str(repo), "add", "with spaces.txt"], check=True)
        result = scanner.scan_files(repo)
        assert result == [("with spaces.txt", ["an email address"])], result
    print("ok tracked paths with spaces are scanned")


def missing_asset_dir_is_reported_as_skipped() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-scan-") as tmp:
        repo = Path(tmp)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        old = sys.argv
        try:
            sys.argv = [str(script), str(repo), "--no-releases"]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = scanner.main()
        finally:
            sys.argv = old
        text = output.getvalue()
        assert code == 0, (code, text)
        assert "=== tarball assets ===\n  SKIPPED" in text, text
        assert "=== tarball assets ===\n  nothing identifying found" not in text, text
    print("ok omitted --assets is not reported as a clean tarball scan")


def empty_asset_dir_is_an_error() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-scan-") as tmp:
        repo = Path(tmp)
        assets = repo / "assets"; assets.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        old = sys.argv
        try:
            sys.argv = [str(script), str(repo), "--no-releases", "--assets", str(assets)]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = scanner.main()
        finally:
            sys.argv = old
        assert code != 0 and "no tarballs found" in output.getvalue(), (code, output.getvalue())
    print("ok empty asset directory cannot pass as a clean scan")


def oversized_tar_member_is_an_error() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-scan-") as tmp:
        assets = Path(tmp)
        with tarfile.open(assets / "fixture.tar.gz", "w:gz") as tf:
            payload = b"x" * 4_000_001
            info = tarfile.TarInfo("root/oversized.txt")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        result = scanner.scan_tarballs(assets)
        assert result and "unscanned" in str(result).lower(), result
    print("ok oversized tar member cannot silently pass as clean")


if __name__ == "__main__":
    tracked_path_with_spaces()
    missing_asset_dir_is_reported_as_skipped()
    empty_asset_dir_is_an_error()
    oversized_tar_member_is_an_error()
    print("scanner check: 4/4 passed")
