"""Product triage: which registered products are real, and which are debris.

Written for the "I tried five times before finding this tool" case. A machine that
has been through failed installs accumulates all of it: runtimes registered by
msiexec, vendor records pointing at files that no longer exist, plugins living in
one prefix while the registry talks about another, and whole extra prefixes
(Bottles bottles, game prefixes) nobody remembers creating.

This walks every Wine prefix it can find, reads the product records out of each
prefix's `system.reg` / `user.reg`, cross-checks them against what is actually on
disk, and says for each one: is this in use, is it only files, is it only a
registry fragment, or does it belong to a different prefix?

Nothing here writes anything -- triage is read-only, so it is safe to run before
deciding what to remove.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .environment import Environment
from .scan import is_system_product

SECTION_RE = re.compile(r"^\[(.*)\]\s+(\d+)\s*$")
VALUE_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"=(.*)$')
PRODUCT_KEYS = ("DisplayName", "DisplayVersion", "Publisher", "InstallLocation", "UninstallString")

# Where a prefix's plugin payload can live, relative to drive_c. Bounded on purpose:
# a full walk of drive_c on a game prefix is minutes, not seconds.
PLUGIN_DIRS = (
    "Program Files/Common Files/VST3",
    "Program Files/Common Files/VST2",
    "Program Files/VstPlugins",
    "Program Files/Steinberg/VSTPlugins",
    "Program Files/Common Files/Avid/Audio/Plug-Ins",
    "Program Files/Neural DSP",
    "ProgramData/Neural DSP",
)
PLUGIN_SUFFIXES = (".vst3", ".dll", ".aaxplugin", ".exe", ".vst2")

PREFIX_KINDS = (
    ("bottles", "bottle"),
    ("compatdata", "steam"),
    ("heroic", "heroic"),
    ("lutris", "lutris"),
    ("wineprefixes", "wine"),
)

# Verdicts, in the order they are worth reading.
VERDICTS = ("in use", "files only", "wrong prefix", "fragment", "registered")


@dataclass
class Product:
    name: str
    version: str = ""
    publisher: str = ""
    prefix: Path | None = None
    keys: list[str] = field(default_factory=list)
    installed_at: float | None = None
    registered_paths: list[Path] = field(default_factory=list)
    on_disk: list[Path] = field(default_factory=list)
    elsewhere: list[tuple[Path, Path]] = field(default_factory=list)  # (path, other prefix)
    system: bool = False

    @property
    def missing_paths(self) -> list[Path]:
        return [p for p in self.registered_paths if not p.exists()]

    @property
    def verdict(self) -> str:
        if self.registered_paths and any(p.exists() for p in self.registered_paths):
            return "in use"
        if self.on_disk:
            return "files only" if not self.registered_paths else "wrong prefix"
        if self.registered_paths or self.keys:
            return "fragment" if self.registered_paths else "registered"
        return "registered"

    @property
    def age(self) -> str:
        if not self.installed_at:
            return "?"
        seconds = max(0.0, time.time() - self.installed_at)
        for limit, divisor, unit in (
            (3600, 60, "m"),
            (86400, 3600, "h"),
            (2592000, 86400, "d"),
            (31536000, 2592000, "mo"),
        ):
            if seconds < limit:
                return f"{seconds / divisor:.0f}{unit} ago"
        return f"{seconds / 31536000:.1f}y ago"

    @property
    def date(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.installed_at)) if self.installed_at else "unknown"


@dataclass
class PrefixReport:
    path: Path
    kind: str
    environment: Environment | None = None
    products: list[Product] = field(default_factory=list)
    plugin_files: list[Path] = field(default_factory=list)
    registry_plugin_paths: list[Path] = field(default_factory=list)
    readable: bool = True

    @property
    def is_target(self) -> bool:
        return self.kind == "target"


# ----------------------------------------------------------------- prefix discovery


def find_prefixes(env: Environment, home: Path) -> list[PrefixReport]:
    """Every Wine prefix that looks like one: the target first, then the rest."""
    candidates: list[tuple[Path, str]] = [(env.prefix, "target")]
    patterns = (
        (".wine", "wine"),
        (".wine-*", "wine"),
        (".local/share/wineprefixes/*", "wine"),
        (".local/share/Steam/steamapps/compatdata/*/pfx", "steam"),
        (".var/app/com.usebottles.bottles/data/bottles/bottles/*", "bottle"),
        (".config/heroic/prefixes/*", "heroic"),
        (".local/share/lutris/prefixes/*", "lutris"),
        ("Games/*/pfx", "wine"),
    )
    for pattern, kind in patterns:
        for path in sorted(home.glob(pattern)):
            if path.is_dir():
                candidates.append((path, kind))

    seen: set[Path] = set()
    reports: list[PrefixReport] = []
    for path, kind in candidates:
        real = path.resolve()
        if real in seen or not looks_like_prefix(path):
            continue
        seen.add(real)
        report = PrefixReport(path=path, kind=kind)
        report.products, report.registry_plugin_paths = read_products(path)
        report.plugin_files = scan_plugin_files(path)
        reports.append(report)
    return reports


def looks_like_prefix(path: Path) -> bool:
    return (path / "drive_c").is_dir() and (
        (path / "system.reg").is_file() or (path / "user.reg").is_file()
    )


# ------------------------------------------------------------------- reg parsing


def reg_sections(path: Path) -> dict[str, dict]:
    """key -> {'epoch': int, 'values': {name: data}} for one .reg file."""
    return _sections(path)


def decode_value(value: str) -> str:
    """Undo Wine .reg escaping (including UTF-16 `str(2):` blobs)."""
    return _decode(value)


def to_path(drive_c: Path, value: str) -> Path | None:
    """`C:\\Program Files\\X` -> <prefix>/drive_c/Program Files/X, or None."""
    return _to_path(drive_c, value)


def is_plugin_path(path: Path) -> bool:
    return _is_plugin_path(path)


def _sections(path: Path) -> dict[str, dict]:
    """key -> {'epoch': int, 'values': {name: data}} for one .reg file."""
    out: dict[str, dict] = {}
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return out
    current: dict | None = None
    for line in text.splitlines():
        stripped = line.strip()
        header = SECTION_RE.match(stripped)
        if header:
            current = out.setdefault(header.group(1), {"epoch": int(header.group(2)), "values": {}})
            continue
        if current is None:
            continue
        value = VALUE_RE.match(stripped)
        if value:
            current["values"][value.group(1)] = value.group(2)
    return out


def _decode(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("str(2):"):
        hexed = value.split(":", 1)[1].strip().strip('"')
        try:
            return bytes.fromhex(hexed).decode("utf-16-le", errors="replace")
        except ValueError:
            return ""
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value.replace("\\\\", "\\")


def _to_path(drive_c: Path, value: str) -> Path | None:
    decoded = _decode(value)
    if not decoded.upper().startswith("C:\\"):
        return None
    rel = decoded[3:].replace("\\", "/").rstrip("/")
    if not rel:
        return None
    return drive_c / rel


PLUGIN_DIR_MARKERS = ("VST3", "VSTPLUGINS", "PLUG-INS", "AAXPLUGIN", "NEURAL DSP", "ILOK", "PACE")

# Registry trees that hold class/COM/WinRT registrations rather than products. They
# carry thousands of `C:\...\*.dll` values, so without this every WinRT class looks
# like an installed product.
NOISE_KEY_MARKERS = (
    "\\classes\\",
    "\\activatableclasses\\",
    "\\clsid\\",
    "\\interface\\",
    "\\services\\",
    "currentcontrolset",
)


def _is_plugin_path(path: Path) -> bool:
    """A registered path only counts when it lands in a plugin directory.

    Matching on `.dll` alone is the trap here: system dll registrations would all
    qualify. The directory has to look like a plugin location.
    """
    directory = str(path.parent).upper()
    if any(marker in directory for marker in PLUGIN_DIR_MARKERS):
        return path.suffix.lower() in (".vst3", ".dll", ".aaxplugin", ".exe", ".vst2") or path.is_dir()
    return path.suffix.lower() in (".vst3", ".aaxplugin")


def read_products(prefix: Path) -> tuple[list[Product], list[Path]]:
    """Products recorded in a prefix's registry, merged across both .reg files."""
    drive_c = prefix / "drive_c"
    records: dict[str, Product] = {}
    registry_paths: list[Path] = []

    for reg_name in ("system.reg", "user.reg"):
        reg = prefix / reg_name
        if not reg.is_file():
            continue
        for key, payload in _sections(reg).items():
            values = payload["values"]
            epoch = payload["epoch"] or None
            lowered_key = key.lower()
            if any(marker in lowered_key for marker in NOISE_KEY_MARKERS):
                continue
            uniq_paths = sorted(
                {p for p in (_to_path(drive_c, v) for v in values.values()) if p and _is_plugin_path(p)},
                key=str,
            )
            registry_paths.extend(uniq_paths)

            name = _decode(values.get("DisplayName", "")) if values.get("DisplayName") else ""
            if not name and not uniq_paths:
                continue
            if not name and any(noise in key.lower() for noise in ("installer", "classes", "microsoft\\windows")):
                continue
            label = name or _label_from_key(key, uniq_paths)
            if not label:
                continue
            # a record is "system" when nothing about it points at a plugin payload
            system = is_system_product(label) and not uniq_paths

            product = records.get(label)
            if product is None:
                product = records[label] = Product(name=label, prefix=prefix, system=system)
            if product.system and not system:
                product.system = False
            if values.get("DisplayVersion"):
                product.version = product.version or _decode(values["DisplayVersion"])
            if values.get("Publisher"):
                product.publisher = product.publisher or _decode(values["Publisher"])
            if epoch and (product.installed_at is None or epoch > product.installed_at):
                product.installed_at = epoch
            product.keys.append(key)
            for path in uniq_paths:
                if path not in product.registered_paths:
                    product.registered_paths.append(path)

    for product in records.values():
        product.registered_paths.sort(key=str)
    return (
        sorted(records.values(), key=lambda p: (p.system, -(p.installed_at or 0), p.name)),
        sorted(set(registry_paths), key=str),
    )


