"""Reading vendor MSIs with msitools -- no Wine, no Windows Installer.

Every function here shells out to `msiinfo` / `msiextract` from the msitools
package, which can read and unpack an MSI on Linux. That is what lets the tool
install plugins on a prefix where the vendor's own installer refuses to run.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

TABLES_NEEDED = ("msiinfo", "msiextract")


class MsiError(RuntimeError):
    pass


def missing_tools() -> list[str]:
    return [tool for tool in TABLES_NEEDED if shutil.which(tool) is None]


def require_tools() -> None:
    missing = missing_tools()
    if missing:
        raise MsiError(
            "msitools is missing: "
            + ", ".join(missing)
            + "  --  install it with 'sudo pacman -S msitools' (Arch) "
            "or 'sudo apt install msitools' (Debian/Ubuntu)"
        )


def _run(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    require_tools()
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise MsiError(f"{args[0]} did not finish within {timeout}s reading this installer "
                       ", the file may be corrupt or on a stalled mount") from exc
    except OSError as exc:
        raise MsiError(f"could not run {args[0]}: {exc}") from exc
    if proc.returncode != 0 and "table not found" not in (proc.stderr or ""):
        raise MsiError(f"{' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc


def export_table(msi: Path, table: str) -> list[list[str]]:
    """Dump one MSI table as rows of cells. Empty list if the table is absent."""
    proc = _run(["msiinfo", "export", str(msi), table])
    rows: list[list[str]] = []
    for line in proc.stdout.splitlines():
        line = line.rstrip("\r")
        if not line.strip():
            continue
        rows.append(line.split("\t"))
    # first two rows are the column name header and the type header
    return rows[2:] if len(rows) >= 2 else []


def tables(msi: Path) -> list[str]:
    proc = _run(["msiinfo", "tables", str(msi)])
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


@dataclass
class MsiIdentity:
    product_name: str = ""
    product_version: str = ""
    product_code: str = ""
    upgrade_code: str = ""
    manufacturer: str = ""
    properties: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        bits = [self.product_name or "unknown product"]
        if self.product_version:
            bits.append(self.product_version)
        return " ".join(bits)


def identity(msi: Path) -> MsiIdentity:
    """ProductName / ProductVersion / ProductCode / UpgradeCode / Manufacturer."""
    props: dict[str, str] = {}
    for row in export_table(msi, "Property"):
        if len(row) >= 2:
            props[row[0]] = row[1]
    return MsiIdentity(
        product_name=props.get("ProductName", ""),
        product_version=props.get("ProductVersion", ""),
        product_code=props.get("ProductCode", ""),
        upgrade_code=props.get("UpgradeCode", ""),
        manufacturer=props.get("Manufacturer", ""),
        properties=props,
    )


def directory_properties(msi: Path) -> dict[str, str]:
    """Windows install targets declared by the MSI, e.g. VST3DIR -> C:\\...\\VST3."""
    return {
        key: value
        for key, value in identity(msi).properties.items()
        if key.endswith("DIR") and value.upper().startswith("C:\\")
    }


def launch_conditions(msi: Path) -> list[tuple[str, str]]:
    """The LaunchCondition table as (condition, description) pairs.

    A condition that Wine cannot satisfy is the usual reason `msiexec /i` ends
    in 1603 -- e.g. Neural DSP's 'SETUPEXEDIR OR (REMOVE="ALL")', which means
    'this package can only be run from a bootstrapper'.
    """
    pairs: list[tuple[str, str]] = []
    for row in export_table(msi, "LaunchCondition"):
        if len(row) >= 2:
            pairs.append((row[0], row[1]))
    return pairs


def bootstrap_only(msi: Path) -> tuple[str, str] | None:
    """Return the bootstrapper-only launch condition, if the MSI has one."""
    for condition, description in launch_conditions(msi):
        if "SETUPEXEDIR" in condition.upper():
            return condition, description
    return None


def watcher_conditions(msi: Path) -> list[tuple[str, str]]:
    """Launch conditions unrelated to Windows version gates (the interesting ones)."""
    interesting: list[tuple[str, str]] = []
    for condition, description in launch_conditions(msi):
        upper = condition.upper()
        if "VERSIONNT" in upper and "SETUPEXEDIR" not in upper:
            continue
        interesting.append((condition, description))
    return interesting


@dataclass
class PayloadFile:
    name: str          # long file name, as it lands on disk
    size: int          # bytes, from the File table -- the acceptance criterion
    component: str
    directory: str = ""


def payload_files(msi: Path) -> list[PayloadFile]:
    """The File table: what the MSI says it will write, and how big each file is.

    The FileSize column is what makes verification possible -- if a file on disk
    has a different size, the install did not really complete.
    """
    # Read the Component table *once*. It used to be exported inside the loop, which meant one
    # msitools process per File row - hundreds of them for a real installer, for a value that is
    # only informational.
    try:
        directory_of = {r[0]: r[2] for r in export_table(msi, "Component") if len(r) >= 3}
    except MsiError:
        directory_of = {}

    out: list[PayloadFile] = []
    for row in export_table(msi, "File"):
        if len(row) < 4:
            continue
        raw_name = row[2]
        long_name = raw_name.split("|")[-1] if "|" in raw_name else raw_name
        try:
            size = int(row[3])
        except ValueError:
            size = 0
        out.append(PayloadFile(name=long_name, size=size, component=row[1],
                               directory=directory_of.get(row[1], "")))
    return out


# Reading a File table means shelling out to msitools, and the same MSI gets asked for the
# same table several times per command (inventory builds a size index *and* an owner index).
# Wine Mono's 83 MB MSI alone costs 20 s. Cache by path + mtime + size so a rebuilt or
# replaced MSI is re-read rather than served stale.
_TABLE_CACHE: dict[tuple[str, float, int], dict[str, int]] = {}


def expected_sizes(msi: Path, use_cache: bool = True) -> dict[str, int]:
    msi = Path(msi)
    key = None
    if use_cache:
        try:
            stat = msi.stat()
            key = (str(msi), stat.st_mtime, stat.st_size)
        except OSError:
            key = None
        if key is not None and key in _TABLE_CACHE:
            return _TABLE_CACHE[key]
    sizes = {f.name: f.size for f in payload_files(msi)}
    if key is not None:
        _TABLE_CACHE[key] = sizes
    return sizes


# The directory properties these vendors' installers declare for their payload. An MSI that
# declares one of these is a plugin product's MSI; one that declares none is a runtime,
# a driver or a service (Bonjour, PACE, Wine Mono all declare none, checked 2026-09-26).
VENDOR_PAYLOAD_DIRS = ("VST3DIR", "VSTDIR", "AAXDIR", "APPDIR", "PREDIR")

_PAYLOAD_DECLARED: dict[tuple[str, float, int], bool] = {}


def declares_plugin_payload(msi: Path) -> bool:
    """True when an MSI's own Directory table names a plugin payload directory.

    This is the honest way to tell a plugin's MSI from the prefix's housekeeping ones: their
    contents were inspected rather than their names guessed, and it is the same table the
    installer half uses to decide where a file goes.
    """
    msi = Path(msi)
    try:
        stat = msi.stat()
        key = (str(msi), stat.st_mtime, stat.st_size)
    except OSError:
        key = None
    if key is not None and key in _PAYLOAD_DECLARED:
        return _PAYLOAD_DECLARED[key]
    try:
        rows = export_table(msi, "Directory")
        names = {row[0] for row in rows if row}
        verdict = bool(names.intersection(VENDOR_PAYLOAD_DIRS))
    except Exception:  # noqa: BLE001 - an unreadable MSI is not a plugin MSI
        verdict = False
    if key is not None:
        _PAYLOAD_DECLARED[key] = verdict
    return verdict


def find_extracted_msis(
    prefix: Path, hint: str | None = None, include_installer_cache: bool = False
) -> list[Path]:
    """Locate the MSIs vendor installers unpack inside the prefix.

    Neural DSP wrappers drop theirs in
    <prefix>/drive_c/users/<user>/AppData/Roaming/Neural DSP/<Product> <ver>/install/.
    Others end up in ProgramData/Package Cache/{GUID}/.

    `include_installer_cache` adds **Wine's own msiexec cache**, `drive_c/windows/Installer/`,
    where Windows Installer keeps a copy of every MSI a product registered with it. That is
    the only copy some products leave behind: a product installed by running its vendor wrapper
    vendor wrapper without ever writing an MSI into its own vendor folder, so its
    `Installer/d80a.msi` is what makes it verifiable, repairable and removable at all
    (found 2026-09-26, after the GUI greyed the uninstall button out for it).

    It is off by default because that directory also holds the prefix's runtimes (PACE,
    Wine Mono, Bonjour), which are noise when the caller only wants vendor plugin MSIs.
    """
    bases = ["drive_c/users", "drive_c/ProgramData/Package Cache"]
    if include_installer_cache:
        bases.append("drive_c/windows/Installer")
    found: list[Path] = []
    for base in bases:
        root = prefix / base
        if not root.is_dir():
            continue
        for path in root.rglob("*.msi"):
            if hint and hint.lower() not in str(path).lower():
                continue
            if base.endswith("windows/Installer") and not declares_plugin_payload(path):
                # the prefix's runtimes, drivers and services live here too (Wine Mono's
                # 83 MB MSI costs 20 s to read and owns no plugin file) - skip them before
                # anyone reads a File table
                continue
            found.append(path)
    def _stamp(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0          # unreadable now; keep it in the list but sort it last

    return sorted(set(found), key=_stamp, reverse=True)


def media_cabinets(msi: Path) -> list[str]:
    """The cabinet filenames this package's Media table declares.

    A package's payload cabinet is named by the MSI, not after it: the Fortin Cali Suite package
    is `abba.msi` in Wine's installer cache and its payload is `Fortin Cali Suite1.cab`. Guessing
    sidecars from the MSI's own filename finds nothing, and the uninstall then fails to read a
    file list that is perfectly readable (review, 2026-09-27). Embedded cabinets (`#name`) are not
    files to look for, so they are skipped.
    """
    try:
        rows = export_table(msi, "Media")
    except Exception:                      # noqa: BLE001 - an unreadable Media table is not fatal here
        return []
    names: list[str] = []
    for row in rows[1:]:                   # row 0 is the header
        for cell in row:
            name = Path(cell).name
            if not name or name.startswith("#"):
                continue
            if name.lower().endswith(".cab") and name not in names:
                names.append(name)
    return names


def cabinet_search_roots(msi: Path) -> list[Path]:
    """Where a package's cabinet can be, given where its MSI is.

    Vendors leave the cabinet next to the MSI they ship, but Wine's installer cache keeps only the
    MSI: the wrapper's install directory is where the payload stays, so those are searched too.
    """
    roots = [msi.parent]
    parts = msi.resolve().parts
    if "drive_c" in parts:
        drive_c = Path(*parts[: parts.index("drive_c") + 1])
        roots += [drive_c / "windows" / "Installer", drive_c / "ProgramData", drive_c / "users"]
    return roots


def find_cabinet(name: str, roots: list[Path]) -> Path | None:
    """The first file called `name` under any of `roots`."""
    for root in roots:
        if not root.is_dir():
            continue
        for candidate in sorted(root.rglob(name)):
            if candidate.is_file():
                return candidate
    return None


def stage_msi(msi: Path, scratch: Path, cabinets: list[str] | None = None) -> Path:
    """Copy an MSI somewhere safe *before* anything can delete it.

    `msiexec /x` removes the copy of the package Windows Installer keeps in
    `drive_c/windows/Installer/`, and on this stack that cache is often the only copy a product
    has. Read the file list after running msiexec and it is simply not there any more: that is
    exactly what happens on an uninstall, which removed
    `d80a.msi` and then failed to read it, leaving the presets and registry entries behind.

    The copy goes to a **sibling** of the scratch dir, because `extract()` empties its scratch
    before every extraction.
    """
    msi = Path(msi)
    scratch = Path(scratch)
    unpack_root = scratch.parent / f"{scratch.name}-unpack"
    if msi.resolve().is_relative_to(unpack_root.resolve()):
        # Already staged. The uninstall job is handed whatever copy still exists, and after
        # `msiexec /x` that can be the staged one - re-staging it must return it unchanged rather
        # than bury a copy one level deeper.
        return msi
    # One directory per staged MSI. A single shared directory let two packages collide: a sidecar
    # of the same name from a different product overwrote the staged one whenever the sizes
    # differed, and the staged copy is what matters here precisely because `msiexec /x` may have
    # already deleted every other copy of the package. Keyed on the resolved path, so the same MSI
    # reuses its directory instead of accumulating them.
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", msi.stem).strip("._")[:60] or "msi"
    digest = hashlib.sha1(str(msi.resolve()).encode("utf-8")).hexdigest()[:8]
    keep = unpack_root / "msi" / f"{stem}-{digest}"
    keep.mkdir(parents=True, exist_ok=True)
    target = keep / msi.name
    if not (target.exists() and target.stat().st_size == msi.stat().st_size):
        shutil.copy2(msi, target)

    # Bring the sidecars. These MSIs keep their payload in a cabinet *next to* the .msi
    # (Neural DSP: "Archetype Nolly X.msi" + "Archetype Nolly X1.cab"), so copying the MSI
    # alone gives a file list nobody can extract: msiextract then fails with
    # "Error opening file ...1.cab: No such file or directory" and an uninstall dies after
    # every check has passed. Found by the full health check, 2026-09-26.
    for sibling in sorted(msi.parent.iterdir()):
        if sibling == msi or not sibling.is_file():
            continue
        related = sibling.suffix.lower() == ".cab" or sibling.name.lower().startswith(msi.stem.lower())
        if not related:
            continue
        copy = keep / sibling.name
        if not (copy.exists() and copy.stat().st_size == sibling.stat().st_size):
            shutil.copy2(sibling, copy)

    # Then the cabinets this MSI actually names, wherever they are on the machine. Wine's installer
    # cache keeps the MSI without its payload - the cabinet stays in the wrapper's own install
    # directory - so a cached copy could not be read at all until this looked for it. Found on a
    # real prefix: the Tim Henson package's cabinet sat 400 MB away in
    # `AppData/Roaming/Neural DSP/.../install/`, and the uninstall refused for want of it.
    wanted = media_cabinets(msi) if cabinets is None else list(cabinets)
    absent: list[str] = []
    for name in wanted:
        copy = keep / name
        if copy.is_file():
            continue
        source = msi.parent / name
        if not source.is_file():
            source = find_cabinet(name, cabinet_search_roots(msi)[1:])
        if source is None:
            absent.append(name)
            continue
        if not (copy.exists() and copy.stat().st_size == source.stat().st_size):
            shutil.copy2(source, copy)
    if absent:
        # Refuse with something actionable: without the cabinet there is no file list, and a file
        # list is what every removal path is built on.
        raise MsiError(
            f"the cabinet {', '.join(absent)} that {msi.name} needs is not on this machine any "
            "more, so its file list cannot be read and nothing has been removed. Running the "
            "vendor's installer again brings the cabinet back"
        )
    return target


EXTRACT_MARKER = ".wpt-extract"


def prepare_scratch(dest: Path) -> Path:
    """Make sure the scratch dir holds nothing but this extraction.

    Anything left over from a previous product in the same scratch directory becomes
    part of the next plan: the plan is built by listing the extracted folders, so
    stale payload would be **copied into the prefix** on install and **deleted from
    it** on uninstall. Clearing it is what keeps a plan to one product.

    A directory is only wiped when it is ours (marker file present) or when it lives
    under the system temp dir / ~/.cache, so an explicit `--scratch` pointing at real
    data is refused rather than emptied.
    """
    dest = Path(dest)
    resolved = dest.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    # `Path.is_relative_to` is True for *equal* paths, so without this the shared temp directory
    # itself counted as "scratch-like" and `--scratch /tmp` would delete every other program's
    # live temp files. The temp root and the home directory are never ours to remove.
    protected = {Path("/"), temp_root, Path.home().resolve(), Path.home().resolve() / ".cache"}
    if resolved in protected:
        raise MsiError(f"{dest} is a shared directory, not a scratch directory - refusing to empty it")
    if dest.exists() and any(dest.iterdir()):
        ours = (dest / EXTRACT_MARKER).is_file()
        scratch_like = (resolved != temp_root and resolved.is_relative_to(temp_root)) or (
            Path.home().resolve() / ".cache" in resolved.parents
        )
        if not (ours or scratch_like):
            raise MsiError(
                f"{dest} is not empty and was not created by wpt - "
                "pick an empty --scratch directory (leftovers there would end up in the plan)"
            )
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / EXTRACT_MARKER).write_text("wpt scratch; safe to delete\n")
    return dest


def extract(msi: Path, dest: Path, *, clean: bool = True) -> Path:
    """Unpack an MSI's payload with msiextract. Returns the extraction root.

    The scratch directory is emptied first (see `prepare_scratch`): a stale payload
    from another product would otherwise join this plan.
    """
    require_tools()
    dest = Path(dest)
    if clean:
        prepare_scratch(dest)
    else:
        dest.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["msiextract", "-C", str(dest), str(msi)], capture_output=True, text=True, timeout=900
        )
    except subprocess.TimeoutExpired as exc:
        raise MsiError("msiextract did not finish within 900s: a 400 MB payload on a slow disk can "
                       "take a while, but this usually means the installer is corrupt") from exc
    except OSError as exc:
        raise MsiError(f"could not run msiextract: {exc}") from exc
    if proc.returncode != 0:
        raise MsiError(f"msiextract failed ({proc.returncode}): {proc.stderr.strip()}")
    return dest


def extracted_root_dirs(root: Path) -> list[str]:
    """The MSI directory names msiextract created, e.g. VST3DIR, APPDIR:./Product."""
    if not root.is_dir():
        return []
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir() and p.name != EXTRACT_MARKER and not p.name.startswith(".")
    )