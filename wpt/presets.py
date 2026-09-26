"""Presets: find them, and never let a removal take them with it.

The files a product's MSI ships are reproducible -- reinstall and the factory
presets, artist presets and default XML all come back. The ones that are *not*
reproducible are the two other kinds:

  * the user's own presets, saved into the product's `User` folder
  * downloaded preset packs (artist/vendor packs dropped into the vendor's
    ProgramData tree), which may not be downloadable again later

A plugin manager that deletes a product silently deletes both, so this module
collects them and copies them somewhere safe before anything is removed.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .environment import Environment

PRESET_SUFFIXES = (".xml", ".nesp", ".ndsp", ".preset", ".presets")
DEFAULT_RESCUE_ROOT = Path.home() / ".local" / "share" / "wpt" / "presets"
UNSAFE_NAME = re.compile(r"[^A-Za-z0-9 ._-]+")


@dataclass
class PresetSet:
    """What a product has, split by whether it can be reproduced."""

    product: str
    user: list[Path] = field(default_factory=list)
    downloaded: list[Path] = field(default_factory=list)

    @property
    def irreplaceable(self) -> list[Path]:
        return sorted({*self.user, *self.downloaded}, key=str)

    @property
    def total(self) -> int:
        return len(self.irreplaceable)


def _is_preset(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in PRESET_SUFFIXES


def _walk(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [path for path in root.rglob("*") if _is_preset(path)]


def product_dirs(env: Environment, vendor: str = "Neural DSP") -> list[str]:
    """Product names the vendor has a folder for inside this prefix.

    A folder that merely *contains* preset packs is not a product: Neural DSP nests
    their packs under a second `Neural DSP` folder, which would otherwise be listed
    as a product with no presets of its own.
    """
    root = env.program_data / vendor
    if not root.is_dir():
        return []
    pack_containers = {pack.parent for pack in pack_dirs(env, vendor)}
    names = []
    for child in root.iterdir():
        if not child.is_dir() or child in pack_containers:
            continue
        if child.name.strip().lower() == vendor.strip().lower():
            continue
        if any(_is_preset(p) for p in child.rglob("*")):
            names.append(child.name)
    return sorted(names)


def pack_dirs(env: Environment, vendor: str = "Neural DSP") -> list[Path]:
    """Downloaded preset packs: `*Preset*` folders under the vendor tree.

    They sit either directly under the vendor folder or one level deeper (Neural
    DSP nests theirs as `Neural DSP/NEURAL DSP/<Pack Name>`), so look at both.
    """
    root = env.program_data / vendor
    if not root.is_dir():
        return []
    packs: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if "preset" in child.name.lower():
            packs.append(child)
            continue
        packs.extend(
            grandchild
            for grandchild in sorted(child.iterdir())
            if grandchild.is_dir() and "preset" in grandchild.name.lower()
        )
    return packs


def collect(env: Environment, product: str, vendor: str = "Neural DSP") -> PresetSet:
    """User presets and downloaded packs for one product.

    `User` holds what the plugin saved; anything in a downloaded pack folder is kept
    too, because packs disappear from vendor sites.
    """
    found = PresetSet(product=product)
    product_root = env.program_data / vendor / product
    found.user = _walk(product_root / "User")

    for pack in pack_dirs(env, vendor):
        if pack == product_root:
            continue
        found.downloaded.extend(_walk(pack))

    # plugin settings and MIDI maps are small and also hand-made; keep them with the presets
    roaming = env.appdata_roaming / vendor / product
    for extra in ("MIDI",):
        found.downloaded.extend(p for p in _walk(roaming / extra) if p.suffix.lower() == ".xml")

    found.user = sorted(set(found.user), key=str)
    found.downloaded = sorted(set(found.downloaded), key=str)
    return found


def rescue(
    env: Environment,
    product: str,
    vendor: str = "Neural DSP",
    rescue_root: Path | None = None,
    dry_run: bool = False,
    stamp: str | None = None,
) -> tuple[list[tuple[str, str, str]], Path]:
    """Copy every irreplaceable preset for a product somewhere safe.

    Returns (rows, destination). Files are copied, never moved, and never overwrite
    an existing rescue copy, so running an uninstall twice cannot lose the earlier
    state of a preset that was changed in between.
    """
    root = Path(rescue_root) if rescue_root else DEFAULT_RESCUE_ROOT
    safe_product = UNSAFE_NAME.sub("_", product).strip() or "product"
    destination = root / safe_product / (stamp or time.strftime("%Y-%m-%d-%H%M"))

    presets = collect(env, product, vendor)
    rows: list[tuple[str, str, str]] = []
    if not presets.total:
        return rows, destination

    for source in presets.irreplaceable:
        try:
            relative = source.relative_to(env.program_data)
        except ValueError:
            relative = Path(source.parent.name) / source.name
        target = destination / relative
        if dry_run:
            rows.append(("dry-run", str(source), f"would be copied to {target}"))
            continue
        if target.exists() and target.stat().st_size == source.stat().st_size:
            rows.append(("kept", str(source), "already rescued"))
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            rows.append(("saved", str(source), f"-> {target}"))
        except OSError as exc:
            rows.append(("failed", str(source), str(exc)))
    return rows, destination


def render(env: Environment, vendor: str = "Neural DSP", rescue_root: Path | None = None) -> str:
    """What presets exist here, and what has been rescued already."""
    root = Path(rescue_root) if rescue_root else DEFAULT_RESCUE_ROOT
    lines: list[str] = []
    products = product_dirs(env, vendor)
    lines.append(f"presets under {env.program_data / vendor} ({len(products)} product folder(s))")
    lines.append("-" * 60)
    if not products:
        lines.append("  none: no product folder with preset files in this prefix")

    for product in products:
        presets = collect(env, product, vendor)
        lines.append(
            f"  {product:26} {len(presets.user):4} yours   {len(presets.downloaded):4} downloaded/pack"
        )
        for path in presets.user[:3]:
            lines.append(f"        yours: {path.name}")

    vendor_root = env.program_data / vendor
    packs = pack_dirs(env, vendor)
    if packs:
        lines.append("")
        lines.append("downloaded packs:")
        for pack in packs:
            try:
                where = pack.relative_to(vendor_root)
            except ValueError:
                where = Path(pack.name)
            lines.append(f"  {pack.name:30} {sum(1 for f in pack.rglob('*') if f.is_file()):4} file(s)  ({vendor}/{where})")

    lines.append("")
    lines.append(f"rescued copies: {root}")
    lines.append("-" * 60)
    if not root.is_dir():
        lines.append("  nothing rescued yet")
    else:
        for product_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            stamps = sorted((s for s in product_dir.iterdir() if s.is_dir()), reverse=True)
            for stamp in stamps[:3]:
                count = sum(1 for f in stamp.rglob("*") if f.is_file())
                lines.append(f"  {product_dir.name:26} {stamp.name}  {count} file(s)")
    lines.append("")
    lines.append("Factory and artist presets are not rescued: they come back with a reinstall.")
    lines.append("Yours and downloaded packs are, every time a product is removed.")
    return "\n".join(lines)


def export(env: Environment, destination: Path, vendor: str = "Neural DSP") -> list[tuple[str, str, str]]:
    """Copy every product's user presets and packs out to a plain directory."""
    rows: list[tuple[str, str, str]] = []
    destination = Path(destination)
    for product in product_dirs(env, vendor):
        presets = collect(env, product, vendor)
        target_root = destination / UNSAFE_NAME.sub("_", product).strip()
        for source in presets.irreplaceable:
            target = target_root / source.name
            if target.exists():
                rows.append(("kept", str(source), "already exported"))
                continue
            try:
                target_root.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            except OSError as exc:
                # one unwritable file (permissions, a full disk) must not abandon the whole export
                rows.append(("failed", str(source), str(exc)))
                continue
            rows.append(("exported", str(source), f"-> {target}"))
    return rows
