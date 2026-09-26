"""Diagnostics: does the prefix actually contain what its registry claims?

The failure this exists for: a vendor MSI registers the product, then fails to
copy any files. The registry says "installed", the plugin folder is empty, and
the DAW silently shows nothing. Scanning the registry for plugin paths and
checking them against the disk finds that in a second -- and can't be fooled by
an exit code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .environment import Environment

VALUE_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"="(.*)"\s*$')

PLUGIN_MARKERS = ("VST3", "VSTPLUGINS", "PLUG-INS", "AAXPLUGIN", "PLUGIN")
PLUGIN_EXTENSIONS = (".vst3", ".dll", ".aaxplugin", ".vst2")
PRODUCT_KEYS = ("DisplayName", "DisplayVersion", "InstallLocation", "UninstallString")

# Registered products that are runtime noise, not the user's plugins. Listing
# forty Visual C++ redistributables buries the four entries anyone cares about.
SYSTEM_PRODUCT_PREFIXES = (
    "microsoft visual c++",
    "microsoft edge webview2",
    "microsoft .net",
    "wine mono",
    "my computer",
    "local intranet",
    "trusted sites",
    "internet",
    "restricted sites",
)


def is_system_product(name: str) -> bool:
    return name.strip().lower().startswith(SYSTEM_PRODUCT_PREFIXES)


@dataclass
class Entry:
    raw: str
    path: Path
    kind: str  # "plugin" | "dir" | "other"
    exists: bool
    source: str


@dataclass
class Report:
    entries: list[Entry] = field(default_factory=list)
    products: list[dict[str, str]] = field(default_factory=list)
    scanned_files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def missing(self) -> list[Entry]:
        return [e for e in self.entries if not e.exists]

    @property
    def present(self) -> list[Entry]:
        return [e for e in self.entries if e.exists]


def _decode(value: str) -> str:
    """Undo Wine .reg escaping. `str(2):"hex"` values are UTF-16LE blobs."""
    value = value.strip()
    if value.lower().startswith("str(2):"):
        hexed = value.split(":", 1)[1].strip().strip('"')
        try:
            return bytes.fromhex(hexed).decode("utf-16-le", errors="replace")
        except ValueError:
            return ""
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value.replace("\\\\", "\\").replace('\\"', '"')


def _to_linux(env: Environment, windows_path: str) -> Path | None:
    """C:\\Program Files\\X -> <prefix>/drive_c/Program Files/X"""
    cleaned = windows_path.strip().strip('"')
    if not cleaned.upper().startswith("C:\\"):
        return None
    cleaned = cleaned[3:].replace("\\", "/").rstrip("/")
    if not cleaned:
        return None
    return env.drive_c / cleaned


def _classify(windows_path: str) -> str:
    """Only paths *inside a plugin directory* count as plugin paths.

    The check looks at the directory part only. Matching on the whole path is a
    trap: '.vst3' contains 'VST3', so extension matching would classify anything
    with that extension as a plugin and bury the four paths that matter under
    every system dll in the registry.
    """
    upper = windows_path.upper()
    directory = upper.rsplit("\\", 1)[0] if "\\" in upper else ""
    if not any(marker in directory for marker in PLUGIN_MARKERS):
        return "other"
    if upper.endswith(tuple(extension.upper() for extension in PLUGIN_EXTENSIONS)):
        return "plugin"
    return "dir"


UNINSTALL_MARKER = "microsoft\\windows\\currentversion\\uninstall"


def uninstall_keys(env: Environment) -> set[str]:
    """Product codes registered under ...\\Uninstall in this prefix, upper-cased.

    Worth checking *before* an uninstall: on a prefix where the vendor's files were
    placed by hand (or by `msiextract`), the product was often never registered with
    Windows Installer at all, so `msiexec /x` has nothing to remove and its exit code
    says nothing useful either way.
    """
    found: set[str] = set()
    for reg_name in ("system.reg", "user.reg"):
        reg = env.prefix / reg_name
        if not reg.is_file():
            continue
        for line in reg.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("["):
                continue
            end = line.find("]")
            if end < 0:
                continue
            section = line[1:end].replace("\\\\", "\\").lower()
            if UNINSTALL_MARKER not in section:
                continue
            found.add(section.rsplit("\\", 1)[-1].upper())
    return found


def is_registered(env: Environment, product_code: str) -> bool:
    return product_code.strip().upper() in uninstall_keys(env)


def scan(env: Environment, include_other: bool = False) -> Report:
    report = Report()
    raw_products: list[dict[str, str]] = []
    for reg_name in ("system.reg", "user.reg"):
        reg = env.prefix / reg_name
        if not reg.is_file():
            continue
        report.scanned_files.append(str(reg))
        text = reg.read_text(errors="replace")

        window: list[tuple[str, str]] = []
        for line in text.splitlines():
            match = VALUE_RE.match(line.strip())
            if match:
                key, raw = match.group(1), match.group(2)
                value = _decode(raw)
                window.append((key, value))
                if len(window) > 24:
                    window.pop(0)
                if key in PRODUCT_KEYS:
                    raw_products.append({"key": key, "value": value, "source": reg_name})

                if value.upper().startswith("C:\\"):
                    kind = _classify(value)
                    if kind == "other" and not include_other:
                        continue
                    linux_path = _to_linux(env, value)
                    if linux_path is None:
                        continue
                    report.entries.append(
                        Entry(
                            raw=value,
                            path=linux_path,
                            kind=kind,
                            exists=linux_path.exists(),
                            source=reg_name,
                        )
                    )

    # collapse the flat DisplayName/DisplayVersion rows into product records.
    # Only entries carrying a version are products -- services register a
    # DisplayName too ("Event Log", "Task Scheduler"), and without a version
    # there is nothing to tell them apart from a real install.
    report.products = [p for p in _group_products(raw_products) if p.get("DisplayVersion")]

    # de-duplicate entries by path, keeping the first sighting
    seen: set[Path] = set()
    unique: list[Entry] = []
    for entry in report.entries:
        if entry.path in seen:
            continue
        seen.add(entry.path)
        unique.append(entry)
    report.entries = sorted(unique, key=lambda e: str(e.path))
    return report


def _group_products(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Collapse flat DisplayName/DisplayVersion rows into one record per product.

    A product appears in both system.reg and user.reg, and its version can be
    written before or after its name, so rows are merged by name rather than
    emitted one per sighting.
    """
    products: dict[str, dict[str, str]] = {}
    order: list[str] = []
    current: dict[str, str] | None = None

    for row in rows:
        if row["key"] == "DisplayName":
            name = row["value"]
            if name not in products:
                products[name] = {"DisplayName": name, "source": row["source"]}
                order.append(name)
            current = products[name]
        elif current is not None and row["key"] in PRODUCT_KEYS:
            current.setdefault(row["key"], row["value"])
    return [products[name] for name in order]


