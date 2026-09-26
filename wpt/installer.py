"""Turn an extracted MSI payload into plugin files in the right place.

This is the "skip Windows Installer entirely" route: `msiextract` unpacks the
payload on Linux, and we copy each piece where the MSI's own directory
properties say it belongs. No custom actions, no Wine, no 1603.
"""

from __future__ import annotations

import shutil
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import msi as msi_mod
from . import products as products_mod
from .environment import Environment

SKIP_KEYS_UNLESS_WANTED = {"AAXDIR"}  # Pro Tools only; harmless but bulky


@dataclass
class Action:
    source: Path
    dest: Path
    label: str
    no_clobber: bool = False


@dataclass
class Plan:
    msi: Path
    identity: msi_mod.MsiIdentity
    actions: list[Action] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    expected: dict[str, int] = field(default_factory=dict)
    skipped_app: list[tuple[str, Path]] = field(default_factory=list)   # app/driver payloads, left alone

    def total_bytes(self) -> int:
        total = 0
        for action in self.actions:
            if action.source.is_file():
                total += action.source.stat().st_size
            elif action.source.is_dir():
                total += sum(f.stat().st_size for f in action.source.rglob("*") if f.is_file())
        return total


def _destination_for(
    dirname: str, identity: msi_mod.MsiIdentity, env: Environment, children: list[Path] | None = None
) -> Path | None:
    """Resolve an msiextract folder name to a path inside the prefix.

    `children` are the extracted entries inside that folder, which matter for APPDIR:
    vendors ship it as either `APPDIR:.` = the install directory's contents (children
    include the product folder) or = the product folder itself (children are loose
    files). Getting that wrong nests the standalone app one level too deep.
    """
    key = dirname.split(":")[0].strip().upper()

    # 1. trust the MSI's own declared target, e.g. VST3DIR = C:\Program Files\Common Files\VST3
    declared = identity.properties.get(key, "")
    if declared.upper().startswith("C:\\"):
        rel = declared[3:].replace("\\", "/").rstrip("/")
        return env.drive_c / rel

    # 2. fall back to the standard locations for this stack
    vendor = identity.manufacturer or "Vendor"
    product = identity.product_name or ""
    if key == "VST3DIR":
        return env.vst3_dir
    if key in {"VSTDIR", "VST2DIR"}:
        return env.vst2_dir
    if key == "AAXDIR":
        return env.aax_dir
    if key == "PREDIR":
        return env.program_data / vendor / product
    if key in {
        "APPDIR",
        "INSTALLDIR",
        "PROGRAMFILESDIR",
        # plain application/driver payloads (Neural DSP's Nano Cortex driver MSI names its
        # directory PFiles64): the same product-folder rule applies, and without this the
        # package silently resolves to nothing at all
        "PFILES64",
        "PFILES",
        "PFILES32",
        "PROGRAMFILES64FOLDER",
        "PROGRAMFILESFOLDER",
        "INSTDIR",
    }:
        # if the payload already contains a folder named after the product, the
        # destination is the vendor directory itself; otherwise add the product
        if children and any(
            child.is_dir() and _same_product(child.name, product or vendor) for child in children
        ):
            return env.program_files / vendor
        return env.program_files / vendor / product
    return None


def _same_product(name: str, product: str) -> bool:
    normalise = lambda value: "".join(ch for ch in value.lower() if ch.isalnum())  # noqa: E731
    return bool(product) and normalise(name) == normalise(product)


PLUGIN_KEYS = {"VST3DIR", "VSTDIR", "VST2DIR", "AAXDIR"}


def places_a_plugin(plan: Plan) -> bool:
    """True when the plan places a VST3/VST2/AAX payload — i.e. something a DAW can scan.

    A plan that places files but no plugin payload is a driver or an application package. Saying
    so is the whole point: Neural DSP's Nano Cortex download is a USB driver (.sys/.inf/.cat) plus
    a control panel, and "0 destinations, install complete" said nothing about why.
    """
    return any(action.label.split(" ")[0] in PLUGIN_KEYS for action in plan.actions)


