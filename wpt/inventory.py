"""Inventory: what is actually installed in this prefix, and is it intact?

The installer half of the toolkit puts files where they belong. This half answers
the other question -- *what is in here* -- by walking the plugin directories,
matching what it finds against the vendor MSIs cached inside the prefix, and
flagging anything whose size disagrees with what the MSI promised.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import msi as msi_mod
from .environment import Environment
from .installer import DISABLED_SUFFIX

VST3_SUFFIX = ".vst3"
VST2_SUFFIX = ".dll"
AAX_SUFFIX = ".aaxplugin"
STANDALONE_SUFFIX = ".exe"

KIND_ORDER = {"vst3": 0, "vst2": 1, "aax": 2, "standalone": 3, "other": 4}


@dataclass
class PluginEntry:
    name: str
    path: Path
    kind: str
    size: int
    mtime: float
    expected_size: int | None = None
    msi: Path | None = None
    disabled: bool = False

    @property
    def integrity(self) -> str:
        if self.expected_size is None:
            return "unverified"
        return "ok" if self.size == self.expected_size else "size-mismatch"

    @property
    def broken(self) -> bool:
        return self.integrity == "size-mismatch"


@dataclass
class Inventory:
    entries: list[PluginEntry] = field(default_factory=list)
    msis: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[PluginEntry]:
        return [e for e in self.entries if e.kind == kind]

    @property
    def broken(self) -> list[PluginEntry]:
        return [e for e in self.entries if e.broken]

    @property
    def disabled(self) -> list[PluginEntry]:
        return [e for e in self.entries if e.disabled]

    @property
    def unowned_standalone(self) -> list[PluginEntry]:
        """Executables in Program Files that no plugin MSI describes.

        Wine's own stubs (`iexplore.exe`, `wordpad.exe`, `wmplayer.exe`) and the helpers other
        vendors drop there (Bonjour's `mDNSResponder.exe`, iLok's tools) are not plugins: they
        are behind no plugin MSI, which is exactly how this is decided. They are counted and
        can be listed with `--all-standalone`, but they are not what the inventory is for.
        """
        return [e for e in self.entries if e.kind == "standalone" and e.msi is None]

    @property
    def plugins(self) -> list[PluginEntry]:
        """Everything except execs nothing claims."""
        unowned = {id(e) for e in self.unowned_standalone}
        return [e for e in self.entries if id(e) not in unowned]

    def names(self) -> list[str]:
        return sorted({e.name for e in self.entries})


def _classify(path: Path, env: Environment) -> str:
    """Decide what kind of plugin a file is from where it lives."""
    try:
        path.relative_to(env.vst3_dir)
        return "vst3"
    except ValueError:
        pass
    try:
        path.relative_to(env.vst2_dir)
        return "vst2" if path.suffix.lower() == VST2_SUFFIX else "other"
    except ValueError:
        pass
    if path.name.lower().endswith(AAX_SUFFIX):
        return "aax"
    try:
        path.relative_to(env.program_files)
    except ValueError:
        return "other"
    return "standalone" if path.suffix.lower() == STANDALONE_SUFFIX else "other"


def _msi_indexes(msis: list[Path]) -> tuple[dict[str, int], dict[str, Path]]:
    """One pass over the cached MSIs: expected size *and* owning MSI per file name.

    Both answers come from the same File table, and reading that table is the expensive part
    (it shells out to msitools), so they are built together rather than in two loops — that
    halving is what took `wpt list` from 54 s back to single figures once Wine's installer
    cache joined the search.
    """
    sizes: dict[str, int] = {}
    owners: dict[str, Path] = {}
    for msi in msis:
        try:
            table = msi_mod.expected_sizes(msi)
        except Exception:  # noqa: BLE001 - one unreadable MSI must not sink the listing
            continue
        for name, size in table.items():
            key = name.lower()
            sizes.setdefault(key, size)
            owners.setdefault(key, msi)
    return sizes, owners


def build(env: Environment, include_standalone: bool = True) -> Inventory:
    inv = Inventory()
    # the installer cache too: some products only leave their MSI there, and without it
    # they read as 'unverified' and cannot be repaired or uninstalled from the GUI
    inv.msis = msi_mod.find_extracted_msis(env.prefix, include_installer_cache=True)
    index, owners = _msi_indexes(inv.msis)

    roots: list[tuple[Path, str]] = [
        (env.vst3_dir, VST3_SUFFIX),
        (env.vst2_dir, VST2_SUFFIX),
        (env.aax_dir, AAX_SUFFIX),
    ]
    if include_standalone:
        roots.append((env.program_files, STANDALONE_SUFFIX))

    seen: set[Path] = set()
    for root, suffix in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path in seen:
                continue
            # a disabled plugin is renamed 'X.vst3.disabled': still real, just hidden
            disabled = path.name.lower().endswith(DISABLED_SUFFIX)
            base = path.name[: -len(DISABLED_SUFFIX)] if disabled else path.name
            if not base.lower().endswith(suffix):
                continue
            kind = _classify(path.with_name(base), env)
            if kind == "other":
                continue
            seen.add(path)
            stat = path.stat()
            expected = index.get(base.lower())
            inv.entries.append(
                PluginEntry(
                    name=path.name,
                    path=path,
                    kind=kind,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    expected_size=expected,
                    msi=owners.get(base.lower()),
                    disabled=disabled,
                )
            )

    if not inv.entries:
        inv.warnings.append(
            f"no plugins found under {env.vst3_dir} or {env.vst2_dir} - is that the right prefix?"
        )
    if not inv.msis:
        inv.warnings.append(
            "no vendor MSIs cached in the prefix, so sizes cannot be cross-checked "
            "(the wrapper only leaves one behind when it has been run)"
        )
    inv.entries.sort(key=lambda e: (KIND_ORDER.get(e.kind, 9), e.name.lower()))
    return inv


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def render(inv: Inventory, limit: int = 200, all_standalone: bool = False) -> str:
    entries = inv.entries if all_standalone else inv.plugins
    lines: list[str] = []
    counts = {kind: len(inv.by_kind(kind)) for kind in ("vst3", "vst2", "aax", "standalone")}
    lines.append(
        "installed  "
        + "  ".join(f"{kind}: {count}" for kind, count in counts.items())
        + f"   (total {len(inv.entries)})"
        + (f"   disabled: {len(inv.disabled)}" if inv.disabled else "")
    )
    if inv.msis:
        lines.append(f"cached MSIs: {len(inv.msis)}")
    lines.append("")
    width = max((len(e.name) for e in entries), default=0)
    for entry in entries[:limit]:
        flag = {"ok": "ok ", "unverified": "?  ", "size-mismatch": "BAD"}[entry.integrity]
        expected = f" (MSI says {_human(entry.expected_size)})" if entry.expected_size else ""
        lines.append(f"  {flag} {entry.kind:10} {entry.name.ljust(width)}  {_human(entry.size)}{expected}")
    if len(entries) > limit:
        lines.append(f"  ... and {len(entries) - limit} more")
    if not all_standalone and inv.unowned_standalone:
        lines.append(
            f"  ({len(inv.unowned_standalone)} other .exe file(s) in Program Files belong to no plugin "
            f"MSI - Wine's own tools and other vendors' helpers; --all-standalone lists them)"
        )
    for warning in inv.warnings:
        lines.append(f"  ! {warning}")
    if inv.broken:
        lines.append("")
        lines.append(f"{len(inv.broken)} file(s) disagree with the MSI they came from:")
        for entry in inv.broken:
            lines.append(f"  ! {entry.path}")
    return "\n".join(lines)