def _label_from_key(key: str, paths: list[Path]) -> str:
    """Name a vendor record that has no DisplayName, from its own paths.

    A Neural DSP record points at `.../ProgramData/Neural DSP/<Product>/...`, so the
    product is the segment after the vendor. For anything else, the first segment
    under Program Files / ProgramData is the vendor, which is the useful label.
    """
    for path in paths:
        parts = list(path.parts)
        for vendor in ("Neural DSP", "iLok", "PACE", "PACE Anti-Piracy"):
            if vendor in parts:
                after = [
                    p
                    for p in parts[parts.index(vendor) + 1 :]
                    if p and p.lower() not in ("plugins", "common files", "vst3", "vstplugins", "legacy", "user")
                ]
                if after:
                    return after[0]
        for root in ("ProgramData", "Program Files (x86)", "Program Files"):
            if root in parts:
                index = parts.index(root)
                if index + 1 < len(parts):
                    return parts[index + 1]
    return key.rsplit("\\", 1)[-1]


def scan_plugin_files(prefix: Path) -> list[Path]:
    """Plugin payload actually on disk, from the standard directories only."""
    found: list[Path] = []
    drive_c = prefix / "drive_c"
    for relative in PLUGIN_DIRS:
        root = drive_c / relative
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in PLUGIN_SUFFIXES and not path.name.startswith("."):
                found.append(path)
            elif path.is_dir() and path.suffix.lower() in (".vst3", ".aaxplugin"):
                found.append(path)
    # a bundle directory *and* the binary inside it were both being counted, so the triage
    # total did not agree with `wpt list` (15 against 12 on his prefix, 2026-09-26): keep the
    # bundle, drop anything living inside one
    bundles = {path for path in found if path.is_dir()}
    found = [
        path for path in found
        if not any(parent in bundles for parent in path.parents)
    ]
    return sorted(set(found), key=str)


