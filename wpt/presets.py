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

import os
import re
import shutil
import stat
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


def _safe_path_component(value: str) -> bool:
    """Reject MSI/user labels that could reset or escape a prefix subdirectory path."""
    if not isinstance(value, str):
        return False
    name = value.strip()
    return bool(name) and name not in {".", ".."} and not any(
        separator in name for separator in ("/", "\\", "\x00")
    )


def _rescue_target(env: Environment, destination: Path, source: Path) -> Path:
    """Where one preset goes inside a rescue directory, keeping the ProgramData layout."""
    try:
        relative = source.relative_to(env.program_data)
    except ValueError:
        relative = Path(source.parent.name) / source.name
    return destination / relative


def _save_one(source: Path, target: Path, dry_run: bool) -> tuple[str, str, str]:
    """Copy one preset and report it, in the four shapes `rescue` has always reported.

    Shared so that the files found by `rescue_for_plan` beyond `collect()`'s set are copied and
    reported exactly like the rest, rather than through a second implementation that can drift.
    """
    if source.is_symlink():
        return ("failed", str(source), "refusing to rescue a symlink target")
    if dry_run:
        return ("dry-run", str(source), f"would be copied to {target}")
    created: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        index = 0
        while True:
            candidate = target if index == 0 else target.with_name(f"{target.stem}.{index}{target.suffix}")
            if candidate.exists():
                if _same_bytes(source, candidate):
                    return ("kept", str(source), f"already rescued at {candidate}")
                index += 1
                continue
            try:
                # Exclusive creation: never overwrite a prior snapshot, including another copy
                # made in the same minute with identical size but different content.
                with source.open("rb") as original, candidate.open("xb") as copy:
                    created = candidate
                    shutil.copyfileobj(original, copy)
            except FileExistsError:
                index += 1
                continue
            shutil.copystat(source, candidate)
            return ("saved", str(source), f"-> {candidate}")
    except OSError as exc:
        if created is not None:
            created.unlink(missing_ok=True)  # no partial rescue is mistaken for a good snapshot
        return ("failed", str(source), str(exc))


def _same_bytes(left: Path, right: Path) -> bool:
    """Content equality, not size equality: presets can change without changing byte count."""
    with left.open("rb") as first, right.open("rb") as second:
        while True:
            a, b = first.read(1024 * 1024), second.read(1024 * 1024)
            if a != b:
                return False
            if not a:
                return True


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
    if not _safe_path_component(product) or not _safe_path_component(vendor):
        return [("failed", f"{vendor}/{product}", "unsafe preset vendor/product path component")], destination
    try:
        prefix_root = Path(env.prefix).resolve()
        product_root = (env.program_data / vendor / product).resolve()
    except OSError as exc:
        return [("failed", f"{vendor}/{product}", f"cannot resolve preset source root: {exc}")], destination
    if not product_root.is_relative_to(prefix_root):
        return [("failed", str(product_root), "preset source root resolves outside the Wine prefix")], destination

    presets = collect(env, product, vendor)
    rows: list[tuple[str, str, str]] = []
    if not presets.total:
        return rows, destination

    for source in presets.irreplaceable:
        try:
            source_resolved = source.resolve()
            if not source_resolved.is_relative_to(prefix_root):
                rows.append(("failed", str(source), "preset source resolves outside the Wine prefix"))
                continue
            rows.append(_save_one(source, _rescue_target(env, destination, source), dry_run))
        except OSError as exc:
            rows.append(("failed", str(source), f"cannot verify preset source: {exc}"))
    return rows, destination


def products_under(env: Environment, paths, vendor: str = "Neural DSP") -> list[str]:
    """Product folder names a set of paths sits inside, under the vendor tree.

    Used to work out which products an uninstall is about to touch, without trusting the name the
    MSI gives itself. Order follows the paths, so the caller sees a stable list.
    """
    root = env.program_data / vendor
    names: list[str] = []
    for path in paths:
        try:
            relative = Path(path).relative_to(root)
        except (ValueError, TypeError):
            continue
        if relative.parts and relative.parts[0] not in names:
            names.append(relative.parts[0])
    return names


