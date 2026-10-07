"""The release suite runner must pass an external asset directory to updater E2E."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_ASSETS = (
    "wpt-0.6.5.tar.gz",
    "wpt-0.6.5.tar.gz.sha256",
    "wine-plugin-toolkit-0.6.5-1-any.pkg.tar.zst",
    "wine-plugin-toolkit-0.6.5-1-any.pkg.tar.zst.sha256",
)


def populate_assets(directory: Path) -> None:
    import hashlib

    directory.mkdir(parents=True, exist_ok=True)
    payloads = {
        RELEASE_ASSETS[0]: b"synthetic source archive",
        RELEASE_ASSETS[2]: b"synthetic Arch package",
    }
    for name, payload in payloads.items():
        (directory / name).write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        (directory / f"{name}.sha256").write_text(f"{digest}  {name}\n", encoding="utf-8")


def log_from(output: str, suite: str) -> Path:
    line = next(line for line in output.splitlines() if line.startswith("suite logs: "))
    return Path(line.removeprefix("suite logs: ")) / f"suite-{suite}.log"


def assert_scratch_removed(log: Path) -> None:
    line = next(line for line in log.read_text().splitlines() if line.startswith("test scratch: "))
    scratch = Path(line.removeprefix("test scratch: "))
    assert not scratch.exists(), f"runner left temporary suite scratch behind: {scratch}"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="wpt-run-suites-assets-") as scratch:
        root = Path(scratch)
        tree = root / "extracted-release"
        assets = root / "built-assets"
        tmp = root / "tmp"
        home = root / "home"
        (tree / "tests").mkdir(parents=True)
        (tree / "CHANGELOG.md").write_text("# Changelog\n\n## 0.6.5\n\n## 0.6.4\n", encoding="utf-8")
        (tree / "PKGBUILD").write_text("pkgver=0.6.5\npkgrel=1\n", encoding="utf-8")
        populate_assets(assets)
        tmp.mkdir()
        home.mkdir()
        (tree / "tests" / "updater_e2e_check.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "assert Path(os.environ['WPT_RELEASE_DIR']) == Path(os.environ['EXPECTED_TREE'])\n"
            "assert Path(os.environ['WPT_RELEASE_ASSET_DIR']) == Path(os.environ['EXPECTED_ASSETS'])\n"
            "assert Path(os.environ['WPT_RELEASE_ASSET_DIR']).is_dir()\n"
            "scratch = Path(os.environ['TMPDIR'])\n"
            "(scratch / 'suite-scratch-marker').write_text('temporary')\n"
            "print(f'test scratch: {scratch}')\n"
            "print('external updater asset directory reached')\n",
            encoding="utf-8",
        )
        env = {key: value for key, value in os.environ.items() if not key.startswith("WPT_")}
        env.update(
            HOME=str(home),
            TMPDIR=str(tmp),
            WPT_RELEASE_DIR=str(tree),
            WPT_RELEASE_ASSET_DIR=str(assets),
            EXPECTED_TREE=str(tree),
            EXPECTED_ASSETS=str(assets),
        )
        result = subprocess.run(
            ["bash", str(ROOT / "packaging/run-suites.sh"), str(tree)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        log = log_from(result.stdout, "updater_e2e_check")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "SKIPPED:" not in result.stdout, result.stdout
        assert "suites run: 1, skipped: 0, non-zero exits: 0" in result.stdout, result.stdout
        assert log.is_file() and "external updater asset directory reached" in log.read_text(), (
            result.stdout + result.stderr
        )
        assert log.parent.parent == tmp, f"unexpected log root: {log}"
        assert_scratch_removed(log)

        default_assets = tree / "dist"
        populate_assets(default_assets)
        default_env = dict(env)
        default_env.pop("WPT_RELEASE_DIR")
        default_env.pop("WPT_RELEASE_ASSET_DIR")
        default_env["EXPECTED_TREE"] = str(tree)
        default_env["EXPECTED_ASSETS"] = str(default_assets)
        defaulted = subprocess.run(
            ["bash", str(ROOT / "packaging/run-suites.sh"), tree.name],
            cwd=root,
            env=default_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert defaulted.returncode == 0, defaulted.stdout + defaulted.stderr
        assert "SKIPPED:" not in defaulted.stdout, defaulted.stdout
        assert "suites run: 1, skipped: 0, non-zero exits: 0" in defaulted.stdout, defaulted.stdout
        default_log = log_from(defaulted.stdout, "updater_e2e_check")
        assert default_log != log, "separate runner invocations must not reuse a log path"
        assert "external updater asset directory reached" in default_log.read_text()
        assert_scratch_removed(default_log)

        relative_assets = root / "relative-built-assets"
        populate_assets(relative_assets)
        relative_env = dict(
            default_env,
            WPT_RELEASE_DIR=tree.name,
            WPT_RELEASE_ASSET_DIR=relative_assets.name,
            EXPECTED_TREE=str(tree),
            EXPECTED_ASSETS=str(relative_assets),
        )
        relative_override = subprocess.run(
            ["bash", str(ROOT / "packaging/run-suites.sh"), tree.name],
            cwd=root,
            env=relative_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert relative_override.returncode == 0, relative_override.stdout + relative_override.stderr
        assert "SKIPPED:" not in relative_override.stdout, relative_override.stdout
        assert "suites run: 1, skipped: 0, non-zero exits: 0" in relative_override.stdout
        assert_scratch_removed(log_from(relative_override.stdout, "updater_e2e_check"))

        concurrent = []
        for _ in range(2):
            concurrent.append(subprocess.Popen(
                ["bash", str(ROOT / "packaging/run-suites.sh"), tree.name],
                cwd=root,
                env=default_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ))
        try:
            concurrent_results = [proc.communicate(timeout=30) for proc in concurrent]
        except subprocess.TimeoutExpired:
            for proc in concurrent:
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            for proc in concurrent:
                proc.communicate()
            raise
        concurrent_logs = [log_from(out, "updater_e2e_check") for out, _ in concurrent_results]
        assert concurrent_logs[0] != concurrent_logs[1], "concurrent suite runs must have separate logs"
        for (out, err), path in zip(concurrent_results, concurrent_logs, strict=True):
            assert "suites run: 1, skipped: 0, non-zero exits: 0" in out, out + err
            assert "external updater asset directory reached" in path.read_text()
            assert_scratch_removed(path)

        bad_assets = root / "bad-built-assets"
        populate_assets(bad_assets)
        (bad_assets / RELEASE_ASSETS[1]).write_text(f"{'0' * 64}  {RELEASE_ASSETS[0]}\n", encoding="utf-8")
        bad_env = dict(env, WPT_RELEASE_ASSET_DIR=str(bad_assets))
        bad_checksum = subprocess.run(
            ["bash", str(ROOT / "packaging/run-suites.sh"), str(tree)],
            env=bad_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert bad_checksum.returncode != 0, bad_checksum.stdout + bad_checksum.stderr
        assert "FAILED: updater E2E asset checksums failed" in bad_checksum.stdout, bad_checksum.stdout
        assert "non-zero exits: 1" in bad_checksum.stdout, bad_checksum.stdout

        stale_assets = root / "stale-built-assets"
        stale_assets.mkdir()
        for name in RELEASE_ASSETS:
            old_name = name.replace("0.6.5", "0.6.4")
            (stale_assets / old_name).write_text("old release", encoding="utf-8")
        stale_env = dict(env, WPT_RELEASE_ASSET_DIR=str(stale_assets))
        stale = subprocess.run(
            ["bash", str(ROOT / "packaging/run-suites.sh"), str(tree)],
            env=stale_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert stale.returncode == 0, stale.stdout + stale.stderr
        assert "SKIPPED: current-version updater assets are missing" in stale.stdout, stale.stdout
        assert "suites run: 0, skipped: 1, non-zero exits: 0" in stale.stdout, stale.stdout

        required_env = dict(stale_env)
        required_env["WPT_RELEASE_DIR"] = str(tree)
        required_env["WPT_RELEASE_ASSET_DIR"] = str(root / "missing-assets")
        required_env["WPT_REQUIRE_UPDATER_E2E"] = "1"
        required = subprocess.run(
            ["bash", str(ROOT / "packaging/run-suites.sh"), str(tree)],
            env=required_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert required.returncode != 0, required.stdout + required.stderr
        assert "required updater E2E assets are missing" in required.stdout, required.stdout

        relative_logs = root / "relative-logs"
        relative_logs.mkdir()
        relative_env = dict(default_env, TMPDIR="relative-logs")
        relative = subprocess.run(
            ["bash", str(ROOT / "packaging/run-suites.sh"), tree.name],
            cwd=root,
            env=relative_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert relative.returncode == 0, relative.stdout + relative.stderr
        relative_log = log_from(relative.stdout, "updater_e2e_check")
        assert relative_log.is_file(), relative.stdout
        assert relative_log.parent.parent == relative_logs, f"relative TMPDIR resolved incorrectly: {relative_log}"
        assert_scratch_removed(relative_log)
        assert not (tree / "relative-logs").exists(), "relative TMPDIR was resolved after chdir"
    print("run-suites updater asset defaults, required mode and relative TMPDIR checks: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
