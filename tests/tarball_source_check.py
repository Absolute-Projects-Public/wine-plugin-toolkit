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
LOCAL_BUILDER = ROOT / "packaging" / "build-local.sh"


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    tmp_base = Path(tempfile.gettempdir())
    if not tmp_base.is_dir():
        tmp_base = Path("/tmp")
    env = dict(os.environ, TMPDIR=str(tmp_base.resolve()))
    return subprocess.run(list(args), cwd=cwd, env=env, capture_output=True, text=True)


def fixture(repo: Path) -> None:
    for name in ("wpt", "tests", "packaging", "docs"):
        (repo / name).mkdir()
    for name in ("README.md", "TESTING.md", "CONTRIBUTING.md", "CHANGELOG.md",
                 "LICENSE", "pyproject.toml"):
        (repo / name).write_text(f"{name}\n")
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## 0.6.4\n")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "wine-plugin-toolkit"\nversion = "0.6.4"\n'
    )
    (repo / "PKGBUILD").write_text("pkgver=0.6.4\npkgrel=1\nsha256sums=('old')\n")
    (repo / "wpt" / "__init__.py").write_text("__version__ = '0.6.4'\n")
    (repo / "tests" / "dummy.py").write_text("pass\n")
    (repo / "docs" / "DESIGN.md").write_text("review map\n")
    (repo / "packaging" / "make-tarball.sh").write_bytes(BUILDER.read_bytes())
    (repo / "packaging" / "check_version.py").write_bytes(
        (ROOT / "packaging" / "check_version.py").read_bytes()
    )
    (repo / "packaging" / "release.sh").write_bytes((ROOT / "packaging" / "release.sh").read_bytes())
    (repo / "packaging" / "build-local.sh").write_bytes(LOCAL_BUILDER.read_bytes())
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
        loose_umask = run("bash", "-c", "umask 0002; bash packaging/make-tarball.sh", cwd=repo)
        assert loose_umask.returncode == 0, (loose_umask.stdout, loose_umask.stderr)
        after = hashlib.sha256(tarball.read_bytes()).hexdigest()
        assert after == before, (before, after)
        with tarfile.open(tarball) as tf:
            names = set(tf.getnames())
            assert "wine-plugin-toolkit-0.6.4/CONTRIBUTING.md" in names
            assert "wine-plugin-toolkit-0.6.4/docs/DESIGN.md" in names
        (repo / "docs" / "private-draft.md").write_text("private" + "@" + "example.com\n")
        attempted = run("bash", "packaging/make-tarball.sh", cwd=repo)
        assert attempted.returncode != 0, (attempted.stdout, attempted.stderr)
        assert hashlib.sha256(tarball.read_bytes()).hexdigest() == before
    print("ok clean tarball is umask-independent; untracked shipped-subtree file refuses")


def tracked_edit_refusal() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-tarball-check-") as tmp:
        repo = Path(tmp)
        fixture(repo)
        (repo / "README.md").write_text("changed but not committed\n")
        attempted = run("bash", "packaging/make-tarball.sh", cwd=repo)
        assert attempted.returncode != 0, (attempted.stdout, attempted.stderr)
    print("ok uncommitted tracked edit refuses the release builder")


def version_drift_refusal() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-tarball-check-") as tmp:
        repo = Path(tmp)
        fixture(repo)
        project = repo / "pyproject.toml"
        project.write_text(project.read_text().replace('version = "0.6.4"', 'version = "9.9.9"'))
        assert run("git", "add", "pyproject.toml", cwd=repo).returncode == 0
        committed = run("git", "-c", "user.name=test", "-c", "user.email=test" + "@" + "example.invalid",
                        "commit", "-qm", "version drift", cwd=repo)
        assert committed.returncode == 0, committed.stderr
        attempted = run("bash", "packaging/make-tarball.sh", cwd=repo)
        assert attempted.returncode != 0
        assert "version metadata mismatch" in (attempted.stdout + attempted.stderr)
        assert not (repo / "dist" / "wpt-0.6.4.tar.gz").exists()
    print("ok source builder refuses committed version drift")


