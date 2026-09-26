"""Installer discovery: what is downloaded but not installed yet.

A full plugin manager has to answer "what have I got lying around?" as well as
"what is installed?". This finds vendor installers in the usual dumping grounds
(Downloads, the prefix root), works out the product and version from the file
name, and says whether that version is already installed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import msi as msi_mod
from .environment import Environment

INSTALLER_SUFFIXES = (".exe", ".msi")
IGNORE_NAMES = {
    "wine-mono-9.0.0-x86.msi",
    "wine-gecko-2.47.4-x86.msi",
    "vcredist_x64.exe",
    "vcredist_x86.exe",
    "dxsetup.exe",
}
VERSION_RE = re.compile(r"[vV]?(\d+(?:\.\d+){1,})\s*$")
CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Words that are marketing noise in a file name rather than part of the product.
NOISE = ("Installer", "Setup", "Install", "Win64", "x64", "64bit")


@dataclass
class Installer:
    path: Path
    product: str
    version: str
    kind: str          # "msi" | "wrapper"
    size: int
    installed: str | None = None   # version already installed, if we can tell

    @property
    def status(self) -> str:
        if self.installed is None:
            return "not installed"
        if self.version and self.installed and self.version != self.installed:
            return f"upgrade available ({self.installed} -> {self.version})"
        return f"already installed ({self.installed})"


def product_and_version(stem: str) -> tuple[str, str]:
    """'ArchetypeRabeaXv1.1.0' -> ('Archetype Rabea X', '1.1.0').

    Vendor file names are camel-cased with the version glued on the end, so this
    is a best-effort human-readable guess, not a certainty -- the MSI's own
    ProductName/ProductVersion always wins when there is an MSI to read.
    """
    cleaned = stem.strip()
    version = ""
    match = VERSION_RE.search(cleaned)
    if match:
        version = match.group(1)
        cleaned = cleaned[: match.start()].rstrip("_- ")
    for word in NOISE:
        cleaned = cleaned.replace(word, "")
    # "ArchetypeRabeaX" -> "Archetype Rabea X"; underscores are separators too
    spaced = CAMEL_RE.sub(" ", cleaned).replace("_", " ")
    spaced = re.sub(r"\s{2,}", " ", spaced).strip("_- ")
    return spaced or stem, version


def _installed_versions(env: Environment) -> dict[str, str]:
    """Product name (lowercase) -> installed version, from the prefix's cached MSIs.

    The registry's Uninstall entries are the authoritative source, but the cached
    MSI carries the same ProductName/ProductVersion and is far cheaper to read
    with msitools than to parse out of system.reg.
    """
    versions: dict[str, str] = {}
    for msi in msi_mod.find_extracted_msis(env.prefix, include_installer_cache=True):
        try:
            ident = msi_mod.identity(msi)
        except Exception:  # noqa: BLE001
            continue
        if ident.product_name:
            versions[ident.product_name.lower()] = ident.product_version
    return versions


def _readable(path: Path) -> bool:
    """Whether a listed file can still be measured - downloads and temp files come and go."""
    try:
        path.stat()
        return True
    except OSError:
        return False


def discover(env: Environment, extra_dirs: list[Path] | None = None) -> list[Installer]:
    """Find installers in Downloads, the prefix root, and any extra directories."""
    search_dirs: list[Path] = [Path.home() / "Downloads", env.drive_c]
    if extra_dirs:
        search_dirs.extend(extra_dirs)

    known = _installed_versions(env)
    found: list[Installer] = []
    seen: set[Path] = set()

    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*")):
            if not path.is_file() or path in seen:
                continue
            if path.suffix.lower() not in INSTALLER_SUFFIXES:
                continue
            if path.name.lower() in IGNORE_NAMES:
                continue
            seen.add(path)
            product, version = product_and_version(path.stem)
            installed = None
            key = product.lower()
            if key in known:
                installed = known[key]
            else:
                # fall back to a loose match: "Archetype Rabea X" vs "archetype rabea"
                for name, ver in known.items():
                    if name.startswith(key) or key.startswith(name):
                        installed = ver
                        break
            found.append(
                Installer(
                    path=path,
                    product=product,
                    version=version,
                    kind="msi" if path.suffix.lower() == ".msi" else "wrapper",
                    size=path.stat().st_size if _readable(path) else 0,
                    installed=installed,
                )
            )
    return sorted(found, key=lambda i: (i.status == "not installed", i.product.lower()))


def msi_for(installer: Installer, env: Environment) -> Path | None:
    """The MSI that would install this download.

    A download that *is* an MSI is used directly. A wrapper (.exe) has to be run
    once under Wine before its payload MSI is left inside the prefix, so in that
    case look for a cached MSI whose product name matches the file name.
    """
    if installer.kind == "msi":
        return installer.path
    wanted = installer.product.lower()
    fallback: Path | None = None
    for msi in msi_mod.find_extracted_msis(env.prefix, include_installer_cache=True):
        try:
            ident = msi_mod.identity(msi)
        except Exception:  # noqa: BLE001 - a bad MSI must not hide the rest
            continue
        name = (ident.product_name or "").lower()
        if not name:
            continue
        if name == wanted:
            return msi
        if fallback is None and (name.startswith(wanted) or wanted.startswith(name)):
            fallback = msi
    return fallback


def match_download(product: str, candidates) -> Path | None:
    """The downloaded file that is *this* product, or nothing.

    Two chances, both strict: the product's own key appears inside the file's key
    ("archetyperabeax" in "archetyperabeaxv1110"), or every significant word of the product
    appears in the file name. A single shared word is not enough — "Cortex Control" and
    "Quad Cortex" must never resolve to "NeuralDSP Nano Cortex v5.74.0.exe".
    """
    product_key = _norm(product)
    tokens = [token for token in _words(product) if len(token) >= 4]
    if not product_key and not tokens:
        return None
    for path in candidates:
        name = _norm(path.stem)
        if product_key and product_key in name:
            return path
        if tokens and all(token in name for token in tokens):
            return path
    return None


def render(installers: list[Installer]) -> str:
    if not installers:
        return "no installers found in ~/Downloads or the prefix root"
    lines = []
    width = max(len(i.product) for i in installers)
    for item in installers:
        lines.append(
            f"  {item.product.ljust(width)}  {item.version or '?':<10} {item.kind:<8} {item.status}"
        )
        lines.append(f"      {item.path}")
    return "\n".join(lines)

def _norm(text: str) -> str:
    """Loose key for matching: "Archetype: Rabea X" -> "archetyperabeax"."""
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _words(text: str) -> list[str]:
    return [part for part in re.split(r"[^A-Za-z0-9]+", text.lower()) if part]