def build_plan(
    msi: Path,
    env: Environment,
    extraction_root: Path,
    *,
    include_vst2: bool = True,
    include_aax: bool = False,
    include_standalone: bool = True,
    include_presets: bool = True,
    include_app_files: bool = False,
) -> Plan:
    """Describe every copy the install needs, without touching the disk."""
    ident = msi_mod.identity(msi)
    plan = Plan(msi=msi, identity=ident, expected=msi_mod.expected_sizes(msi))
    # A plugin package may carry a standalone app (APPDIR) and other directories; an application
    # or driver package carries none of the plugin payload directories. That is the line.
    is_plugin_package = msi_mod.declares_plugin_payload(msi)

    for name in msi_mod.extracted_root_dirs(extraction_root):
        key = name.split(":")[0].strip().upper()
        source = extraction_root / name
        children = sorted(source.iterdir()) if source.is_dir() else []
        dest = _destination_for(name, ident, env, children=children)

        if key == "AAXDIR" and not include_aax:
            plan.warnings.append(f"skipping {name} (AAX / Pro Tools only)")
            continue
        if key in {"VSTDIR", "VST2DIR"} and not include_vst2:
            plan.warnings.append(f"skipping {name} (VST2 disabled)")
            continue
        if key in {"APPDIR", "INSTALLDIR"} and not include_standalone:
            plan.warnings.append(f"skipping {name} (standalone disabled)")
            continue
        if key == "PREDIR" and not include_presets:
            plan.warnings.append(f"skipping {name} (presets disabled)")
            continue
        if dest is None:
            plan.warnings.append(f"no destination known for {name} - skipped")
            continue

        if not is_plugin_package and key not in PLUGIN_KEYS and key != "PREDIR" and not include_app_files:
            # A driver or an application package: placing its files by hand is not a favour.
            # A driver needs its INF and service registration, which only its own installer
            # performs — files dropped in the wrong place are worse than files left alone.
            # (Neural DSP's Nano Cortex download is exactly this: a USB driver + control panel.)
            plan.skipped_app.append((name, dest))
            plan.warnings.append(
                f"{name} is an application/driver payload, not a plugin - left alone "
                f"(it would go to {dest}). Run the vendor's own installer for it, or pass "
                f"--include-app-files to place the files anyway"
            )
            continue

        for child in sorted(source.iterdir()):
            plan.actions.append(
                Action(
                    source=child,
                    dest=dest / child.name,
                    label=f"{key} -> {dest}",
                    no_clobber=(key == "PREDIR"),
                )
            )

    if plan.actions and not places_a_plugin(plan):
        # Someone asked to install this and it placed files; those files are an application or a
        # driver, not a plugin. Verified on Neural DSP's Nano Cortex package 2026-09-26: it is a
        # USB driver (.sys/.inf/.cat) plus a control panel, so no DAW will ever scan it.
        plan.warnings.append(
            "this package declares no VST3/VST2/AAX payload: it is an application or driver, "
            "not a plugin - a DAW will not see what it places"
        )
    return plan


def apply_plan(plan: Plan, dry_run: bool = False) -> list[tuple[str, str, str]]:
    """Execute a plan. Returns (status, path, note) rows; nothing is silent."""
    results: list[tuple[str, str, str]] = []
    for action in plan.actions:
        if dry_run:
            results.append(("dry-run", str(action.dest), action.label))
            continue
        action.dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            if action.source.is_dir():
                if action.no_clobber:
                    copied = _copy_tree_no_clobber(action.source, action.dest)
                    results.append(("copied", str(action.dest), f"{copied} new files (kept existing)"))
                else:
                    shutil.copytree(action.source, action.dest, dirs_exist_ok=True)
                    results.append(("copied", str(action.dest), "tree merged"))
            else:
                if action.no_clobber and action.dest.exists():
                    results.append(("kept", str(action.dest), "already present"))
                    continue
                shutil.copy2(action.source, action.dest)
                results.append(("copied", str(action.dest), _human(action.source.stat().st_size)))
        except OSError as exc:
            results.append(("failed", str(action.dest), str(exc)))
    return results