def local_builder_requires_pin_and_refuses_bypass() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-local-build-check-") as tmp:
        repo = Path(tmp)
        fixture(repo)
        bindir = repo / "fake-bin"
        bindir.mkdir()
        marker = repo / "makepkg-called"
        fake_makepkg = bindir / "makepkg"
        fake_makepkg.write_text(f"#!/bin/sh\nprintf called > {str(marker)!r}\n")
        fake_makepkg.chmod(0o755)
        env = dict(
            os.environ,
            PATH=str(bindir) + os.pathsep + os.environ["PATH"],
            WPT_ARCHIVE_DIR=str(repo / "artifacts"),
        )
        mismatch = subprocess.run(
            ["bash", str(repo / "packaging" / "build-local.sh")], cwd=repo.parent, env=env,
            capture_output=True, text=True,
        )
        assert mismatch.returncode != 0, (mismatch.stdout, mismatch.stderr)
        assert "does not match PKGBUILD pin" in (mismatch.stdout + mismatch.stderr)
        assert not marker.exists(), "makepkg ran with a mismatched pin"
        assert not (repo / "artifacts").exists(), "mismatched pin created an archive directory"
        assert not (repo / "wpt-0.6.4.tar.gz").exists(), "helper left a root tarball behind"

        for options in (
            ["--skipchecksums"], ["--skipc"], ["--skipi"], ["-p", "other.PKGBUILD"],
            ["--config", "unsafe.conf"],
        ):
            refused = subprocess.run(
                ["bash", "packaging/build-local.sh", *options], cwd=repo, env=env,
                capture_output=True, text=True,
            )
            assert refused.returncode != 0, (options, refused.stdout, refused.stderr)
            assert "refusing makepkg options" in (refused.stdout + refused.stderr)
            assert not marker.exists(), f"makepkg ran with options {options!r}"
    print("ok local builder refuses an unpinned tarball and checksum-bypass flags")


