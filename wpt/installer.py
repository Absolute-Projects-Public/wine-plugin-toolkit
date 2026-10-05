"""Turn an extracted MSI payload into plugin files in the right place.

This is the "skip Windows Installer entirely" route: `msiextract` unpacks the
payload on Linux, and we copy each piece where the MSI's own directory
properties say it belongs. No custom actions, no Wine, no 1603.
"""

from __future__ import annotations

import os
import shutil
import re
import stat
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
    owned_files: dict[Path, msi_mod.MsiFileEntry] = field(default_factory=dict)
    skipped_app: list[tuple[str, Path]] = field(default_factory=list)   # app/driver payloads, left alone
    destinations_only: bool = False   # read from the MSI's own tables: what to remove, nothing to place
    shared_paths: set[str] = field(default_factory=set)  # case-folded resolved paths claimed by another MSI

    def total_bytes(self) -> int:
        if self.destinations_only:   # there is no payload to copy, only destinations to act on
            return 0
        total = 0
        for action in self.actions:
            if action.source.is_file():
                total += action.source.stat().st_size
            elif action.source.is_dir():
                total += sum(f.stat().st_size for f in action.source.rglob("*") if f.is_file())
        return total


def ownership_key(path: Path) -> str:
    """Case-insensitive identity for a planned destination, including symlink aliases."""
    try:
        return str(Path(path).resolve()).casefold()
    except OSError:
        return str(Path(path).absolute()).casefold()


def inside_prefix(path: Path, env: Environment) -> bool:
    """Whether a path really is inside this prefix.

    Everything this tool writes or deletes must be. The check is deliberately made through
    `resolve()`: a path can be spelled inside the prefix and still point outside it
    (`<prefix>/drive_c/../../../etc`), and the whole point is to catch that.
    """
    drive_c = getattr(env, "drive_c", None)
    if drive_c is None:
        return False          # without a prefix there is nothing to be inside of: refuse
    try:
        return Path(path).resolve().is_relative_to(Path(drive_c).resolve())
    except OSError:
        return False


def _safe_msi_folder_component(value: str, label: str, *, allow_empty: bool = False) -> str:
    """Normalize an MSI identity field before using it as one filesystem component."""
    if not isinstance(value, str):
        raise msi_mod.MsiError(f"unsafe MSI {label} path component")
    normalized = value.strip()
    if not normalized and allow_empty:
        return ""
    if (not normalized or normalized in {".", ".."} or chr(0) in normalized
            or "/" in normalized or chr(92) in normalized):
        raise msi_mod.MsiError(f"unsafe MSI {label} path component")
    return normalized


def _fallback_base(base: Path, env: Environment) -> Path:
    if not inside_prefix(base, env):
        raise msi_mod.MsiError("fallback MSI destination escaped its install root")
    return Path(base)


def _fallback_destination(
    base: Path, vendor: str, product: str, env: Environment
) -> Path:
    vendor_part = _safe_msi_folder_component(vendor, "Manufacturer")
    product_part = _safe_msi_folder_component(product, "ProductName", allow_empty=True)
    candidate = Path(base) / vendor_part
    if product_part:
        candidate /= product_part
    try:
        base_resolved = Path(base).resolve()
        candidate_resolved = candidate.resolve()
    except OSError as exc:
        raise msi_mod.MsiError(f"cannot resolve fallback MSI destination: {exc}") from exc
    if not candidate_resolved.is_relative_to(base_resolved) or not inside_prefix(candidate, env):
        raise msi_mod.MsiError("fallback MSI destination escaped its expected install root")
    return candidate


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
    #    -- but only as far as the prefix. A declared path is data out of an installer, and
    #    `C:\..\..\..\home\you` resolves outside the prefix; placing (or later deleting) there
    #    is not something this tool does unasked.
    declared = identity.properties.get(key, "")
    if declared.upper().startswith("C:\\"):
        rel = declared[3:].replace("\\", "/").rstrip("/")
        candidate = env.drive_c / rel
        if not inside_prefix(candidate, env):
            return None
        return candidate

    # 2. fall back to the standard locations for this stack
    if key == "VST3DIR":
        return env.vst3_dir
    if key in {"VSTDIR", "VST2DIR"}:
        return env.vst2_dir
    if key == "AAXDIR":
        return env.aax_dir

    vendor = identity.manufacturer or "Vendor"
    product = identity.product_name or ""
    if key == "PREDIR":
        return _fallback_destination(env.program_data, vendor, product, env)
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
        vendor_part = _safe_msi_folder_component(vendor, "Manufacturer")
        product_part = _safe_msi_folder_component(product, "ProductName", allow_empty=True)
        # if the payload already contains a folder named after the product, the
        # destination is the vendor directory itself; otherwise add the product
        if children and any(
            child.is_dir() and _same_product(child.name, product_part or vendor_part) for child in children
        ):
            return _fallback_destination(env.program_files, vendor_part, "", env)
        if children and any(
            child.is_dir() and _same_product(child.name, vendor_part) for child in children
        ):
            return _fallback_base(env.program_files, env)
        return _fallback_destination(env.program_files, vendor_part, product_part, env)
    return None