def _copy_tree_no_clobber(source: Path, dest: Path) -> int:
    copied = 0
    dest.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        target = dest / item.relative_to(source)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not target.exists():
            shutil.copy2(item, target)
            copied += 1
    return copied


def _measured_size(path: Path, filename: str) -> int | None:
    """Size of the file the MSI named.

    Some payloads are directories (an .aaxplugin bundle, a preset tree). The MSI's
    File table names the binary *inside* the bundle, so measure that rather than the
    directory entry -- comparing a directory's own size against the MSI reports a
    bogus size-mismatch.
    """
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        for candidate in path.rglob("*"):
            if candidate.is_file() and candidate.name.lower() == filename.lower():
                return candidate.stat().st_size
    return None


def verify_plan(plan: Plan) -> list[tuple[str, str, str]]:
    """Compare what is now on disk with the sizes the MSI promised.

    This is the acceptance test: the File table's FileSize column is exact, so a
    mismatched or missing file means the install did not really complete.
    """
    rows: list[tuple[str, str, str]] = []
    for action in plan.actions:
        for target_name, expected_size in plan.expected.items():
            if action.dest.name.lower() != target_name.lower():
                continue
            if not action.dest.exists():
                rows.append(("missing", str(action.dest), f"expected {expected_size} bytes"))
                continue
            actual = _measured_size(action.dest, target_name)
            if actual is None:
                # the path the MSI named is a directory and does not contain the file
                # the File table describes -- that is the bundle-half-empty failure
                rows.append(("missing", str(action.dest), f"no {target_name} inside it"))
            elif actual == expected_size:
                rows.append(("ok", str(action.dest), _human(actual)))
            else:
                rows.append(
                    ("size-mismatch", str(action.dest), f"on disk {actual}, MSI says {expected_size}")
                )
    return rows


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size / 1:.1f} {unit}"
        size = size / 1024  # type: ignore[assignment]
    return f"{size} B"


def filter_needing_repair(plan: Plan) -> Plan:
    """Keep only the destinations that are missing or the wrong size.

    This is what `repair` acts on: a plugin whose registry entry exists but whose
    files vanished (or were half-written) gets exactly those files put back.
    """
    repaired = Plan(msi=plan.msi, identity=plan.identity, expected=plan.expected)
    repaired.warnings = list(plan.warnings)
    for action in plan.actions:
        destination = action.dest
        expected_size = plan.expected.get(destination.name.lower()) or plan.expected.get(destination.name)
        if not destination.exists():
            repaired.actions.append(action)
            continue
        if not expected_size:
            continue
        if destination.is_file():
            if _safe_size(destination) != expected_size:
                repaired.actions.append(action)
            continue
        # A directory destination is a bundle (.vst3/.aaxplugin): its own mtime says nothing, so the
        # size has to be measured the same way verification measures it. Skipping these meant a broken
        # bundle was reported as intact while verify_plan flagged it — the filter and the verifier
        # have to agree, so both go through _measured_size.
        if _measured_size(destination, destination.name) != expected_size:
            repaired.actions.append(action)
    return repaired


def _safe_size(path: Path) -> int | None:
    """Size of a file, or None if it cannot be read (it may have vanished since we listed it)."""
    try:
        return path.stat().st_size
    except OSError:
        return None


def uninstall(env: Environment, product_code: str, dry_run: bool = False) -> tuple[int, str]:
    """Remove a product through Windows Installer, inside the prefix.

    Uninstalling is the one operation worth routing through msiexec: it also
    clears the registration. It works even for products whose *install* fails,
    because the bootstrapper-only launch condition is satisfied by REMOVE="ALL".
    """
    import subprocess

    command = [
        str(env.wine_binary),
        "msiexec",
        "/x",
        product_code,
        "/qn",
    ]
    if dry_run:
        return 0, "would run: " + " ".join(command)
    try:
        proc = subprocess.run(
            command, env=env.wine_env(), capture_output=True, text=True, timeout=600
        )
    except subprocess.TimeoutExpired:
        # a stuck wineserver is common on a prefix in a bad state; say so rather than raising,
        # because the caller has to tell the user how far the uninstall got (usually nowhere)
        return 124, f"{env.wine_binary.name} msiexec /x did not finish within 600s - nothing was removed"
    except OSError as exc:
        return 125, f"could not run wine: {exc}"
    detail = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return proc.returncode, detail