def local_builder_builds_from_the_pinned_source() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-local-build-check-") as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        fixture(repo)
        initial = run("bash", "packaging/make-tarball.sh", cwd=repo)
        assert initial.returncode == 0, (initial.stdout, initial.stderr)
        tarball = repo / "dist" / "wpt-0.6.4.tar.gz"
        digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
        pkgbuild = repo / "PKGBUILD"
        pkgbuild.write_text(pkgbuild.read_text().replace("sha256sums=('old')", f"sha256sums=('{digest}')"))
        assert run("git", "add", "PKGBUILD", cwd=repo).returncode == 0
        pinned_commit = run(
            "git", "-c", "user.name=test", "-c", "user.email=test" + "@" + "example.invalid",
            "commit", "-qm", "pin fixture tarball", cwd=repo,
        )
        assert pinned_commit.returncode == 0, pinned_commit.stderr
        fixed_point = run("bash", "packaging/make-tarball.sh", cwd=repo)
        assert fixed_point.returncode == 0, (fixed_point.stdout, fixed_point.stderr)
        assert hashlib.sha256(tarball.read_bytes()).hexdigest() == digest

        bindir = repo / "fake-bin"
        bindir.mkdir()
        fake_makepkg = bindir / "makepkg"
        fake_makepkg.write_text(
            "#!/bin/sh\nset -eu\n"
            "test \"$#\" -eq 1 && test \"$1\" = -f\n"
            "test \"$PKGDEST\" = \"$PWD/out\" && test \"$SRCDEST\" = \"$PWD\"\n"
            "test \"$PKGEXT\" = .pkg.tar.zst && test \"$BUILDDIR\" = \"$PWD/build\"\n"
            "test \"$HOME\" = \"$PWD/envroot\" && test \"$TMPDIR\" = \"$PWD/tmp\"\n"
            "test \"$XDG_CONFIG_HOME\" = \"$HOME/.config\" && test \"$XDG_CACHE_HOME\" = \"$HOME/.cache\"\n"
            "test \"$XDG_DATA_HOME\" = \"$HOME/.local/share\" && test \"$WINEPREFIX\" = \"$HOME/synthetic-prefix\"\n"
            "test \"$WPT_NO_UPDATE_CHECK\" = 1 && test \"$WPT_RELEASE_DIR\" = \"$HOME/release-source\"\n"
            "test \"$WPT_RELEASE_ASSET_DIR\" = \"$HOME/release-assets\" && test ! -e \"$WPT_RELEASE_ASSET_DIR\"\n"
            "PIN=$(sed -n \"s/^sha256sums=('\\\\([0-9a-f]*\\\\)').*/\\\\1/p\" PKGBUILD)\n"
            "ACTUAL=$(sha256sum \"$SRCDEST/wpt-0.6.4.tar.gz\" | cut -d' ' -f1)\n"
            "test \"$PIN\" = \"$ACTUAL\"\n"
            "mkdir -p \"$PKGDEST\"\n"
            "printf synthetic-package > \"$PKGDEST/wine-plugin-toolkit-0.6.4-1-any.pkg.tar.zst\"\n"
        )
        fake_makepkg.chmod(0o755)
        artifacts = repo.parent / "relative-artifacts"
        relative_tmp = repo.parent / "relative-tmp"
        relative_tmp.mkdir()
        env = dict(
            os.environ,
            PATH=str(bindir) + os.pathsep + os.environ["PATH"],
            TMPDIR="relative-tmp",
            WPT_ARCHIVE_DIR="relative-artifacts",
        )
        built = subprocess.run(
            ["bash", str(repo / "packaging" / "build-local.sh")], cwd=repo.parent, env=env,
            capture_output=True, text=True,
        )
        assert built.returncode == 0, (built.stdout, built.stderr)
        package_name = "wine-plugin-toolkit-0.6.4-1-any.pkg.tar.zst"
        for name in (package_name, package_name + ".sha256", "wpt-0.6.4.tar.gz", "wpt-0.6.4.tar.gz.sha256"):
            assert (artifacts / name).is_file(), name
        for checksum in (package_name + ".sha256", "wpt-0.6.4.tar.gz.sha256"):
            verified = run("sha256sum", "-c", checksum, cwd=artifacts)
            assert verified.returncode == 0, (checksum, verified.stdout, verified.stderr)
        assert not (repo / "wpt-0.6.4.tar.gz").exists(), "helper left a root tarball behind"

        for asset_name in (package_name, "wpt-0.6.4.tar.gz"):
            sidecar = artifacts / f"{asset_name}.sha256"
            target = relative_tmp / f"{asset_name}.sidecar-target"
            target.write_text("must not be changed through a sidecar symlink", encoding="utf-8")
            sidecar.unlink()
            sidecar.symlink_to(target)
            unsafe = subprocess.run(
                ["bash", str(repo / "packaging" / "build-local.sh")], cwd=repo.parent, env=env,
                capture_output=True, text=True,
            )
            assert unsafe.returncode != 0, (unsafe.stdout, unsafe.stderr)
            assert "refusing to overwrite non-regular archive path" in (unsafe.stdout + unsafe.stderr)
            assert target.read_text(encoding="utf-8") == "must not be changed through a sidecar symlink"
            sidecar.unlink()
            target.unlink()
            digest = hashlib.sha256((artifacts / asset_name).read_bytes()).hexdigest()
            sidecar.write_text(f"{digest}  {asset_name}\n", encoding="utf-8")

        archived_package = artifacts / package_name
        previous = b"existing archive artifact that must not be replaced"
        archived_package.write_bytes(previous)
        attempted = subprocess.run(
            ["bash", str(repo / "packaging" / "build-local.sh")], cwd=repo.parent, env=env,
            capture_output=True, text=True,
        )
        assert attempted.returncode != 0, (attempted.stdout, attempted.stderr)
        assert "refusing to overwrite" in (attempted.stdout + attempted.stderr)
        assert archived_package.read_bytes() == previous, "existing package artifact was overwritten"

        archived_package.write_bytes(b"synthetic-package")
        archived_tarball = artifacts / "wpt-0.6.4.tar.gz"
        previous_tarball = b"existing source archive that must not be replaced"
        archived_tarball.write_bytes(previous_tarball)
        attempted = subprocess.run(
            ["bash", str(repo / "packaging" / "build-local.sh")], cwd=repo.parent, env=env,
            capture_output=True, text=True,
        )
        assert attempted.returncode != 0, (attempted.stdout, attempted.stderr)
        assert "refusing to overwrite" in (attempted.stdout + attempted.stderr)
        assert archived_tarball.read_bytes() == previous_tarball, "existing source tarball was overwritten"
        assert archived_package.read_bytes() == b"synthetic-package", "package changed before tarball refusal"
        assert not list(relative_tmp.iterdir()), "relative TMPDIR scratch was not cleaned"
    print("ok local builder uses the pinned tarball, absolute scratch paths and preserves conflicting assets")


def legacy_make_tarball_build_mode_is_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-tarball-check-") as tmp:
        repo = Path(tmp)
        fixture(repo)
        refused = run("bash", "packaging/make-tarball.sh", "--build", cwd=repo)
        assert refused.returncode == 2, (refused.stdout, refused.stderr)
        assert "use packaging/build-local.sh" in (refused.stdout + refused.stderr)
        assert not (repo / "dist" / "wpt-0.6.4.tar.gz").exists()
        assert not (repo / "wpt-0.6.4.tar.gz").exists()
        assert not (repo / "src").exists() and not (repo / "pkg").exists()
    print("ok legacy make-tarball --build refuses before leaving working-tree artifacts")


