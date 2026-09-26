"""Check GitHub for a newer release, and hand the install to pacman.

Deliberately small and honest:

* it asks GitHub's public API for the newest release tag (no account, no identifiers, one GET),
* it compares that tag with the version of the code that is running,
* it downloads the release's Arch package, checks the file is really a pacman package and, when the
  release publishes a checksum, that the bytes are the published ones,
* and it does *not* install anything itself. Installing needs root, and a GUI that silently
  escalates is a bad thing to ship: instead it opens a terminal with the `sudo pacman -U` line
  already typed, so the user sees exactly what runs and pacman asks for the password.

If the machine is not Arch-family, it says so and points at the source tarball instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__

# Override for forks and for the tests: WPT_REPO=owner/name
REPO = os.environ.get("WPT_REPO", "Absolute-Projects-Public/wine-plugin-toolkit")
API = "https://api.github.com"
TIMEOUT = 10
CACHE_TTL = 24 * 60 * 60          # one check a day is plenty for a plugin installer
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
GZIP_MAGIC = b"\x1f\x8b"


class UpdateError(Exception):
    """Anything that stopped us finding out whether there is an update."""


@dataclass
class Asset:
    name: str
    url: str
    size: int = 0

    @property
    def is_package(self) -> bool:
        return self.name.endswith(".pkg.tar.zst")

    @property
    def is_source(self) -> bool:
        return self.name.endswith(".tar.gz")

    @property
    def is_checksum(self) -> bool:
        """A checksum file, however it is named: `x.sha256`, `sha256sums.txt`, `SHA256SUMS`."""
        name = self.name.lower()
        if name.endswith((".pkg.tar.zst", ".tar.gz")):
            return False
        return "sha256" in name or name.endswith(".sums")


@dataclass
class Release:
    tag: str
    version: tuple[int, ...]
    html_url: str
    published_at: str = ""
    body: str = ""
    assets: list[Asset] = field(default_factory=list)

    @property
    def version_text(self) -> str:
        return ".".join(str(part) for part in self.version)

    def asset(self, *, package: bool) -> Asset | None:
        """The Arch package asset, or the source tarball when package=False."""
        for candidate in self.assets:
            if package and candidate.is_package:
                return candidate
            if not package and candidate.is_source:
                return candidate
        return None

    def checksum_for(self, asset: Asset) -> Asset | None:
        """The release asset that would carry this asset's checksum.

        Accepts both layouts: a per-file `<asset name>.sha256`, or one shared `sha256sums.txt`.
        The shared file is preferred only if nothing names the asset specifically, and
        `expected_sha256()` then picks the line for the right file.
        """
        stem = asset.name.removesuffix(".pkg.tar.zst").removesuffix(".tar.gz")
        checksums = [a for a in self.assets if a.is_checksum]
        for candidate in checksums:
            if stem and stem in candidate.name:
                return candidate
        return checksums[0] if checksums else None


def parse_version(text: str) -> tuple[int, ...]:
    """`v0.5.10` / `0.5.10` -> (0, 5, 10).

    Only the leading dotted number sequence counts: a packaging suffix (`0.5.9-1`) is not part of
    the upstream version, so a re-packaged release is not mistaken for a new upstream version.
    """
    match = re.match(r"\s*v?(\d+(?:\.\d+)*)", text or "")
    if not match:
        return (0,)
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(candidate: tuple[int, ...], current: tuple[int, ...]) -> bool:
    """Compare version tuples of unequal length, padding with zeros."""
    length = max(len(candidate), len(current))
    left = candidate + (0,) * (length - len(candidate))
    right = current + (0,) * (length - len(candidate))
    return left > right


def _get(url: str, timeout: int = TIMEOUT) -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": f"wpt/{__version__}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("no published release yet (GitHub has nothing for this repository)")
        if exc.code in (403, 429):
            raise UpdateError("GitHub is rate-limiting this machine right now - try again later")
        raise UpdateError(f"GitHub answered {exc.code} {exc.reason}")
    except urllib.error.URLError as exc:
        raise UpdateError(f"cannot reach GitHub ({exc.reason})")
    except (TimeoutError, OSError) as exc:
        raise UpdateError(f"cannot reach GitHub ({exc})")


def latest_release(timeout: int = TIMEOUT) -> Release:
    """The newest published release, or UpdateError if it cannot be determined."""
    payload = _get(f"{API}/repos/{REPO}/releases/latest", timeout=timeout)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise UpdateError("GitHub sent something that is not JSON")
    tag = data.get("tag_name") or ""
    if not tag:
        raise UpdateError("the newest release has no tag")
    return Release(
        tag=tag,
        version=parse_version(tag),
        html_url=data.get("html_url") or f"https://github.com/{REPO}/releases",
        published_at=(data.get("published_at") or "")[:10],
        body=data.get("body") or "",
        assets=[Asset(name=a.get("name", ""), url=a.get("browser_download_url", ""),
                      size=int(a.get("size") or 0))
                for a in data.get("assets", [])],
    )


# ----------------------------------------------------------------------------- cache
def cache_file(home: Path | None = None) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", (home or Path.home()) / ".cache"))
    return base / "wpt" / "update-check.json"


def read_cache(home: Path | None = None) -> dict | None:
    path = cache_file(home)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - float(data.get("checked_at") or 0) > CACHE_TTL:
        return None
    return data


def write_cache(release: Release | None, error: str | None = None, home: Path | None = None) -> None:
    path = cache_file(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "checked_at": time.time(),
            "tag": release.tag if release else None,
            "version": list(release.version) if release else None,
            "html_url": release.html_url if release else None,
            "error": error,
        }, indent=1))
    except OSError:
        pass          # a cache we cannot write is not worth failing over


# ----------------------------------------------------------------------------- download
def download(asset: Asset, dest_dir: Path, progress=None) -> Path:
    """Fetch one release asset, with a size sanity check while it streams."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / asset.name
    part = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(asset.url, headers={"User-Agent": f"wpt/{__version__}"})
    written = 0
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT * 6) as response, \
                open(part, "wb") as handle:
            total = int(response.headers.get("Content-Length") or asset.size or 0)
            while True:
                chunk = response.read(262144)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
                if progress:
                    progress(written, total)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        part.unlink(missing_ok=True)
        raise UpdateError(f"download failed ({exc})")
    if asset.size and written != asset.size:
        part.unlink(missing_ok=True)
        raise UpdateError(f"downloaded {written} bytes but the release lists {asset.size}")
    part.replace(target)
    return target


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def expected_sha256(release: Release, asset: Asset, dest_dir: Path) -> str | None:
    """The published checksum for an asset, or None when the release does not publish one.

    Handles both a bare hash file and the `sha256sum` format, where the line for this file is
    identified by its name — a shared sums file must not hand back another file's hash.
    """
    published = release.checksum_for(asset)
    if not published:
        return None
    try:
        text = _get(published.url).decode("utf-8", "replace")
    except UpdateError:
        return None
    for line in text.splitlines():
        match = re.match(r"\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if match and Path(match.group(2)).name == asset.name:
            return match.group(1).lower()
    match = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    return match.group(1).lower() if match else None


def validate_package(path: Path) -> None:
    """A pacman package is a zstd stream starting with the zstd magic number."""
    with open(path, "rb") as handle:
        head = handle.read(4)
    if not head.startswith(ZSTD_MAGIC):
        raise UpdateError(f"{path.name} is not a zstd archive - refusing to hand it to pacman")


def validate_source(path: Path) -> None:
    with open(path, "rb") as handle:
        head = handle.read(2)
    if not head.startswith(GZIP_MAGIC):
        raise UpdateError(f"{path.name} is not a gzip archive")


# ----------------------------------------------------------------------------- installing
ARCH_DISTROS = ("arch", "cachyos", "endeavouros", "manjaro", "garuda", "artix", "arcolinux")

TERMINALS = (
    ("kitty", ["kitty", "-e", "bash", "-c"]),
    ("alacritty", ["alacritty", "-e", "bash", "-c"]),
    ("foot", ["foot", "bash", "-c"]),
    ("wezterm", ["wezterm", "start", "--", "bash", "-c"]),
    ("konsole", ["konsole", "-e", "bash", "-c"]),
    ("xfce4-terminal", ["xfce4-terminal", "--command", "bash -c %s"]),
    ("gnome-terminal", ["gnome-terminal", "--", "bash", "-c"]),
    ("xterm", ["xterm", "-e", "bash", "-c"]),
)


def is_arch_family() -> bool:
    try:
        text = Path("/etc/os-release").read_text().lower()
    except OSError:
        return False
    ident = ""
    for line in text.splitlines():
        if line.startswith("id="):
            ident = line.partition("=")[2].strip().strip('"')
    return ident in ARCH_DISTROS


def find_terminal() -> list[str] | None:
    """A terminal that can run a command string, preferring $TERMINAL."""
    override = os.environ.get("TERMINAL")
    if override:
        binary = shutil.which(override)
        if binary:
            return [binary, "-e", "bash", "-c"]
    for name, argv in TERMINALS:
        binary = shutil.which(name)
        if binary:
            return [binary] + argv[1:]
    return None


def install_script(package: Path, restart: bool = True, gui_command: str = "wpt-gui") -> str:
    """The transcript the user will watch in their terminal."""
    lines = [
        "#!/bin/bash",
        "# wpt update: install the release you just downloaded, then bring the app back.",
        "set -u",
        f'echo "==> installing {package.name}"',
        f'sudo pacman -U --noconfirm "{package}"',
        "rc=$?",
        'if [ "$rc" -ne 0 ]; then',
        '    echo',
        'echo "==> pacman exited $rc - nothing else was changed. Press Enter to close."',
        'echo "    (sudo asking for a password is the usual reason; nothing was half-installed)"',
        "    read -r _",
        "    exit $rc",
        "fi",
        "echo",
        "echo '==> updated. Verifying...'",
        f'"$(command -v wpt || echo /usr/bin/wpt)" --version',
    ]
    if restart:
        lines += [
            "echo '==> reopening the toolkit'",
            f"( setsid {gui_command} >/dev/null 2>&1 & ) || true",
        ]
    lines += ["sleep 1", "exit 0"]
    return "\n".join(lines) + "\n"


def launch_install(package: Path, *, restart: bool = True) -> tuple[bool, str]:
    """Open a terminal running the installer. Returns (launched, what to tell the user)."""
    terminal = find_terminal()
    if terminal is None:
        return False, ("no terminal emulator found. Run this yourself: "
                       f"sudo pacman -U {package}")
    workdir = package.parent
    script = workdir / f"wpt-update-{package.name.removesuffix('.pkg.tar.zst')}.sh"
    script.write_text(install_script(package, restart=restart))
    script.chmod(0o755)
    try:
        subprocess.Popen(terminal + [str(script)], cwd=str(workdir),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as exc:
        return False, f"could not start {terminal[0]} ({exc}). Run: sudo pacman -U {package}"
    return True, f"opened {Path(terminal[0]).name} to install it"


# ----------------------------------------------------------------------------- reporting
def status_lines(release: Release | None, error: str | None, package: bool) -> list[str]:
    current = parse_version(__version__)
    if error:
        return [f"update check: {error}"]
    if release is None:
        return ["update check: no release information"]
    if not is_newer(release.version, current):
        return [f"wpt {__version__} is the newest release ({release.tag})"]
    lines = [f"wpt {__version__} -> {release.version_text} is available ({release.tag}, "
             f"published {release.published_at})"]
    if package:
        asset = release.asset(package=True)
        if asset:
            lines.append(f"  Arch package: {asset.name} ({asset.size / 1048576:.1f} MB)")
        else:
            lines.append("  this release publishes no Arch package - install from the source tarball")
    else:
        lines.append("  non-Arch system: install from the source tarball at " + release.html_url)
    return lines


def disable_hint() -> str:
    return "set WPT_NO_UPDATE_CHECK=1 to stop the startup check"


# ----------------------------------------------------------------------------- user settings
def config_path(home: Path | None = None) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", (home or Path.home()) / ".config"))
    return base / "wpt" / "config.json"


def load_config(home: Path | None = None) -> dict:
    try:
        data = json.loads(config_path(home).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(values: dict, home: Path | None = None) -> None:
    path = config_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        merged = load_config(home)
        merged.update(values)
        path.write_text(json.dumps(merged, indent=1))
    except OSError:
        pass


def update_check_enabled(home: Path | None = None) -> bool:
    """Off when the environment says so, or when the user turned it off in the app."""
    if os.environ.get("WPT_NO_UPDATE_CHECK"):
        return False
    return load_config(home).get("update_check", True) is not False


def skipped_version(home: Path | None = None) -> str:
    return str(load_config(home).get("skip_version") or "")
