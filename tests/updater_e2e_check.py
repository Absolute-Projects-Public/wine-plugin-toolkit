"""End-to-end check of the updater against the artefacts we are about to publish.

GitHub cannot serve the new release before it is published, so this stands a GitHub-API-shaped stub
on localhost, serves four release assets from WPT_RELEASE_ASSET_DIR (default: WPT_RELEASE_DIR/dist),
and then runs the *real* updater code -
latest_release, is_newer, asset selection, download, the zstd check, the per-asset checksum - plus
the real `wpt update --install show` command. Nothing is installed; that is the one step that needs
a password.

The two versions it is about are read from the tree: the release being cut is the newest heading in
CHANGELOG.md, and the release before it is the one after that. Pinned literals meant editing this
gate on every release, and quietly checking the wrong pair when nobody did.

    WPT_RELEASE_DIR=~/wpt-release WPT_RELEASE_ASSET_DIR=~/wpt-pkg \
        python3 tests/updater_e2e_check.py
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

release_dir = Path(os.environ.get("WPT_RELEASE_DIR", "~/wpt-release")).expanduser()
dist = Path(os.environ.get("WPT_RELEASE_ASSET_DIR", str(release_dir / "dist"))).expanduser()
sys.path.insert(0, str(release_dir))

from wpt import updates as updates_mod  # noqa: E402

checks = 0
failures: list[str] = []


def check(name: str, got, want) -> None:
    global checks
    checks += 1
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if got == want else 'FAIL'}  {name}")


_headings = re.findall(r"^## (\d+\.\d+\.\d+)", (release_dir / "CHANGELOG.md").read_text(), re.M)
if len(_headings) < 2:
    print(f"CHANGELOG.md in {release_dir} does not name two released versions")
    sys.exit(2)
NEW, PREV = _headings[0], _headings[1]
NEW_TUPLE = tuple(int(part) for part in NEW.split("."))
PREV_TUPLE = tuple(int(part) for part in PREV.split("."))
TARBALL = f"wpt-{NEW}.tar.gz"
_pkgbuild = (release_dir / "PKGBUILD").read_text()
_pkgver_match = re.search(r"(?m)^pkgver=(\S+)$", _pkgbuild)
_pkgrel_match = re.search(r"(?m)^pkgrel=(\d+)$", _pkgbuild)
if not _pkgver_match or _pkgver_match.group(1) != NEW or not _pkgrel_match:
    print(f"PKGBUILD in {release_dir} does not match changelog version {NEW} with a package release")
    sys.exit(2)
PACKAGE = f"wine-plugin-toolkit-{NEW}-{_pkgrel_match.group(1)}-any.pkg.tar.zst"

ASSETS = [
    TARBALL,
    f"{TARBALL}.sha256",
    PACKAGE,
    f"{PACKAGE}.sha256",
]
missing = [name for name in ASSETS if not (dist / name).is_file()]
if missing:
    print(f"missing artefacts in {dist} for {NEW}: {missing}")
    sys.exit(2)

work = Path(tempfile.mkdtemp(prefix="wpt-update-e2e-"))
asset_server_dir = work / "assets"
try:
    asset_server_dir.mkdir()
    for name in ASSETS:
        shutil.copy2(dist / name, asset_server_dir / name)
except BaseException:
    shutil.rmtree(work, ignore_errors=True)
    raise


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serve a scratch copy of the four release assets and a GitHub-shaped response."""

    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(asset_server_dir), **k)

    def log_message(self, *_a):        # keep the output readable
        pass

    def do_GET(self):                  # noqa: N802 - http.server's interface
        if self.path.endswith("/releases/latest"):
            port = self.server.server_address[1]
            body = json.dumps({
                "tag_name": f"v{NEW}",
                "html_url": f"http://127.0.0.1:{port}/release-notes",
                "published_at": "2026-09-27T00:00:00Z",
                "body": "stub",
                "assets": [
                    {"name": name, "size": (asset_server_dir / name).stat().st_size,
                     "browser_download_url": f"http://127.0.0.1:{port}/{name}"}
                    for name in ASSETS
                ],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


server = None
server_thread = None
try:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
except BaseException:
    if server is not None and server_thread is not None and server_thread.is_alive():
        server.shutdown()
        server_thread.join(timeout=5)
    if server is not None:
        server.server_close()
    shutil.rmtree(work, ignore_errors=True)
    raise
assert server is not None and server_thread is not None
print(f"stub release host on 127.0.0.1:{port}, isolated assets from {asset_server_dir}")
print(f"cutting {NEW}, upgrading from {PREV}")

updates_mod.API = f"http://127.0.0.1:{port}"
updates_mod.REPO = "stub/repo"

try:
    print("\n1. reading the release, as the updater does")
    tree_version = re.search(
        r'__version__ = "([^"]+)"', (release_dir / "wpt/__init__.py").read_text()
    ).group(1)
    check("the changelog's newest version is the tree's version", tree_version, NEW)
    release = updates_mod.latest_release()
    check("tag", release.tag, f"v{NEW}")
    check("parsed version", release.version, NEW_TUPLE)
    check("four assets seen", len(release.assets), 4)

    print("\n2. the comparison that decides whether anyone is offered it")
    check(f"a machine on the published {PREV} is offered it",
          updates_mod.is_newer(release.version, PREV_TUPLE), True)
    check("a machine on 0.5.8 is offered it",
          updates_mod.is_newer(release.version, (0, 5, 8)), True)
    check(f"a machine already on {NEW} is not (documented, see the report)",
          updates_mod.is_newer(release.version, NEW_TUPLE), False)

    print("\n3. picking the right asset and downloading it, through the real code")
    asset = release.asset(package=True)
    check("the package is selected, not the source tarball", asset.name, PACKAGE)
    fetched = updates_mod.download(asset, work)
    check("downloaded", fetched.is_file(), True)
    check("byte-for-byte what we built",
          updates_mod.sha256(fetched), hashlib.sha256((asset_server_dir / asset.name).read_bytes()).hexdigest())

    print("\n4. the checks the updater runs before it hands a file to pacman")
    checksum_asset = release.checksum_for(asset)
    check("it finds our per-asset checksum file", checksum_asset.name, f"{PACKAGE}.sha256")
    want = updates_mod.expected_sha256(release, asset, work)
    got = updates_mod.sha256(fetched)
    check("the published checksum is read and matches", want, got)
    updates_mod.validate_package(fetched)
    print("  ok    the package passes the zstd magic check")

    print("\n5. the 0.6.1 regression: a sums file naming only the tarball must not verify the package")
    sums = asset_server_dir / "sha256sums.txt"
    sums.write_text(f"{hashlib.sha256((asset_server_dir / TARBALL).read_bytes()).hexdigest()}  {TARBALL}\n")
    try:
        lone = updates_mod.Release(tag=f"v{NEW}", version=NEW_TUPLE, html_url="",
                                   assets=[updates_mod.Asset(name="sha256sums.txt",
                                                             url=f"http://127.0.0.1:{port}/sha256sums.txt",
                                                             size=sums.stat().st_size), asset])
        check("no checksum is claimed for the package", updates_mod.expected_sha256(lone, asset, work), None)
    finally:
        sums.unlink()

    print("\n6. the real command a user runs: wpt update --install show")
    home = work / "home"
    home.mkdir()
    env = dict(os.environ, HOME=str(home), XDG_CACHE_HOME=str(home / ".cache"),
               XDG_CONFIG_HOME=str(home / ".config"), TMPDIR=str(work),
               PYTHONPATH=str(release_dir))

    def run_cli(running_version: str) -> tuple[int, str]:
        """Run the real CLI with the running version it should believe it has.

        `running_version` is patched rather than the release, because the release is the thing under
        test: a user on the published release is the case that has to work.
        """
        arch_override = (
            "updates.is_arch_family = lambda: True;"
            if os.environ.get("WPT_UPDATER_E2E_ASSUME_ARCH") == "1"
            else ""
        )
        wrapper = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from wpt import updates;"
            f"updates.API = {updates_mod.API!r}; updates.REPO = 'stub/repo';"
            f"{arch_override}"
            f"updates.__version__ = {running_version!r};"
            "from wpt import cli; sys.argv = ['wpt','update','--install','show'];"
            "raise SystemExit(cli.main())"
        )
        proc = subprocess.run([sys.executable, "-c", wrapper, str(release_dir)],
                              capture_output=True, text=True, env=env, timeout=600)
        return proc.returncode, proc.stdout + proc.stderr

    rc, out = run_cli(PREV)
    print(f"    as a machine on the published {PREV}:")
    print("      " + "\n      ".join(line for line in out.strip().splitlines() if line.strip()))
    check("the command succeeded", rc, 0)
    check("it downloaded the package", f"downloading {PACKAGE}" in out, True)
    check("it verified the checksum", "matches the release" in out, True)
    check("it printed the command that installs it", "sudo pacman -U" in out, True)

    rc, out = run_cli(NEW)
    print(f"    as a machine already on {NEW}:")
    print("      " + "\n      ".join(line for line in out.strip().splitlines() if line.strip()))
    check("it says up to date and downloads nothing", "is the newest release" in out, True)
    check("and no download was attempted", "downloading" in out, False)
finally:
    server.shutdown()
    server.server_close()
    server_thread.join(timeout=5)
    shutil.rmtree(work, ignore_errors=True)

print(f"\nupdater end-to-end: {checks - len(failures)}/{checks} passed")
if failures:
    for line in failures:
        print("  FAILED:", line)
    sys.exit(1)