def _enumerate_tree_strict(root: Path) -> list[tuple[Path, os.stat_result]]:
    """Enumerate a path without hiding scan/stat errors or following symlink directories."""
    try:
        root_stat = os.lstat(root)
    except FileNotFoundError:
        return []

    entries = [(root, root_stat)]
    if not stat.S_ISDIR(root_stat.st_mode):
        return entries

    def raise_scan_error(error: OSError) -> None:
        raise error

    for directory, dirnames, filenames in os.walk(
        root, topdown=True, onerror=raise_scan_error, followlinks=False
    ):
        current = Path(directory)
        descend: list[str] = []
        for name in sorted(dirnames):
            path = current / name
            child_stat = os.lstat(path)
            entries.append((path, child_stat))
            if stat.S_ISDIR(child_stat.st_mode):
                descend.append(name)
            elif not stat.S_ISLNK(child_stat.st_mode):
                raise OSError(f"unsupported file type in rescue tree: {path}")
        dirnames[:] = descend

        for name in sorted(filenames):
            path = current / name
            child_stat = os.lstat(path)
            entries.append((path, child_stat))
            if not (stat.S_ISREG(child_stat.st_mode) or stat.S_ISLNK(child_stat.st_mode)):
                raise OSError(f"unsupported file type in rescue tree: {path}")
    return entries


def _rescue_no_clobber_files(
    env: Environment,
    plan,
    product: str,
    vendor: str,
    destination: Path,
    dry_run: bool,
    already_rescued: set[str],
) -> list[tuple[str, str, str]]:
    """Copy all bytes below a no-clobber MSI destination, not just preset-shaped files.

    The install may have skipped a pre-existing file with byte-identical payload content. The MSI
    has no provenance marker for that decision, so preserve every regular file before msiexec.
    """
    product_root = env.program_data / vendor / product
    prefix_root = Path(env.prefix).resolve()
    try:
        product_root_resolved = product_root.resolve()
    except OSError as exc:
        return [("failed", str(product_root), f"cannot resolve no-clobber product root: {exc}")]
    rows: list[tuple[str, str, str]] = []
    for action in getattr(plan, "actions", []):
        if not getattr(action, "no_clobber", False):
            continue
        action_root = Path(action.dest)
        if action_root.is_symlink():
            rows.append(("failed", str(action_root), "refusing to rescue a symlinked no-clobber root"))
            continue
        try:
            action_resolved = action_root.resolve()
        except OSError as exc:
            rows.append(("failed", str(action_root), f"cannot resolve no-clobber root: {exc}"))
            continue
        if not action_resolved.is_relative_to(prefix_root):
            rows.append(("failed", str(action_root), "no-clobber root is outside the Wine prefix"))
            continue
        if product_root_resolved == action_resolved or product_root_resolved in action_resolved.parents:
            scope = action_root
        elif action_resolved in product_root_resolved.parents:
            scope = product_root
        else:
            continue
        if scope.is_symlink():
            rows.append(("failed", str(scope), "refusing to rescue a symlinked no-clobber scope"))
            continue
        try:
            entries = _enumerate_tree_strict(scope)
        except OSError as exc:
            rows.append(("failed", str(scope), f"cannot enumerate no-clobber files: {exc}"))
            continue
        for source, source_stat in entries:
            if stat.S_ISLNK(source_stat.st_mode):
                rows.append(("failed", str(source), "refusing to rescue a symlink in no-clobber data"))
                continue
            if stat.S_ISDIR(source_stat.st_mode):
                continue
            if not stat.S_ISREG(source_stat.st_mode):
                rows.append(("failed", str(source), "unsupported file type in no-clobber data"))
                continue
            try:
                source_resolved = source.resolve()
                if not source_resolved.is_relative_to(prefix_root):
                    rows.append(("failed", str(source), "no-clobber file resolves outside the Wine prefix"))
                    continue
                key = str(source_resolved).casefold()
                if key in already_rescued:
                    continue
                rows.append(_save_one(source, _rescue_target(env, destination, source), dry_run))
            except OSError as exc:
                rows.append(("failed", str(source), f"cannot rescue no-clobber file: {exc}"))
    return rows


def _unrescued_no_clobber_files(env: Environment, plan, rows) -> list[tuple[str, str, str]]:
    """Fail closed if any existing no-clobber destination file lacks a rescue row.

    MSI identity/path metadata can be incomplete or point outside the conventional vendor tree.
    A rescue lookup miss must not be reported as a warning followed by an uninstall that can delete
    the files. The CLI and GUI already stop when rescue rows contain ``failed``.
    """
    rescued = {
        str(Path(source).resolve()).casefold()
        for status, source, _note in rows
        if status in {"saved", "kept", "dry-run"}
    }
    failures: list[tuple[str, str, str]] = []
    prefix_root = Path(env.prefix).resolve()
    for action in getattr(plan, "actions", []):
        if not getattr(action, "no_clobber", False):
            continue
        root = Path(action.dest)
        if root.is_symlink():
            failures.append(("failed", str(root), "refusing to uninstall with a symlinked no-clobber root"))
            continue
        try:
            root_resolved = root.resolve()
        except OSError as exc:
            failures.append(("failed", str(root), f"cannot resolve no-clobber root: {exc}"))
            continue
        if not root_resolved.is_relative_to(prefix_root):
            failures.append(("failed", str(root), "no-clobber root is outside the Wine prefix"))
            continue
        try:
            entries = _enumerate_tree_strict(root)
        except OSError as exc:
            failures.append(("failed", str(root), f"cannot enumerate no-clobber files: {exc}"))
            continue
        for path, path_stat in entries:
            if stat.S_ISLNK(path_stat.st_mode):
                failures.append(("failed", str(path), "refusing to uninstall with a symlink in no-clobber data"))
                continue
            if stat.S_ISDIR(path_stat.st_mode):
                continue
            if not stat.S_ISREG(path_stat.st_mode):
                failures.append(("failed", str(path), "unsupported file type in no-clobber data"))
                continue
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(prefix_root):
                    failures.append(("failed", str(path), "no-clobber file resolves outside the Wine prefix"))
                    continue
                if str(resolved).casefold() not in rescued:
                    failures.append(("failed", str(path), "no-clobber file was not rescued"))
            except OSError as exc:
                failures.append(("failed", str(path), f"cannot verify no-clobber rescue: {exc}"))
    return failures