DISABLED_SUFFIX = ".disabled"

# Roots that must never be pruned, even when they end up empty.
PRUNE_STOP = ("vst3_dir", "vst2_dir", "aax_dir", "program_files", "program_data", "drive_c")


def _removal_targets(action: Action) -> list[Path]:
    """Paths a destination could exist as: the real name, and the disabled rename."""
    if action.source.is_dir():
        return [action.dest]
    return [action.dest, action.dest.with_name(action.dest.name + DISABLED_SUFFIX)]


def _prune_empty_parents(parent: Path, env: Environment) -> int:
    """Remove now-empty directories above a deleted file, never the roots themselves."""
    stops = {getattr(env, name) for name in PRUNE_STOP if hasattr(env, name)}
    removed = 0
    while parent not in stops and parent != env.drive_c and parent.is_relative_to(env.drive_c):
        try:
            parent.rmdir()  # fails while it still has anything in it, which is the point
        except OSError:
            break
        removed += 1
        parent = parent.parent
    return removed


def remove_files(plan: Plan, env: Environment, dry_run: bool = False) -> list[tuple[str, str, str]]:
    """Delete exactly the files this MSI placed -- its File table, nothing else.

    This is the uninstall route that works on a prefix where the product was never
    registered with Windows Installer (`msiexec /x` then has nothing to remove and
    silently does nothing). It never deletes a path the MSI does not name, and it
    refuses anything outside the prefix.
    """
    rows: list[tuple[str, str, str]] = []
    removed_dirs: list[Path] = []
    for action in plan.actions:
        if not action.dest.is_relative_to(env.drive_c):
            rows.append(("refused", str(action.dest), "outside the prefix"))
            continue
        for target in _removal_targets(action):
            if not target.exists():
                continue
            if dry_run:
                rows.append(("dry-run", str(target), "would be deleted"))
                continue
            try:
                if target.is_dir():
                    count = sum(1 for f in target.rglob("*") if f.is_file())
                    shutil.rmtree(target)
                    rows.append(("removed", str(target), f"directory, {count} file(s)"))
                else:
                    size = target.stat().st_size
                    target.unlink()
                    rows.append(("removed", str(target), _human(size)))
                    removed_dirs.append(target.parent)
            except OSError as exc:
                rows.append(("failed", str(target), str(exc)))

    if not dry_run and removed_dirs:
        pruned = 0
        for directory in sorted(set(removed_dirs), key=lambda p: -len(p.parts)):
            pruned += _prune_empty_parents(directory, env)
        if pruned:
            rows.append(("pruned", "", f"{pruned} empty director{'y' if pruned == 1 else 'ies'} removed"))
    return rows


def leftovers(plan: Plan, env: Environment) -> list[Path]:
    """Anything the MSI describes that is still on disk after a removal."""
    remaining: list[Path] = []
    for action in plan.actions:
        for target in _removal_targets(action):
            if target.is_relative_to(env.drive_c) and target.exists():
                remaining.append(target)
    return remaining


# ------------------------------------------------------------------ purge


@dataclass
class RegistryEdit:
    """One registry change to make, with the reason it is safe to make it."""

    hive: str            # "HKLM" (system.reg) or "HKCU" (user.reg)
    key: str             # backslash-separated key path
    value: str | None    # None = delete the whole key
    reason: str

    @property
    def target(self) -> str:
        return f"{self.hive}\\{self.key}" + (f"  [{self.value}]" if self.value else "")

    def command(self, wine: Path) -> list[str]:
        command = [str(wine), "reg", "delete", f"{self.hive}\\{self.key}", "/f"]
        if self.value is not None:
            command = [str(wine), "reg", "delete", f"{self.hive}\\{self.key}", "/v", self.value, "/f"]
        return command


