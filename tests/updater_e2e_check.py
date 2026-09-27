"""End-to-end check of the updater against the artefacts we are about to publish.

GitHub only reports v0.6.2 (the new release does not exist yet), so `wpt update` cannot fetch
0.6.3 for real until you publish it. This stands a GitHub-API-shaped stub on localhost, serves the
four release assets from dist/, and then runs the *real* updater code - latest_release, is_newer,
asset selection, download, the zstd check, the per-asset checksum - plus the real `wpt update
--install show` command. Nothing is installed; that is the one step that needs a password.

    WPT_RELEASE_DIR=~/wpt-release python3 tests/updater_e2e_check.py
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

release_dir = Path(os.environ.get("WPT_RELEASE_DIR", "~/wpt-release")).expanduser()
dist = release_dir / "dist"
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


ASSETS = [
    "wpt-0.6.3.tar.gz",
    "wpt-0.6.3.tar.gz.sha256",
    "wine-plugin-toolkit-0.6.3-1-any.pkg.tar.zst",
    "wine-plugin-toolkit-0.6.3-1-any.pkg.tar.zst.sha256",
]
missing = [name for name in ASSETS if not (dist / name).is_file()]
if missing:
    print(f"missing artefacts in {dist}: {missing}")
    sys.exit(2)


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves dist/ as the asset host and a GitHub-shaped latest-release document."""

    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(dist), **k)

    def log_message(self, *_a):        # keep the output readable
        pass

    def do_GET(self):                  # noqa: N802 - http.server's interface
        if self.path.endswith("/releases/latest"):
            port = self.server.server_address[1]
            body = json.dumps({
                "tag_name": "v0.6.3",
                "html_url": f"http://127.0.0.1:{port}/release-notes",
                "published_at": "2026-09-27T00:00:00Z",
                "body": "stub",
                "assets": [
                    {"name": name, "size": (dist / name).stat().st_size,
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


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
print(f"stub release host on 127.0.0.1:{port}, assets from {dist}")

updates_mod.API = f"http://127.0.0.1:{port}"
updates_mod.REPO = "stub/repo"
work = Path(tempfile.mkdtemp(prefix="wpt-update-e2e-"))

try:
    print("\n1. reading the release, as the updater does")
    release = updates_mod.latest_release()
    check("tag", release.tag, "v0.6.3")
    check("parsed version", release.version, (0, 6, 3))
    check("four assets seen", len(release.assets), 4)

    print("\n2. the comparison that decides whether anyone is offered it")
    check("a machine on the published 0.6.2 is offered it",
          updates_mod.is_newer(release.version, (0, 6, 2)), True)
    check("a machine on 0.5.8 is offered it",
          updates_mod.is_newer(release.version, (0, 5, 8)), True)
    check("a machine already on 0.6.3 is not (documented, see the report)",
          updates_mod.is_newer(release.version, (0, 6, 3)), False)

    print("\n3. picking the right asset and downloading it, through the real code")
    asset = release.asset(package=True)
    check("the package is selected, not the source tarball", asset.name,
          "wine-plugin-toolkit-0.6.3-1-any.pkg.tar.zst")
    fetched = updates_mod.download(asset, work)
    check("downloaded", fetched.is_file(), True)
    check("byte-for-byte what we built",
          updates_mod.sha256(fetched), hashlib.sha256((dist / asset.name).read_bytes()).hexdigest())

    print("\n4. the checks the updater runs before it hands a file to pacman")
    checksum_asset = release.checksum_for(asset)
    check("it finds our per-asset checksum file", checksum_asset.name,
          "wine-plugin-toolkit-0.6.3-1-any.pkg.tar.zst.sha256")
    want = updates_mod.expected_sha256(release, asset, work)
    got = updates_mod.sha256(fetched)
    check("the published checksum is read and matches", want, got)
    updates_mod.validate_package(fetched)
    print("  ok    the package passes the zstd magic check")

    print("\n5. the 0.6.1 regression: a sums file naming only the tarball must not verify the package")
    sums = dist / "sha256sums.txt"
    sums.write_text(f"{hashlib.sha256((dist / 'wpt-0.6.3.tar.gz').read_bytes()).hexdigest()}  wpt-0.6.3.tar.gz\n")
    try:
        lone = updates_mod.Release(tag="v0.6.3", version=(0, 6, 3), html_url="",
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
               XDG_CONFIG_HOME=str(home / ".config"), PYTHONPATH=str(release_dir))

    def run_cli(running_version: str) -> tuple[int, str]:
        """Run the real CLI with the running version it should believe it has.

        `running_version` is patched rather than the release, because the release is the thing under
        test: a user on the published 0.6.2 is the case that has to work.
        """
        wrapper = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from wpt import updates;"
            f"updates.API = {updates_mod.API!r}; updates.REPO = 'stub/repo';"
            f"updates.__version__ = {running_version!r};"
            "from wpt import cli; sys.argv = ['wpt','update','--install','show'];"
            "raise SystemExit(cli.main())"
        )
        proc = subprocess.run([sys.executable, "-c", wrapper, str(release_dir)],
                              capture_output=True, text=True, env=env, timeout=600)
        return proc.returncode, proc.stdout + proc.stderr

    rc, out = run_cli("0.6.2")
    print("    as a machine on the published 0.6.2:")
    print("      " + "\n      ".join(line for line in out.strip().splitlines() if line.strip()))
    check("the command succeeded", rc, 0)
    check("it downloaded the package", "downloading wine-plugin-toolkit-0.6.3-1-any.pkg.tar.zst" in out, True)
    check("it verified the checksum", "matches the release" in out, True)
    check("it printed the command that installs it", "sudo pacman -U" in out, True)

    rc, out = run_cli("0.6.3")
    print("    as a machine already on 0.6.3:")
    print("      " + "\n      ".join(line for line in out.strip().splitlines() if line.strip()))
    check("it says up to date and downloads nothing", "is the newest release" in out, True)
    check("and no download was attempted", "downloading" in out, False)
finally:
    server.shutdown()
    shutil.rmtree(work, ignore_errors=True)

print(f"\nupdater end-to-end: {checks - len(failures)}/{checks} passed")
if failures:
    for line in failures:
        print("  FAILED:", line)
    sys.exit(1)