def render(report: Report, limit: int = 40, all_products: bool = False) -> str:
    lines: list[str] = []
    lines.append(f"registry files read : {', '.join(report.scanned_files) or 'none'}")
    lines.append(f"paths found         : {len(report.entries)}")
    lines.append(f"  present           : {len(report.present)}")
    lines.append(f"  MISSING on disk   : {len(report.missing)}")

    if report.missing:
        lines.append("")
        lines.append("BROKEN INSTALLS - registered in the prefix but not on disk:")
        for entry in report.missing[:limit]:
            lines.append(f"  ! {entry.raw}")
        if len(report.missing) > limit:
            lines.append(f"  ... and {len(report.missing) - limit} more")

    plugins = [e for e in report.present if e.kind == "plugin"]
    if plugins:
        lines.append("")
        lines.append(f"plugins present ({len(plugins)}):")
        for entry in plugins[:limit]:
            lines.append(f"  - {entry.path.name}")

    if report.products:
        listed = (
            report.products
            if all_products
            else [p for p in report.products if not is_system_product(p.get("DisplayName", ""))]
        )
        hidden = len(report.products) - len(listed)
        lines.append("")
        lines.append(f"registered products ({len(listed)}):")
        for product in listed[:limit]:
            name = product.get("DisplayName", "?")
            version = product.get("DisplayVersion", "?")
            lines.append(f"  - {name} {version}")
        if hidden:
            lines.append(f"  ({hidden} runtime/system entr{'y' if hidden == 1 else 'ies'} hidden - see --all-products)")
    return "\n".join(lines)