def stale_registry_edits(env: Environment, plan: Plan, product_code: str | None = None) -> list[RegistryEdit]:
    """Registry entries that point at files this MSI placed (and that are now gone).

    A vendor installer registers where it put things. Deleting the files leaves those
    values behind, pointing at nothing -- which is what makes `scan` report a
    just-uninstalled product as a broken install. These are the entries worth
    removing, and only these: nothing is touched that does not point at this
    product's own payload.
    """
    placed = {action.dest for action in plan.actions}
    filenames = {name.lower() for name in plan.expected}
    # The directories this product owns (the destination itself for a file, the destination for a
    # bundle). A registry value pointing *somewhere else* must not be deleted just because the file
    # at the end of it happens to share a basename with one of this MSI's files - shared helpers,
    # `setup.exe` and common plugin DLL names recur across products, and a purge is irreversible.
    owned_dirs = {(a.dest if a.dest.is_dir() else a.dest.parent) for a in plan.actions} | placed
    code = (product_code or "").strip("{}").upper()
    product_name = (plan.identity.product_name or "").lower()
    edits: list[RegistryEdit] = []
    seen: set[tuple[str, str, str | None]] = set()
    unsure: set[str] = set()

    for reg_name, hive in (("system.reg", "HKLM"), ("user.reg", "HKCU")):
        reg = env.prefix / reg_name
        if not reg.is_file():
            continue
        for raw_key, payload in products_mod.reg_sections(reg).items():
            # .reg keys use doubled backslashes; `wine reg delete` wants single
            key = raw_key.replace("\\\\", "\\")
            lowered = key.lower()
            if code and code in key.upper() and ("uninstall" in lowered or "installproperties" in lowered):
                marker = (hive, key, None)
                if marker not in seen:
                    seen.add(marker)
                    edits.append(RegistryEdit(hive, key, None, "the product's own registration entry"))
                continue
            for name, data in payload["values"].items():
                path = products_mod.to_path(env.drive_c, data)
                if path is None:
                    continue
                under_ours = any(directory in path.parents for directory in owned_dirs if directory)
                if path in placed or (path.parent in placed) or (under_ours and path.name.lower() in filenames):
                    reason = f"points at {path.name}, which this MSI placed"
                elif path.name.lower() in filenames and not under_ours:
                    # same filename, different place: likely another product's copy of a shared
                    # file. Reported, never edited.
                    unsure.add(path.name)
                elif not path.exists() and product_name and product_name in str(path).lower():
                    # e.g. the vendor's 'user presets live here' record, after that
                    # directory has been removed: a pointer to nothing, named after
                    # this product, so it is safe to drop
                    reason = f"stale pointer to {path} (gone, and named after this product)"
                else:
                    continue
                marker = (hive, key, name)
                if marker in seen:
                    continue
                seen.add(marker)
                edits.append(RegistryEdit(hive, key, name, reason))

    for name in sorted(unsure):
        plan.warnings.append(
            f"a registry value points at a file named {name} outside this product's own folders - "
            "left alone (it may belong to another product that ships the same file name)"
        )
    return edits


def purge_registry(
    env: Environment, edits: list[RegistryEdit], dry_run: bool = False
) -> list[tuple[str, str, str]]:
    """Apply registry edits through `wine reg delete`, so the prefix stays consistent."""
    import subprocess

    rows: list[tuple[str, str, str]] = []
    for edit in edits:
        command = edit.command(env.wine_binary)
        if dry_run:
            rows.append(("dry-run", edit.target, edit.reason))
            continue
        try:
            proc = subprocess.run(command, env=env.wine_env(), capture_output=True, text=True,
                                  timeout=300)
        except (subprocess.TimeoutExpired, OSError) as exc:
            rows.append(("failed", edit.target, f"{type(exc).__name__}: nothing was changed"))
            continue
        if proc.returncode == 0:
            rows.append(("purged", edit.target, edit.reason))
        else:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            rows.append(("failed", edit.target, detail[0] if detail else f"exit {proc.returncode}"))
    return rows


