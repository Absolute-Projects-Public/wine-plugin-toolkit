#!/usr/bin/env python3
"""A release source tarball must not silently include untracked or edited files."""
from __future__ import annotations

import hashlib
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "packaging" / "make-tarball.sh"


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True)


def fixture(repo: Path) -> None:
    for name in ("wpt", "tests", "packaging", "docs"):
        (repo / name).mkdir()
    for name in ("README.md", "TESTING.md", "CONTRIBUTING.md", "CHANGELOG.md",
                 "LICENSE", "pyproject.toml"):
        (repo / name).write_text(f"{name}\n")
    (repo / "PKGBUILD").write_text("pkgver=0.6.4\nsha256sums=('old')\n")
    (repo / "wpt" / "__init__.py").write_text("__version__ = '0.6.4'\n")
    (repo / "tests" / "dummy.py").write_text("pass\n")
    (repo / "docs" / "DESIGN.md").write_text("review map\n")
    (repo / "packaging" / "make-tarball.sh").write_bytes(BUILDER.read_bytes())
    (repo / "packaging" / "release.sh").write_bytes((ROOT / "packaging" / "release.sh").read_bytes())
    assert run("git", "init", "-q", cwd=repo).returncode == 0
    assert run("git", "add", "-A", cwd=repo).returncode == 0
    p = run("git", "-c", "user.name=test", "-c", "user.email=test" + "@" + "example.invalid",
            "commit", "-qm", "fixture", cwd=repo)
    assert p.returncode == 0, p.stderr


def clean_build_and_untracked_refusal() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-tarball-check-") as tmp:
        repo = Path(tmp)
        fixture(repo)
        clean = run("bash", "packaging/make-tarball.sh", cwd=repo)
        assert clean.returncode == 0, (clean.stdout, clean.stderr)
        tarball = repo / "dist" / "wpt-0.6.4.tar.gz"
        assert tarball.exists()
        before = hashlib.sha256(tarball.read_bytes()).hexdigest()
        with tarfile.open(tarball) as tf:
            names = set(tf.getnames())
            assert "wine-plugin-toolkit-0.6.4/CONTRIBUTING.md" in names
            assert "wine-plugin-toolkit-0.6.4/docs/DESIGN.md" in names
        (repo / "docs" / "private-draft.md").write_text("private" + "@" + "example.com\n")
        attempted = run("bash", "packaging/make-tarball.sh", cwd=repo)
        assert attempted.returncode != 0, (attempted.stdout, attempted.stderr)
        assert hashlib.sha256(tarball.read_bytes()).hexdigest() == before
    print("ok clean build; untracked shipped-subtree file refuses without replacing tarball")


def tracked_edit_refusal() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-tarball-check-") as tmp:
        repo = Path(tmp)
        fixture(repo)
        (repo / "README.md").write_text("changed but not committed\n")
        attempted = run("bash", "packaging/make-tarball.sh", cwd=repo)
        assert attempted.returncode != 0, (attempted.stdout, attempted.stderr)
    print("ok uncommitted tracked edit refuses the release builder")


def release_refuses_unpinned_tarball() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-tarball-check-") as tmp:
        repo = Path(tmp)
        fixture(repo)
        bindir = repo / "fake-bin"; bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text("#!/bin/sh\nprintf 'gh: Not Found (HTTP 404)\\n' >&2\nexit 1\n")
        gh.chmod(0o755)
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"])
        attempted = subprocess.run(["bash", "packaging/release.sh"], cwd=repo, env=env,
                                   capture_output=True, text=True)
        assert attempted.returncode != 0, (attempted.stdout, attempted.stderr)
        assert "tarball hash has changed" in attempted.stdout, attempted.stdout
        assert "==> publish" not in attempted.stdout, attempted.stdout
    print("ok release helper stops before publication steps when pin mismatches")


def release_refuses_existing_tag() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-tarball-check-") as tmp:
        repo = Path(tmp)
        fixture(repo)
        assert run("git", "tag", "v0.6.4", cwd=repo).returncode == 0
        attempted = run("bash", "packaging/release.sh", cwd=repo)
        assert attempted.returncode != 0
        assert "tag already exists" in (attempted.stdout + attempted.stderr), (attempted.stdout, attempted.stderr)
        assert not (repo / "dist" / "wpt-0.6.4.tar.gz").exists()
    print("ok release helper refuses to recreate a tagged version")


def release_refuses_remote_version() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-tarball-check-") as tmp:
        repo = Path(tmp)
        fixture(repo)
        bindir = repo / "fake-bin"; bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text("#!/bin/sh\nprintf 'v0.6.4\\n'\n")
        gh.chmod(0o755)
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"])
        attempted = subprocess.run(["bash", "packaging/release.sh"], cwd=repo, env=env,
                                   capture_output=True, text=True)
        assert attempted.returncode != 0
        assert "already published" in (attempted.stdout + attempted.stderr), (attempted.stdout, attempted.stderr)
        assert not (repo / "dist" / "wpt-0.6.4.tar.gz").exists()
    print("ok release helper refuses a remotely published version")


if __name__ == "__main__":
    clean_build_and_untracked_refusal()
    tracked_edit_refusal()
    release_refuses_unpinned_tarball()
    release_refuses_existing_tag()
    release_refuses_remote_version()
    print("tarball source check: 5/5 passed")