# ------------------------------------------------------------------ cross-checking


def _tokens(product_name: str) -> list[str]:
    words = [w for w in re.split(r"[^A-Za-z0-9]+", product_name) if len(w) >= 4]
    return [w.lower() for w in words]


def _matches(product: Product, path: Path) -> bool:
    tokens = _tokens(product.name)
    if not tokens:
        return False
    lowered = path.name.lower()
    return all(token in lowered for token in tokens)


def cross_check(reports: list[PrefixReport]) -> None:
    """Fill in on-disk evidence and 'these files live in another prefix' for each product."""
    for report in reports:
        for product in report.products:
            if product.system:
                continue
            product.on_disk = [p for p in report.plugin_files if _matches(product, p)]
            if product.registered_paths and not any(p.exists() for p in product.registered_paths):
                for other in reports:
                    if other.path == report.path:
                        continue
                    hits = [p for p in other.plugin_files if _matches(product, p)]
                    if hits:
                        product.elsewhere = [(p, other.path) for p in hits[:4]]
    add_unrecorded(reports)


# Plugin payloads that are support files, not a plugin of their own: Qt ships its
# image/platform dlls inside the VST3 directory, and they are not products.
SUPPORT_NAME_RE = re.compile(r"^q[a-z0-9]{2,}\.dll$", re.IGNORECASE)