def set_enabled(
    env: Environment, substring: str, enabled: bool, dry_run: bool = False
) -> list[tuple[str, str, str]]:
    """Hide plugins from a DAW's scanner, or bring them back.

    Renaming is the least invasive switch there is: no registry edit, no
    uninstall, and it is exactly reversible. The DAW only scans for the real
    suffix, so `X.vst3.disabled` is invisible to it.
    """
    rows: list[tuple[str, str, str]] = []
    roots = [env.vst3_dir, env.vst2_dir]
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or substring.lower() not in path.name.lower():
                continue
            is_disabled = path.name.lower().endswith(DISABLED_SUFFIX)
            if enabled and not is_disabled:
                continue
            if not enabled and is_disabled:
                continue
            target = path.with_name(
                path.name[: -len(DISABLED_SUFFIX)] if enabled else path.name + DISABLED_SUFFIX
            )
            if dry_run:
                note = f"-> {target.name}"
                if target.exists():
                    note += "  (refused: that name is already taken)"
                rows.append(("dry-run", str(path), note))
                continue
            # Path.rename() on POSIX replaces an existing destination without a word. Both the real
            # file and its .disabled twin existing means renaming would destroy one of them, and a
            # plugin file is not something to lose silently.
            if target.exists():
                rows.append(("kept", str(path), f"{target.name} already exists - nothing renamed"))
                continue
            try:
                path.rename(target)
                rows.append(("renamed", str(path), f"-> {target.name}"))
            except OSError as exc:
                rows.append(("failed", str(path), str(exc)))
    return rows


def product_leftovers(env: Environment, product: str, vendor: str = "Neural DSP") -> list[Path]:
    """Paths in the prefix that still carry this product's name, when no MSI can.

    This is the dead end a real uninstall can reach: `msiexec /x` removes Windows Installer's
    cached copy of the package, and if that was the only copy (anything installed by running its
    vendor wrapper) there is nothing left to derive a File table from — so the toolkit cannot
    say what belongs to the product any more. It can still *show* what is there, which is what
    this does: the vendor's ProgramData folder, any impulse-response folder of the same name,
    and files in the plugin directories whose names match. Reporting it is honest; deleting by
    name is not something this tool should do silently.
    """
    needles = [word.lower() for word in re.split(r"[^A-Za-z0-9]+", product) if len(word) >= 4]
    if not needles:
        return []

    def matches(path: Path) -> bool:
        name = path.name.lower()
        return all(needle in name for needle in needles) or (
            len(needles) == 1 and needles[0] in name
        )

    hits: list[Path] = []
    # the vendor's own trees, where a product's presets and IRs live
    for base_dir in (env.program_data / vendor, env.program_data / vendor / "Impulse Responses"):
        if base_dir.is_dir():
            hits.extend(sorted(p for p in base_dir.iterdir() if matches(p)))
    # plugin directories, one product folder deep
    for root in (env.vst3_dir, env.vst2_dir, env.aax_dir, env.program_files):
        if not root.is_dir():
            continue
        for path in list(root.iterdir()) + [p for d in root.iterdir() if d.is_dir() for p in d.iterdir()]:
            if matches(path):
                hits.append(path)
    return sorted(set(hits), key=str)


def render_plan(plan: Plan) -> str:
    lines = [
        f"MSI:          {plan.msi}",
        f"Product:      {plan.identity.label}",
        f"ProductCode:  {plan.identity.product_code or '-'}",
        f"UpgradeCode:  {plan.identity.upgrade_code or '-'}",
        f"Total payload: {len(plan.actions)} destinations, {_human(plan.total_bytes())}",
        "",
    ]
    for action in plan.actions:
        marker = "keep-if-exists" if action.no_clobber else "write"
        lines.append(f"  [{marker}] {action.source}  ->  {action.dest}")
    for warning in plan.warnings:
        lines.append(f"  ! {warning}")
    return "\n".join(lines)