def _same_product(name: str, product: str) -> bool:
    normalise = lambda value: "".join(ch for ch in value.lower() if ch.isalnum())  # noqa: E731
    return bool(product) and normalise(name) == normalise(product)


PLUGIN_KEYS = {"VST3DIR", "VSTDIR", "VST2DIR", "AAXDIR"}


def unmapped_destination_warnings(plan: Plan) -> list[str]:
    """Return MSI-root warnings that mean some File rows have no safe destination."""
    return [warning for warning in plan.warnings
            if warning.casefold().startswith("no destination known")]


def places_a_plugin(plan: Plan) -> bool:
    """True when the plan places a VST3/VST2/AAX payload, i.e. something a DAW can scan.

    A plan that places files but no plugin payload is a driver or an application package. Saying
    so is the whole point: Neural DSP's Nano Cortex download is a USB driver (.sys/.inf/.cat) plus
    a control panel, and "0 destinations, install complete" said nothing about why.
    """
    return any(action.label.split(" ")[0] in PLUGIN_KEYS for action in plan.actions)


class PayloadEntry:
    """One entry inside a payload directory, as the MSI's own tables describe it.

    `_destination_for` only reads `.name` and `.is_dir()` off what it is handed -- normally paths
    from msiextract's output, here from the Directory/Component/File tables. Both routes therefore
    run through exactly the same resolution, so a tables-derived plan cannot drift from an
    extracted one.
    """

    __slots__ = ("name", "_is_dir")

    def __init__(self, name: str, is_dir: bool) -> None:
        self.name = name
        self._is_dir = is_dir

    def is_dir(self) -> bool:
        return self._is_dir


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
    root = Path(extraction_root)
    return _plan_from(
        msi,
        env,
        roots=msi_mod.extracted_root_dirs(root),
        entries_for=lambda name: sorted((root / name).iterdir()) if (root / name).is_dir() else [],
        destinations_only=False,
        include_vst2=include_vst2,
        include_aax=include_aax,
        include_standalone=include_standalone,
        include_presets=include_presets,
        include_app_files=include_app_files,
    )


def build_plan_from_tables(
    msi: Path,
    env: Environment,
    *,
    include_vst2: bool = True,
    include_aax: bool = False,
    include_standalone: bool = True,
    include_presets: bool = True,
    include_app_files: bool = False,
) -> Plan:
    """Describe what this MSI placed, from its own tables, with no payload needed.

    `build_plan()` extracts the payload first, and `msiextract` needs the cabinet for that. Wine's
    installer cache keeps only the MSI, and a vendor bootstrapper deletes the payload it unpacked
    once it is done, so an installed product can end up with its media gone -- and then reading it
    failed outright and an uninstall was impossible, even though the MSI knows exactly which files
    it placed. (Fortin Cali Suite on this machine, 2026-09-27: a working install nobody could
    remove.)

    Everything a removal needs is in the tables. The result carries no payload: `destinations_only`
    is set, so `apply_plan()` will not install from it.
    """
    tree = msi_mod.payload_directories(msi)
    return _plan_from(
        msi,
        env,
        roots=sorted(tree),
        entries_for=lambda name: [PayloadEntry(n, d) for n, d in tree.get(name, [])],
        destinations_only=True,
        include_vst2=include_vst2,
        include_aax=include_aax,
        include_standalone=include_standalone,
        include_presets=include_presets,
        include_app_files=include_app_files,
    )