def _rescue_roaming_midi(
    env: Environment,
    product: str,
    vendor: str,
    destination: Path,
    dry_run: bool,
    already_rescued: set[str],
) -> list[tuple[str, str, str]]:
    """Strictly rescue user MIDI XML files that MSI no-clobber roots may not cover."""
    root = env.appdata_roaming / vendor / product / "MIDI"
    try:
        prefix_root = Path(env.prefix).resolve()
        root_resolved = root.resolve()
    except OSError as exc:
        return [("failed", str(root), f"cannot resolve roaming MIDI root: {exc}")]
    if not root_resolved.is_relative_to(prefix_root):
        return [("failed", str(root), "roaming MIDI root resolves outside the Wine prefix")]
    try:
        entries = _enumerate_tree_strict(root)
    except OSError as exc:
        return [("failed", str(root), f"cannot enumerate roaming MIDI presets: {exc}")]

    rows: list[tuple[str, str, str]] = []
    for path, path_stat in entries:
        if stat.S_ISLNK(path_stat.st_mode):
            rows.append(("failed", str(path), "refusing to rescue a symlink in roaming MIDI data"))
            continue
        if stat.S_ISDIR(path_stat.st_mode):
            continue
        if not stat.S_ISREG(path_stat.st_mode):
            rows.append(("failed", str(path), "unsupported file type in roaming MIDI data"))
            continue
        if path.suffix.lower() != ".xml":
            continue
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(prefix_root):
                rows.append(("failed", str(path), "roaming MIDI file resolves outside the Wine prefix"))
                continue
            key = str(resolved).casefold()
            if key in already_rescued:
                continue
            row = _save_one(path, _rescue_target(env, destination, path), dry_run)
            rows.append(row)
            if row[0] in {"saved", "kept", "dry-run"}:
                already_rescued.add(key)
        except OSError as exc:
            rows.append(("failed", str(path), f"cannot rescue roaming MIDI file: {exc}"))
    return rows