def add_unrecorded(reports: list[PrefixReport]) -> None:
    """Plugin files with no registry record at all become 'files only' products.

    Without this, a plugin installed by hand (or one whose registry record was
    already cleared) is invisible to triage -- and that is exactly the state a
    half-finished install leaves behind.
    """
    for report in reports:
        claimed = {path for product in report.products for path in product.on_disk}
        orphans = [
            path
            for path in report.plugin_files
            if path not in claimed and not SUPPORT_NAME_RE.match(path.name)
        ]
        grouped: dict[str, list[Path]] = {}
        for path in orphans:
            grouped.setdefault(_base_name(path), []).append(path)
        for name, paths in sorted(grouped.items()):
            report.products.append(Product(name=name, prefix=report.path, on_disk=sorted(set(paths), key=str)))
        report.products.sort(key=lambda p: (p.system, -(p.installed_at or 0), p.name))


def _base_name(path: Path) -> str:
    """A bundle, its inner binary and the VST2 dll are one product, not three."""
    name = path.name
    for suffix in (".aaxplugin", ".vst3", ".vst2", ".dll", ".exe"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def triage(env: Environment, home: Path | None = None) -> list[PrefixReport]:
    home = home or Path.home()
    reports = find_prefixes(env, home)
    cross_check(reports)
    return reports


# ------------------------------------------------------------------------ render


def group_across_prefixes(reports: list[PrefixReport], all_products: bool = False) -> dict[str, list[Product]]:
    """Product name -> the record in each prefix that holds it.

    The same plugin installed twice (or three times, across a bottle and two .wine
    directories) is the single most common piece of debris on a machine that has
    been through failed attempts. Grouping is what makes "which of these is real?"
    answerable at a glance.
    """
    grouped: dict[str, list[Product]] = {}
    for report in reports:
        for product in report.products:
            if product.system and not all_products:
                continue
            grouped.setdefault(product.name, []).append(product)
    return grouped


def render(reports: list[PrefixReport], all_products: bool = False, limit: int = 200) -> str:
    lines: list[str] = []
    lines.append("Wine prefixes found")
    lines.append("-" * 60)
    for report in reports:
        tag = {"target": "TARGET", "bottle": "bottle", "steam": "steam", "heroic": "heroic", "lutris": "lutris"}.get(
            report.kind, "wine"
        )
        products = [p for p in report.products if all_products or not p.system]
        lines.append(
            f"  {tag:7} {str(report.path).replace(str(Path.home()), '~')}"
            f"   {len(report.plugin_files)} plugin file(s), {len(report.registry_plugin_paths)} registry path(s),"
            f" {len(products)} product record(s)"
        )

    for report in reports:
        products = [p for p in report.products if all_products or not p.system]
        if not products:
            continue
        lines.append("")
        lines.append(
            f"Products recorded in {str(report.path).replace(str(Path.home()), '~')} ({len(products)})"
        )
        lines.append("-" * 60)
        width = max((len(p.name) for p in products), default=0)
        for product in products[:limit]:
            lines.append(
                f"  {product.date}  {product.verdict:13} {product.name.ljust(width)}  "
                f"{product.version or '?':<12} {product.publisher or ''}".rstrip()
            )
            if product.verdict in ("fragment", "wrong prefix"):
                for path in product.missing_paths[:3]:
                    lines.append(f"        registered, gone: {path}")
                for path, other in product.elsewhere[:3]:
                    lines.append(f"        files live in {str(other).replace(str(Path.home()), '~')}: {path.name}")
            if product.verdict == "files only":
                for path in product.on_disk[:4]:
                    lines.append(f"        on disk: {path.name}")
        if len(products) > limit:
            lines.append(f"  ... and {len(products) - limit} more")

    grouped = group_across_prefixes(reports, all_products=all_products)
    duplicates = {name: hits for name, hits in grouped.items() if len({str(p.prefix) for p in hits}) > 1}
    if duplicates:
        lines.append("")
        lines.append(f"Same product recorded in more than one prefix ({len(duplicates)})")
        lines.append("-" * 60)
        for name in sorted(duplicates):
            hits = duplicates[name]
            lines.append(f"  {name}  {hits[0].version or '?'}")
            for product in sorted(hits, key=lambda p: (VERDICTS.index(p.verdict), -(p.installed_at or 0))):
                where = str(product.prefix).replace(str(Path.home()), "~")
                lines.append(f"      {product.verdict:13} {product.date if product.installed_at else '     -      '}  {where}")
            live = [p for p in hits if p.verdict == "in use"]
            if live:
                keep = str(live[0].prefix).replace(str(Path.home()), "~")
                others = [str(p.prefix).replace(str(Path.home()), "~") for p in hits if p.verdict != "in use"]
                lines.append(f"      -> the live registration is in {keep}")
                lines.append(
                    f"         copies in {', '.join(others)} show as files-only: usually leftover, but check"
                )
                lines.append("         which prefix your DAW actually scans before deleting either.")

    removable = [
        (p, r)
        for r in reports
        for p in r.products
        if not p.system and p.verdict in ("fragment", "files only", "wrong prefix")
    ]
    if removable:
        lines.append("")
        lines.append(f"Worth a look before you remove anything ({len(removable)})")
        lines.append("-" * 60)
        for product, report in sorted(removable, key=lambda pr: (VERDICTS.index(pr[0].verdict), pr[0].name)):
            where = str(report.path).replace(str(Path.home()), "~")
            if product.verdict == "fragment":
                why = "registry fragment: recorded here, no files on disk anywhere"
            elif product.verdict == "files only":
                why = f"files only: {len(product.on_disk)} plugin file(s) with no registry record"
            else:
                why = "registered here, files live in another prefix"
            lines.append(f"  {product.verdict:13} {product.name}  ({where})")
            lines.append(f"      {why}")

    lines.append("")
    lines.append("What the verdicts mean")
    lines.append("-" * 60)
    lines.append("  in use        registered, and the files it points at are on disk -- leave it alone")
    lines.append("  files only    plugin files with no registry record: placed by hand, or the record was")
    lines.append("                cleared. Playable, just untracked.")
    lines.append("  wrong prefix  registered here, but its files are in another prefix -- the classic")
    lines.append("                'installed into the wrong .wine' case")
    lines.append("  fragment      registered here and nothing on disk anywhere: a failed install, the")
    lines.append("                registry remembers a product that was never really installed")
    lines.append("  registered    recorded with no file evidence either way (runtimes, services)")
    lines.append("")
    lines.append("Before removing anything: files can be deleted, activations cannot.")
    lines.append("  If a plugin was activated (iLok / PACE, or a vendor account) and that machine or")
    lines.append("  prefix will not be used again, deleting the files does NOT free the activation.")
    lines.append("  Deactivate it in iLok License Manager, or if the location is unreachable use")
    lines.append("  'Report as Unusable' there so the slot comes back. Otherwise the licence stays")
    lines.append("  consumed and the plugin will not authorise on the machine you actually use.")
    return "\n".join(lines)