def make_tarball_resolves_relative_tmpdir_from_caller() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-tarball-tmpdir-check-") as tmp:
        parent = Path(tmp)
        repo = parent / "repo"
        repo.mkdir()
        fixture(repo)
        scratch = parent / "relative-scratch"
        scratch.mkdir()
        env = dict(os.environ, TMPDIR="relative-scratch")
        result = subprocess.run(
            ["bash", str(repo / "packaging" / "make-tarball.sh")],
            cwd=parent,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert (repo / "dist" / "wpt-0.6.4.tar.gz").is_file()
        assert not list(scratch.iterdir()), "tarball scratch files were not cleaned"
    print("ok source tarball builder resolves relative TMPDIR from the caller")


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
        status = run("git", "status", "--porcelain", "--untracked-files=all", "--", "wpt", cwd=repo)
        assert "tarball hash has changed" in attempted.stdout, (
            attempted.stdout, attempted.stderr, attempted.returncode, status.stdout)
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


def release_uses_declared_pkgrel() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-tarball-check-") as tmp:
        repo = Path(tmp)
        fixture(repo)
        pkgbuild = repo / "PKGBUILD"
        pkgbuild.write_text(pkgbuild.read_text().replace("pkgrel=1", "pkgrel=7"))
        assert run("git", "add", "PKGBUILD", cwd=repo).returncode == 0
        changed = run(
            "git", "-c", "user.name=test", "-c", "user.email=test" + "@" + "example.invalid",
            "commit", "-qm", "use non-default pkgrel", cwd=repo,
        )
        assert changed.returncode == 0, changed.stderr

        initial = run("bash", "packaging/make-tarball.sh", cwd=repo)
        assert initial.returncode == 0, (initial.stdout, initial.stderr)
        tarball = repo / "dist" / "wpt-0.6.4.tar.gz"
        digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
        pkgbuild.write_text(pkgbuild.read_text().replace("sha256sums=('old')", f"sha256sums=('{digest}')"))
        assert run("git", "add", "PKGBUILD", cwd=repo).returncode == 0
        pinned = run(
            "git", "-c", "user.name=test", "-c", "user.email=test" + "@" + "example.invalid",
            "commit", "-qm", "pin non-default release tarball", cwd=repo,
        )
        assert pinned.returncode == 0, pinned.stderr

        bindir = repo / "fake-bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text("#!/bin/sh\nprintf 'gh: Not Found (HTTP 404)\\n' >&2\nexit 1\n")
        gh.chmod(0o755)
        relative_archive = "release-relative-assets"
        with tempfile.TemporaryDirectory(prefix="wpt-release-relative-tmp-", dir=repo.parent) as tmpdir:
            relative_tmpdir = Path(tmpdir).name
            env = dict(
                os.environ,
                PATH=str(bindir) + os.pathsep + os.environ["PATH"],
                TMPDIR=relative_tmpdir,
                WPT_ARCHIVE_DIR=relative_archive,
            )
            result = subprocess.run(
                ["bash", str(repo / "packaging/release.sh")], cwd=repo.parent, env=env,
                capture_output=True, text=True,
            )
            assert result.returncode == 0, (result.stdout, result.stderr)
            assert f"writes source and package checksum files under:\n      {repo.parent / relative_archive}" in result.stdout
            assert not (repo / relative_archive).exists()
            assert "wine-plugin-toolkit-0.6.4-7-any.pkg.tar.zst" in result.stdout
            assert "wine-plugin-toolkit-0.6.4-1-any.pkg.tar.zst" not in result.stdout
            assert "WPT_REQUIRE_UPDATER_E2E=1" in result.stdout
            assert "WPT_RELEASE_ASSET_DIR=" in result.stdout
            assert "packaging/run-suites.sh" in result.stdout
    print("ok release helper uses the PKGBUILD package release number")


if __name__ == "__main__":
    clean_build_and_untracked_refusal()
    tracked_edit_refusal()
    version_drift_refusal()
    local_builder_requires_pin_and_refuses_bypass()
    local_builder_builds_from_the_pinned_source()
    legacy_make_tarball_build_mode_is_refused()
    make_tarball_resolves_relative_tmpdir_from_caller()
    release_refuses_unpinned_tarball()
    release_refuses_existing_tag()
    release_refuses_remote_version()
    release_uses_declared_pkgrel()
    print("tarball source check: 11/11 passed")