def rescue_for_plan(
    env: Environment,
    plan,
    vendor: str | None = None,
    rescue_root: Path | None = None,
    dry_run: bool = False,
    stamp: str | None = None,
) -> tuple[list[tuple[str, str, str]], list[tuple[str, Path]], list[str]]:
    """Rescue the presets an uninstall is about to delete, whatever the MSI calls itself.

    `rescue()` needs a product name, and the name is not dependable: a package can declare no
    ProductName, or name itself differently from the folder its own installer created. Either way
    the rescue quietly saved nothing while the uninstall went on to delete the user's presets, so
    the dialog's promise was not kept (reproduced in the review, 2026-09-27).

    The products to look at therefore come from the paths the plan is about to touch and from the
    ProductName it declares, not from the name alone. Returns (rows, [(product, destination)],
    `products_looked_at` - the third is what the caller reports when nothing was found, so "none"
    can be told apart from "we did not look".
    """
    # A selected MSI is the authority for its ProgramData vendor tree. The CLI's legacy default
    # (Neural DSP) is not valid for another manufacturer's PREDIR actions, and using it silently
    # skipped the no-clobber rescue immediately before msiexec could delete those files.
    declared_vendor = getattr(getattr(plan, "identity", None), "manufacturer", "") or ""
    effective_vendor = (
        declared_vendor.strip()
        if isinstance(declared_vendor, str) and declared_vendor.strip()
        else vendor.strip()
        if isinstance(vendor, str) and vendor.strip()
        else "Neural DSP"
    )
    if not _safe_path_component(effective_vendor):
        return [("failed", effective_vendor, "unsafe MSI Manufacturer/vendor path component")], [], []

    candidates: list[str] = []
    declared = getattr(getattr(plan, "identity", None), "product_name", "") or ""
    # Deliberately NOT every product folder in the vendor tree: an uninstall of one product would
    # then copy every other product's presets as well (200 files and four "kept in" destinations,
    # seen on a real dry run). The plan is the authority on what is deleted, so only the products
    # its own destinations sit inside are looked at.
    for name in (
        declared,
        *products_under(env, (action.dest for action in plan.actions), effective_vendor),
    ):
        cleaned = name.strip() if isinstance(name, str) else ""
        if not cleaned or cleaned.lower() == effective_vendor.strip().lower():
            # a destination at the vendor root itself is not a product folder
            continue
        if not _safe_path_component(cleaned):
            return [("failed", cleaned, "unsafe MSI ProductName/product path component")], [], []
        if cleaned not in candidates:
            candidates.append(cleaned)

    rows: list[tuple[str, str, str]] = []
    saved_for: list[tuple[str, Path]] = []
    named = [Path(action.dest) for action in plan.actions]
    for product in candidates:
        product_rows, destination = rescue(
            env, product, effective_vendor, rescue_root, dry_run, stamp
        )
        product_rows.extend(
            _unlisted_presets(env, product, effective_vendor, named, destination, dry_run)
        )
        already_rescued = {
            str(Path(source).resolve()).casefold()
            for status, source, _note in product_rows
            if status in {"saved", "kept", "dry-run"}
        }
        product_rows.extend(_rescue_roaming_midi(
            env, product, effective_vendor, destination, dry_run, already_rescued
        ))
        product_rows.extend(_rescue_no_clobber_files(
            env, plan, product, effective_vendor, destination, dry_run, already_rescued
        ))
        if product_rows:
            rows.extend(product_rows)
            saved_for.append((product, destination))
    rows.extend(_unrescued_no_clobber_files(env, plan, rows))
    return rows, saved_for, candidates


def _unlisted_presets(
    env: Environment,
    product: str,
    vendor: str,
    named: list[Path],
    destination: Path,
    dry_run: bool,
) -> list[tuple[str, str, str]]:
    """Preset files under a product's own folder that the plan does not name.

    `collect()` draws its line at `User` and the `*preset*` pack folders, which is the right line
    for what is irreplaceable - factory content comes back with a reinstall. But a downloaded pack
    does not have to be called "*preset*" to be one: a plain `Packs` folder counted as neither a
    product nor a pack, so its contents were never rescued while the uninstall deleted the whole
    directory the MSI listed (reproduced in the review, 2026-09-27).

    The plan is the authority on what it puts back, so anything preset-shaped here that the plan
    does not name is not reproducible and is copied too. Only for a product the plan actually
    touches: with none of the plan's paths under that folder there is nothing to tell a pack from
    the factory set, and guessing would copy every product's shipped presets into the rescue.
    """
    root = env.program_data / vendor / product
    if not product:
        return []
    try:
        root_resolved = root.resolve()
        resolved = [path.resolve() for path in named]
    except OSError as exc:
        return [("failed", str(root), f"cannot resolve product preset paths: {exc}")]
    if not any(entry == root_resolved or root_resolved in entry.parents for entry in resolved):
        return []
    try:
        prefix_root = Path(env.prefix).resolve()
    except OSError as exc:
        return [("failed", str(root), f"cannot resolve Wine prefix for product presets: {exc}")]
    if not root_resolved.is_relative_to(prefix_root):
        return [("failed", str(root), "product preset root resolves outside the Wine prefix")]
    try:
        entries = _enumerate_tree_strict(root)
    except OSError as exc:
        return [("failed", str(root), f"cannot enumerate product presets: {exc}")]
    if not entries:
        return []
    root_stat = entries[0][1]
    if stat.S_ISLNK(root_stat.st_mode):
        return [("failed", str(root), "refusing to rescue a symlinked product root")]
    if not stat.S_ISDIR(root_stat.st_mode):
        if stat.S_ISREG(root_stat.st_mode):
            return []
        return [("failed", str(root), "unsupported product root type")]
    already = set(collect(env, product, vendor).irreplaceable)
    rows: list[tuple[str, str, str]] = []
    for path, path_stat in entries[1:]:
        if stat.S_ISLNK(path_stat.st_mode):
            rows.append(("failed", str(path), "refusing to rescue a symlink in product preset data"))
            continue
        if stat.S_ISDIR(path_stat.st_mode):
            continue
        if not stat.S_ISREG(path_stat.st_mode):
            rows.append(("failed", str(path), "unsupported file type in product preset data"))
            continue
        if not _is_preset(path) or path in already:
            continue
        target = path.resolve()
        if any(target == entry or entry in target.parents for entry in resolved):
            continue
        rows.append(_save_one(path, _rescue_target(env, destination, path), dry_run))
    return rows


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