def _plan_from(
    msi: Path,
    env: Environment,
    *,
    roots: list[str],
    entries_for,
    destinations_only: bool,
    include_vst2: bool = True,
    include_aax: bool = False,
    include_standalone: bool = True,
    include_presets: bool = True,
    include_app_files: bool = False,
) -> Plan:
    """The one place a payload directory becomes a destination, however it was read."""
    ident = msi_mod.identity(msi)
    plan = Plan(
        msi=msi,
        identity=ident,
        expected=msi_mod.expected_sizes(msi),
        destinations_only=destinations_only,
    )
    # A plugin package may carry a standalone app (APPDIR) and other directories; an application
    # or driver package carries none of the plugin payload directories. That is the line.
    is_plugin_package = msi_mod.declares_plugin_payload(msi)

    for name in roots:
        key = name.split(":")[0].strip().upper()
        children = entries_for(name)
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
            # performs: files dropped in the wrong place are worse than files left alone.
            # (Neural DSP's Nano Cortex download is exactly this: a USB driver + control panel.)
            plan.skipped_app.append((name, dest))
            plan.warnings.append(
                f"{name} is an application/driver payload, not a plugin - left alone "
                f"(it would go to {dest}). Run the vendor's own installer for it, or pass "
                f"--include-app-files to place the files anyway"
            )
            continue

        for child in children:
            target = dest / child.name
            # A nested Directory DefaultDir='.' resolves to its parent. Treating it as an
            # action would let remove_files() delete an entire shared VST/preset root.
            if (target.resolve() == dest.resolve()
                    or not target.resolve().is_relative_to(dest.resolve())
                    or not inside_prefix(target, env)):
                raise msi_mod.MsiError(
                    f"refusing payload entry {child.name!r}: it resolves to a shared root or "
                    "outside its declared destination"
                )
            plan.actions.append(
                Action(
                    # a tables plan has no payload to copy, so `source` is nominal there; it is
                    # never read (apply_plan refuses such a plan, and removal acts on `dest`)
                    source=child if isinstance(child, Path) else Path(child.name),
                    dest=target,
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
    # The File table's basename is not an identity: bundles and preset trees can contain the
    # same name many times. Map every selected row to the destination of its payload root.
    roots: dict[str, Path] = {}
    for action in plan.actions:
        root_key = action.label.split(" ", 1)[0].casefold()
        parent = action.dest.parent
        if root_key in roots and roots[root_key] != parent:
            raise msi_mod.MsiError(f"inconsistent destination for {root_key}")
        roots[root_key] = parent
    # A registered uninstall treats these warnings as a refusal before msiexec. File rows without
    # an action must never disappear from ownership accounting silently.
    skipped_roots = {name.split(":", 1)[0].casefold() for name, _dest in plan.skipped_app}
    for entry in msi_mod.file_manifest(msi):
        entry_root = entry.root.casefold()
        if entry_root not in roots:
            if entry_root in skipped_roots:  # known app/driver root; intentionally left alone
                continue
            warning_prefix = f"no destination known for {entry_root}"
            if not any(
                warning.casefold() == warning_prefix
                or warning.casefold().startswith(warning_prefix + " ")
                or warning.casefold().startswith(warning_prefix + ":")
                for warning in plan.warnings
            ):
                plan.warnings.append(
                    f"no destination known for {entry.root} (unmapped File-table root) - skipped"
                )
            continue
        dest = roots[entry_root] / entry.relative
        if not inside_prefix(dest, env) or not any(
            dest == action.dest or dest.is_relative_to(action.dest)
            for action in plan.actions if action.label.split(" ", 1)[0].casefold() == entry_root
        ):
            raise msi_mod.MsiError(f"File row {entry.file_key!r} is outside its planned destination")
        if dest in plan.owned_files:
            raise msi_mod.MsiError(f"ambiguous MSI destination: {dest}")
        plan.owned_files[dest] = entry
    if plan.actions and not plan.owned_files:
        raise msi_mod.MsiError("no exact File-table manifest for the planned payload")
    if not plan.destinations_only:
        uncertain = [entry for entry in plan.owned_files.values() if entry.removal_state_uncertain]
        if uncertain:
            files = ", ".join(entry.file_key for entry in uncertain[:5])
            raise msi_mod.MsiError(
                "cannot safely install files from MSI components with conditional or non-default "
                f"attributes without a component-selection model: {files}"
            )
        for action in plan.actions:
            entries = [destination for destination in plan.owned_files
                       if destination == action.dest or destination.is_relative_to(action.dest)]
            if not entries:
                raise msi_mod.MsiError(f"payload action has no File-table rows: {action.dest}")
            if action.source.is_symlink():
                raise msi_mod.MsiError(f"payload source is a symlink: {action.source}")
            declared: set[Path] = set()
            for destination in entries:
                relative = destination.relative_to(action.dest)
                source = action.source / relative if relative.parts else action.source
                if source.is_symlink() or not source.is_file():
                    raise msi_mod.MsiError(f"payload is missing its File-table file: {source}")
                if source.stat().st_size != plan.owned_files[destination].size:
                    raise msi_mod.MsiError(f"payload size differs from File table: {source}")
                declared.add(source)
            actual = ({path for path in action.source.rglob("*") if path.is_file() or path.is_symlink()}
                      if action.source.is_dir() else {action.source})
            if any(path.is_symlink() for path in actual) or actual != declared:
                raise msi_mod.MsiError(f"payload differs from its File-table manifest: {action.source}")
    return plan


def apply_plan(
    plan: Plan, env: Environment | None = None, dry_run: bool = False
) -> list[tuple[str, str, str]]:
    """Execute a plan. Returns (status, path, note) rows; nothing is silent.

    `env` is what makes the containment check possible: everything written must land inside the
    prefix. Passing no env is only allowed for a dry run, which writes nothing - a real run without
    it refuses every action rather than guessing.
    """
    if plan.destinations_only:
        # Built from the MSI's own tables: it names the destinations to remove and has no payload
        # behind it, so there is nothing to copy. Installing from it would treat file *names* as
        # files. Refuse: a removal is what such a plan is for (see remove_files()).
        raise msi_mod.MsiError(
            "this plan was read from the MSI's own tables, not from its payload - it describes "
            "what to remove, not what to install. Its cabinet is not on disk; run the vendor's "
            "installer once to restore it."
        )
    results: list[tuple[str, str, str]] = []
    for action in plan.actions:
        # Second line of defence: a plan should already contain only in-prefix destinations, but
        # this is the function that writes files, so it checks for itself rather than trusting
        # whoever built the plan (an older version, a hand-edited plan, a future caller).
        if env is None:
            if dry_run:
                results.append(("dry-run", str(action.dest), action.label))
            else:
                results.append(("refused", str(action.dest),
                                "no prefix given to apply_plan - not written"))
            continue
        if not inside_prefix(action.dest, env):
            results.append(("refused", str(action.dest), "outside the prefix - not written"))
            continue
        if dry_run:
            results.append(("dry-run", str(action.dest), action.label))
            continue
        try:
            if action.source.is_dir():
                if action.no_clobber:
                    copied = _copy_tree_no_clobber(action.source, action.dest, env)
                    results.append(("copied", str(action.dest), f"{copied} new files (kept existing)"))
                else:
                    copied = _copy_tree_overwrite(action.source, action.dest, env)
                    results.append(("copied", str(action.dest), f"tree merged; {copied} file(s) written"))
            else:
                if action.no_clobber and action.dest.exists():
                    results.append(("kept", str(action.dest), "already present"))
                    continue
                copied = _copy_file_no_follow(
                    action.source, action.dest, env, overwrite=not action.no_clobber
                )
                if copied:
                    results.append(("copied", str(action.dest), _human(action.source.stat().st_size)))
                else:
                    results.append(("kept", str(action.dest), "already present"))
        except OSError as exc:
            results.append(("failed", str(action.dest), str(exc)))
    return results


def _open_destination_directory(path: Path, env: Environment) -> int:
    """Open/create a destination directory without following any path symlink."""
    drive_c = Path(env.drive_c)
    if not inside_prefix(path, env):
        raise OSError(f"destination outside the prefix: {path}")
    try:
        relative = Path(path).relative_to(drive_c)
    except ValueError as exc:
        raise OSError(f"destination is not beneath drive_c: {path}") from exc
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise OSError(f"unsafe destination path: {path}")
    drive_c.mkdir(parents=True, exist_ok=True)

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    current_fd = os.open(drive_c, flags)
    try:
        for part in relative.parts:
            existing_name = _casefold_existing_name(current_fd, part)
            if existing_name is not None and existing_name != part:
                raise OSError(
                    f"case-insensitive destination already exists as {existing_name!r}: {path}"
                )
            if existing_name is None:
                try:
                    os.mkdir(part, 0o755, dir_fd=current_fd)
                except FileExistsError:
                    pass
                existing_name = _casefold_existing_name(current_fd, part)
                if existing_name != part:
                    raise OSError(
                        f"case-insensitive destination changed during install: {path}"
                    )
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _copy_file_no_follow(
    source: Path, target: Path, env: Environment, *, overwrite: bool = False
) -> bool:
    """Write one regular file through no-follow directory handles.

    Overwrites use a temporary file and an atomic rename, so existing symlinks and hardlinks are
    replaced as directory entries rather than followed or truncated.
    """
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise OSError(f"payload is not a regular file: {source}")
        parent_fd = _open_destination_directory(target.parent, env)
        try:
            existing_name = _casefold_existing_name(parent_fd, target.name)
            if existing_name is not None and existing_name != target.name:
                raise OSError(
                    f"case-insensitive destination already exists as {existing_name!r}: {target}"
                )
            cloexec = getattr(os, "O_CLOEXEC", 0)
            if overwrite:
                try:
                    existing = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISREG(existing.st_mode):
                        raise OSError(f"refusing to overwrite non-regular destination: {target}")
                temporary = f".wpt-{os.getpid()}-{os.urandom(8).hex()}.tmp"
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | cloexec
                temp_fd = os.open(temporary, flags, stat.S_IMODE(source_stat.st_mode), dir_fd=parent_fd)
                try:
                    with os.fdopen(os.dup(source_fd), "rb") as src, os.fdopen(os.dup(temp_fd), "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    os.fchmod(temp_fd, stat.S_IMODE(source_stat.st_mode))
                    os.utime(temp_fd, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
                except BaseException:
                    os.close(temp_fd)
                    try:
                        os.unlink(temporary, dir_fd=parent_fd)
                    except OSError:
                        pass
                    raise
                os.close(temp_fd)
                try:
                    os.replace(temporary, target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                except BaseException:
                    try:
                        os.unlink(temporary, dir_fd=parent_fd)
                    except OSError:
                        pass
                    raise
                return True

            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | cloexec
            try:
                target_fd = os.open(target.name, flags, stat.S_IMODE(source_stat.st_mode),
                                    dir_fd=parent_fd)
            except FileExistsError:
                return False
            try:
                with os.fdopen(os.dup(source_fd), "rb") as src, os.fdopen(os.dup(target_fd), "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.fchmod(target_fd, stat.S_IMODE(source_stat.st_mode))
                os.utime(target_fd, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
            except BaseException:
                os.close(target_fd)
                try:
                    os.unlink(target.name, dir_fd=parent_fd)
                except OSError:
                    pass
                raise
            os.close(target_fd)
            return True
        finally:
            os.close(parent_fd)
    finally:
        os.close(source_fd)


def _casefold_existing_name(parent_fd: int, name: str) -> str | None:
    """Return one matching directory entry, refusing ambiguous case-insensitive spellings."""
    # listdir(fd) advances the directory stream offset on some filesystems. Re-open `.`
    # relative to the trusted directory handle so a later lookup sees newly created entries.
    scan_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    scan_fd = os.open(".", scan_flags, dir_fd=parent_fd)
    try:
        matches = [entry for entry in os.listdir(scan_fd) if entry.casefold() == name.casefold()]
    finally:
        os.close(scan_fd)
    if len(matches) > 1:
        raise OSError(f"ambiguous case-insensitive destination component: {name}")
    return matches[0] if matches else None


def _open_existing_directory_no_follow(path: Path, env: Environment) -> int:
    """Open an existing prefix directory through no-follow directory handles; never create it."""
    path = Path(path)
    if not inside_prefix(path, env):
        raise OSError(f"directory is outside the prefix: {path}")
    drive_c = Path(env.drive_c).absolute()
    try:
        relative = path.relative_to(drive_c)
    except ValueError as exc:
        raise OSError(f"directory is not beneath drive_c: {path}") from exc
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise OSError(f"unsafe directory path: {path}")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    # Match _open_destination_directory: follow legitimate host-side ancestors such as a
    # symlinked $HOME, but never follow a symlink at drive_c or beneath it.
    current_fd = os.open(drive_c, flags)
    try:
        for part in relative.parts:
            existing_name = _casefold_existing_name(current_fd, part)
            if existing_name is None:
                raise FileNotFoundError(f"prefix directory disappeared: {path}")
            next_fd = os.open(existing_name, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _open_existing_file_no_follow(path: Path, env: Environment) -> tuple[int, str, int]:
    """Open a prefix file relative to a pinned, no-follow parent directory."""
    parent_fd = _open_existing_directory_no_follow(path.parent, env)
    try:
        name = _casefold_existing_name(parent_fd, path.name)
        if name is None:
            raise FileNotFoundError(f"prefix file disappeared: {path}")
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        file_fd = os.open(name, flags, dir_fd=parent_fd)
        return parent_fd, name, file_fd
    except BaseException:
        os.close(parent_fd)
        raise


def _rmdir_empty_directory_no_follow(path: Path, env: Environment) -> bool:
    """Remove one empty directory through its pinned, no-follow parent."""
    parent_fd = _open_existing_directory_no_follow(path.parent, env)
    try:
        name = _casefold_existing_name(parent_fd, path.name)
        if name is None:
            return False
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        directory_fd = os.open(name, flags, dir_fd=parent_fd)
        try:
            opened_stat = os.fstat(directory_fd)
        finally:
            os.close(directory_fd)
        current_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISDIR(current_stat.st_mode)
                or (current_stat.st_dev, current_stat.st_ino)
                != (opened_stat.st_dev, opened_stat.st_ino)):
            raise OSError(f"directory changed during pruning: {path}")
        os.rmdir(name, dir_fd=parent_fd)
        return True
    finally:
        os.close(parent_fd)


def _copy_tree_no_clobber(source: Path, dest: Path, env: Environment) -> int:
    return _copy_tree(source, dest, env, overwrite=False)


def _copy_tree_overwrite(source: Path, dest: Path, env: Environment) -> int:
    return _copy_tree(source, dest, env, overwrite=True)


def _copy_tree(source: Path, dest: Path, env: Environment, *, overwrite: bool) -> int:
    if source.is_symlink():
        raise OSError(f"refusing symlink payload root: {source}")
    copied = 0
    dest_fd = _open_destination_directory(dest, env)
    os.close(dest_fd)
    for item in source.rglob("*"):
        if item.is_symlink():
            raise OSError(f"refusing symlink in payload: {item}")
        target = dest / item.relative_to(source)
        if item.is_dir():
            target_fd = _open_destination_directory(target, env)
            os.close(target_fd)
        elif _copy_file_no_follow(item, target, env, overwrite=overwrite):
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


def _find_casefold(path: Path, env: Environment) -> Path | None:
    """Find one on-disk spelling under drive_c without traversing prefix symlinks."""
    candidate = Path(os.path.abspath(path))
    root = Path(os.path.abspath(env.drive_c))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise msi_mod.MsiError(f"MSI path is outside drive_c: {candidate}") from exc
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        current_fd = os.open(root, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise msi_mod.MsiError(f"cannot safely inspect drive_c {root}: {exc}") from exc
    actual_parts: list[str] = []
    try:
        if not relative.parts:
            return root
        for index, part in enumerate(relative.parts):
            try:
                actual = _casefold_existing_name(current_fd, part)
            except OSError as exc:
                raise msi_mod.MsiError(str(exc)) from exc
            if actual is None:
                return None
            try:
                entry_stat = os.stat(actual, dir_fd=current_fd, follow_symlinks=False)
            except OSError as exc:
                location = root.joinpath(*(actual_parts + [actual]))
                raise msi_mod.MsiError(f"cannot safely inspect MSI path {location}: {exc}") from exc
            located = root.joinpath(*(actual_parts + [actual]))
            if stat.S_ISLNK(entry_stat.st_mode):
                raise msi_mod.MsiError(f"refusing symlink in MSI path: {located}")
            actual_parts.append(actual)
            if index < len(relative.parts) - 1:
                if not stat.S_ISDIR(entry_stat.st_mode):
                    raise msi_mod.MsiError(f"MSI path component is not a directory: {located}")
                try:
                    next_fd = os.open(actual, flags, dir_fd=current_fd)
                except OSError as exc:
                    raise msi_mod.MsiError(
                        f"cannot safely open MSI path component {located}: {exc}"
                    ) from exc
                os.close(current_fd)
                current_fd = next_fd
        return root.joinpath(*actual_parts)
    finally:
        os.close(current_fd)


def verify_plan(plan: Plan, env: Environment) -> list[tuple[str, str, str]]:
    """Compare what is now on disk with the sizes the MSI promised.

    This is the acceptance test: the File table's FileSize column is exact, so a
    mismatched or missing file means the install did not really complete.
    """
    rows: list[tuple[str, str, str]] = []
    if plan.owned_files:
        for destination, entry in sorted(plan.owned_files.items(), key=lambda pair: str(pair[0])):
            try:
                located = _find_casefold(destination, env)
            except msi_mod.MsiError as exc:
                rows.append(("ambiguous", str(destination), str(exc)))
                continue
            actual = _safe_size(located) if located and located.is_file() else None
            display = str(located or destination)
            if actual is None:
                rows.append(("missing", display, f"expected {entry.size} bytes"))
            elif actual == entry.size:
                rows.append(("ok", display, _human(actual)))
            else:
                rows.append(("size-mismatch", display,
                             f"on disk {actual}, MSI says {entry.size}"))
        return rows
    for action in plan.actions:
        for target_name, expected_size in plan.expected.items():
            if action.dest.name.lower() != target_name.lower():
                continue
            try:
                located = _find_casefold(action.dest, env)
            except msi_mod.MsiError as exc:
                rows.append(("ambiguous", str(action.dest), str(exc)))
                continue
            if located is None:
                rows.append(("missing", str(action.dest), f"expected {expected_size} bytes"))
                continue
            actual = _measured_size(located, target_name)
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


def filter_needing_repair(plan: Plan, env: Environment) -> Plan:
    """Keep only the destinations that are missing or the wrong size.

    This is what `repair` acts on: a plugin whose registry entry exists but whose
    files vanished (or were half-written) gets exactly those files put back. Existing
    no-clobber targets stay in the plan so verification can report their mismatch, but
    apply_plan preserves them because WPT cannot prove it originally placed them.
    """
    repaired = Plan(msi=plan.msi, identity=plan.identity, expected=plan.expected)
    repaired.warnings = list(plan.warnings)
    if plan.owned_files:
        if plan.destinations_only:
            raise msi_mod.MsiError("cannot repair from a tables-only plan without its payload")
        for destination, entry in plan.owned_files.items():
            located = _find_casefold(destination, env)  # ambiguous spellings refuse repair
            actual = _safe_size(located) if located and located.is_file() else None
            if actual == entry.size:
                continue
            owner = next((action for action in plan.actions
                          if destination == action.dest or destination.is_relative_to(action.dest)), None)
            if owner is None:
                raise msi_mod.MsiError(f"no plan action owns {destination}")
            relative = destination.relative_to(owner.dest)
            source = owner.source / relative if relative.parts else owner.source
            if not source.is_file():
                raise msi_mod.MsiError(f"repair payload is missing: {source}")
            target = located or destination
            repaired.actions.append(Action(source, target, owner.label, owner.no_clobber))
            repaired.owned_files[target] = entry
        return repaired
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
        # bundle was reported as intact while verify_plan flagged it, the filter and the verifier
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


def _protected_removal_root(path: Path, env: Environment) -> bool:
    """A malformed MSI must never make a shared plugin/container root a deletion target."""
    roots = [env.prefix, *(getattr(env, name) for name in PRUNE_STOP if hasattr(env, name))]
    return path.resolve() in {Path(root).resolve() for root in roots}


def _removal_targets(action: Action) -> list[Path]:
    """Paths a destination could exist as: the real name, and the disabled rename.

    Disable/enable renames a plugin by appending `.disabled`, and that applies to a bundle
    directory as much as to a plain file. An uninstall of a plugin that was currently disabled
    therefore used to leave the renamed copy on disk, and `leftovers()` did not report it either
    (found by the review, 2026-09-27): both names, both kinds.
    """
    return [action.dest, action.dest.with_name(action.dest.name + DISABLED_SUFFIX)]


def validate_removal_paths(plan: Plan, env: Environment) -> None:
    """Fail closed if any planned uninstall path is ambiguous or crosses a prefix symlink."""
    paths: set[Path] = set()
    for action in plan.actions:
        paths.update(_removal_targets(action))
        for owned in plan.owned_files:
            if owned == action.dest or owned.is_relative_to(action.dest):
                paths.add(owned)
                paths.add(owned.with_name(owned.name + DISABLED_SUFFIX))
    for path in sorted(paths, key=str):
        _find_casefold(path, env)


def _prune_empty_parents(parent: Path, env: Environment) -> int:
    """Remove now-empty directories above a deleted file, never the roots themselves."""
    try:
        stops = {str(Path(getattr(env, name)).resolve()).casefold()
                 for name in PRUNE_STOP if hasattr(env, name)}
        drive_c = str(Path(env.drive_c).resolve()).casefold()
    except OSError:
        return 0  # cannot establish the stop boundaries, so do not prune
    removed = 0
    while inside_prefix(parent, env):
        try:
            identity = str(parent.resolve()).casefold()
        except OSError:
            break
        if identity in stops or identity == drive_c:
            break
        try:
            if not _rmdir_empty_directory_no_follow(parent, env):
                break
        except OSError:
            break
        removed += 1
        parent = parent.parent
    return removed


def remove_files(plan: Plan, env: Environment, dry_run: bool = False) -> list[tuple[str, str, str]]:
    """Unlink exact File-table files only, preserving unowned content in directories.

    This is the uninstall route for a prefix where the product was never registered
    with Windows Installer. A missing manifest refuses removal, rather than deleting
    a whole bundle on faith. Wine's msiexec remains a separate process with its own
    possible side effects.
    """
    if not plan.owned_files:
        return [("refused", str(action.dest), "no exact File-table manifest; nothing removed")
                for action in plan.actions]
    try:
        validate_removal_paths(plan, env)
    except (OSError, msi_mod.MsiError) as exc:
        return [("refused", "", f"unsafe or ambiguous uninstall path: {exc}")]
    return _remove_owned_files(plan, env, dry_run)


def _same_file_contents(left: Path, right: Path | int) -> bool:
    """Compare payload and installed bytes; an int is an already-open no-follow fd."""
    with left.open("rb") as source:
        installed_context = os.fdopen(os.dup(right), "rb") if isinstance(right, int) else right.open("rb")
        with installed_context as installed:
            while True:
                source_chunk = source.read(1024 * 1024)
                installed_chunk = installed.read(1024 * 1024)
                if source_chunk != installed_chunk:
                    return False
                if not source_chunk:
                    return True


def _remove_owned_files(plan: Plan, env: Environment, dry_run: bool) -> list[tuple[str, str, str]]:
    """Unlink only File-table paths; never recursively delete a planned directory."""
    rows: list[tuple[str, str, str]] = []
    emptied: set[Path] = set()
    for action in plan.actions:
        owned = sorted((path for path in plan.owned_files
                        if path == action.dest or path.is_relative_to(action.dest)), key=str)
        if not owned:
            rows.append(("refused", str(action.dest), "no exact File-table row for this action"))
            continue
        for target_name in _removal_targets(action):
            try:
                target = _find_casefold(target_name, env)
            except msi_mod.MsiError as exc:
                rows.append(("refused", str(target_name), str(exc)))
                continue
            except OSError as exc:
                rows.append(("failed", str(target_name), str(exc)))
                continue
            if target is None:
                continue
            for original in owned:
                relative = original.relative_to(action.dest)
                named = target / relative if relative.parts else target
                # Disable/enable can rename the binary *inside* a bundle.
                for candidate_name in (named, named.with_name(named.name + DISABLED_SUFFIX)):
                    try:
                        candidate = _find_casefold(candidate_name, env)
                    except msi_mod.MsiError as exc:
                        rows.append(("refused", str(candidate_name), str(exc)))
                        continue
                    except OSError as exc:
                        rows.append(("failed", str(candidate_name), str(exc)))
                        continue
                    if candidate is None:
                        continue
                    if not inside_prefix(candidate, env) or _protected_removal_root(candidate, env):
                        rows.append(("refused", str(candidate), "outside the prefix or a shared root"))
                        continue
                    if action.no_clobber:
                        rows.append(("refused", str(candidate),
                                     "no-clobber install may have preserved a pre-existing file; preserved"))
                        continue
                    entry = plan.owned_files[original]
                    if entry.removal_state_uncertain:
                        rows.append(("refused", str(candidate),
                                     f"MSI component state is uncertain ({entry.removal_state_reason}); preserved"))
                        continue
                    if ownership_key(original) in plan.shared_paths:
                        rows.append(("refused", str(candidate),
                                     "also claimed by another cached MSI; preserved"))
                        continue
                    if plan.destinations_only:
                        rows.append(("refused", str(candidate),
                                     "MSI payload bytes are unavailable; size alone cannot verify "
                                     "the file is unchanged, so it was preserved"))
                        continue
                    payload = action.source / relative if relative.parts else action.source
                    if payload.is_symlink() or not payload.is_file():
                        rows.append(("failed", str(candidate),
                                     f"MSI payload file is unavailable for verification: {payload}"))
                        continue
                    try:
                        parent_fd, actual_name, file_fd = _open_existing_file_no_follow(candidate, env)
                    except OSError as exc:
                        rows.append(("refused", str(candidate),
                                     f"could not safely open candidate without following symlinks: {exc}"))
                        continue
                    try:
                        opened_stat = os.fstat(file_fd)
                        if not stat.S_ISREG(opened_stat.st_mode):
                            rows.append(("refused", str(candidate), "expected an MSI-owned regular file"))
                            continue
                        actual_size = opened_stat.st_size
                        expected_size = entry.size
                        if actual_size != expected_size:
                            rows.append(("refused", str(candidate),
                                         f"size differs from MSI ({actual_size} vs {expected_size}); "
                                         "preserved for manual review"))
                            continue
                        if not _same_file_contents(payload, file_fd):
                            rows.append(("refused", str(candidate),
                                         "contents differ from the MSI payload; preserved"))
                            continue
                        try:
                            current_stat = os.stat(actual_name, dir_fd=parent_fd, follow_symlinks=False)
                        except OSError as exc:
                            rows.append(("refused", str(candidate),
                                         f"candidate changed during verification; preserved: {exc}"))
                            continue
                        if (not stat.S_ISREG(current_stat.st_mode)
                                or (current_stat.st_dev, current_stat.st_ino)
                                != (opened_stat.st_dev, opened_stat.st_ino)):
                            rows.append(("refused", str(candidate),
                                         "candidate changed during verification; preserved"))
                            continue
                        if dry_run:
                            rows.append(("dry-run", str(candidate), "would remove this File-table file"))
                            continue
                        os.unlink(actual_name, dir_fd=parent_fd)
                        rows.append(("removed", str(candidate), _human(actual_size)))
                        emptied.add(candidate.parent)
                    except OSError as exc:
                        rows.append(("failed", str(candidate), str(exc)))
                    finally:
                        os.close(file_fd)
                        os.close(parent_fd)
            if not dry_run and not plan.destinations_only and target.is_dir():
                try:
                    remaining_count = sum(1 for child in target.rglob("*") if child.is_file())
                except OSError as exc:
                    rows.append(("failed", str(target), f"could not enumerate remaining files: {exc}"))
                else:
                    if remaining_count:
                        rows.append(("kept", str(target), f"{remaining_count} remaining file(s) left in place"))
    if not dry_run:
        pruned = sum(_prune_empty_parents(parent, env)
                     for parent in sorted(emptied, key=lambda p: -len(p.parts)))
        if pruned:
            rows.append(("pruned", "", f"{pruned} empty director{'y' if pruned == 1 else 'ies'} removed"))
    return rows


def leftovers(plan: Plan, env: Environment) -> list[Path]:
    """MSI-owned files still present after removal; ignore preserved unowned user files."""
    remaining: list[Path] = []
    if plan.owned_files:
        for action in plan.actions:
            owned = [path for path in plan.owned_files
                     if path == action.dest or path.is_relative_to(action.dest)]
            for target_name in _removal_targets(action):
                try:
                    target = _find_casefold(target_name, env)
                except msi_mod.MsiError:
                    remaining.append(target_name)  # ambiguity is not a successful uninstall
                    continue
                except OSError:
                    remaining.append(target_name)  # inaccessible paths are not proven absent
                    continue
                if target is None:
                    continue
                for original in owned:
                    relative = original.relative_to(action.dest)
                    named = target / relative if relative.parts else target
                    for candidate_name in (named, named.with_name(named.name + DISABLED_SUFFIX)):
                        try:
                            candidate = _find_casefold(candidate_name, env)
                        except msi_mod.MsiError:
                            remaining.append(candidate_name)
                            continue
                        except OSError:
                            remaining.append(candidate_name)
                            continue
                        if candidate and inside_prefix(candidate, env):
                            remaining.append(candidate)
        return remaining
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
    owned_paths = {str(path).casefold() for path in plan.owned_files}
    filenames = {path.name.casefold() for path in plan.owned_files}
    code = (product_code or "").strip("{}").upper()

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
                if str(path).casefold() in owned_paths and inside_prefix(path, env):
                    try:
                        still_present = _find_casefold(path, env) is not None
                    except msi_mod.MsiError:
                        still_present = True  # an ambiguous path cannot be safely purged
                    if still_present:
                        continue
                    reason = f"points at missing MSI-owned file {path.name}"
                elif path.name.casefold() in filenames:
                    # The basename alone is never ownership proof, even inside a planned bundle.
                    unsure.add(path.name)
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
            if not inside_prefix(target, env):
                rows.append(("refused", str(path), "outside the prefix - not renamed"))
                continue
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
    vendor wrapper) there is nothing left to derive a File table from, so the toolkit cannot
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