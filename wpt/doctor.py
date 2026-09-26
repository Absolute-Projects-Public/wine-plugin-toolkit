"""`wpt doctor`: check everything this tool depends on, and say what is wrong.

Written for support: it is the difference between "it doesn't work" and a report someone can
act on. Every check is read-only, every failure says what to do about it, and the exit code is
usable from a script (0 clean, 1 warnings, 2 failures).

    python3 -m wpt.cli doctor
    python3 -m wpt.cli doctor --json
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import msi as msi_mod
from . import wrappers as wrappers_mod
from .environment import Environment, EnvironmentError_, detect

OK = "ok"
WARN = "warn"
FAIL = "fail"

REQUIRED_TOOLS = {
    "msiextract": "unpacks an installer's payload (msitools) - required",
    "msiinfo": "reads an installer's identity and file list (msitools) - required",
}

# optional tools, and what each one unlocks
OPTIONAL_TOOLS = {
    "7z": "NSIS, 7-Zip self-extractors and Burn bundles (vc_redist, dotnet) without Wine",
    "cabextract": "CAB and IExpress wrappers, and the cabinets inside other wrappers",
    "innoextract": "Inno Setup wrappers without Wine",
    "unshield": "InstallShield wrappers without Wine",
}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""

    def as_dict(self) -> dict:
        return {"check": self.name, "status": self.status, "detail": self.detail, "fix": self.fix}


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", fix: str = "") -> None:
        self.checks.append(Check(name, status, detail, fix))

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def exit_code(self) -> int:
        if self.failures:
            return 2
        return 1 if self.warnings else 0


def check_environment(report: Report) -> Environment | None:
    try:
        env = detect()
    except EnvironmentError_ as exc:
        report.add("environment", FAIL, str(exc),
                   "Is this an ableton-linux Wine prefix? Pass --prefix/--tree to say so.")
        return None

    report.add(
        "environment",
        OK,
        f"tree {env.wine_tree.name}, prefix {env.prefix}, wine user '{env.user}'",
    )
    if not Path(env.wine_binary).is_file():
        report.add("wine binary", FAIL, f"{env.wine_binary} is missing",
                   "The Wine tree looks incomplete - reinstall or point --tree at another one.")
    else:
        report.add("wine binary", OK, str(env.wine_binary))
    if not (env.prefix / "drive_c").is_dir():
        report.add("prefix layout", FAIL, f"{env.prefix}/drive_c is missing",
                   "That directory is not a Wine prefix.")
    elif not (env.prefix / "system.reg").is_file():
        report.add("prefix layout", WARN, "no system.reg in the prefix - it may never have been run",
                   "Run any Windows program in it once, or check --prefix points at the right place.")
    else:
        report.add("prefix layout", OK, "drive_c, system.reg and user.reg all present")
    return env


def check_tools(report: Report) -> None:
    missing_required = []
    for tool, why in REQUIRED_TOOLS.items():
        if shutil.which(tool):
            report.add(f"tool {tool}", OK, why)
        else:
            missing_required.append(tool)
            report.add(f"tool {tool}", FAIL, f"not installed - {why}", "Install msitools.")
    for tool, why in OPTIONAL_TOOLS.items():
        if shutil.which(tool):
            report.add(f"tool {tool}", OK, why)
        else:
            report.add(f"tool {tool}", WARN, f"not installed - would unlock: {why}",
                       "Optional: without it those wrappers fall back to running under Wine.")


def check_directories(report: Report, env: Environment) -> None:
    for label, path in (
        ("VST3", env.vst3_dir),
        ("VST2", env.vst2_dir),
        ("AAX", env.aax_dir),
        ("Program Files", env.program_files),
        ("ProgramData", env.program_data),
    ):
        if not path.is_dir():
            report.add(f"dir {label}", WARN, f"{path} does not exist",
                       "It appears when something that uses it is installed - usually harmless.")
            continue
        if not _writable(path):
            report.add(f"dir {label}", FAIL, f"{path} is not writable",
                       "Fix ownership/permissions, or you cannot install into this prefix.")
        else:
            report.add(f"dir {label}", OK, str(path))


def _writable(path: Path) -> bool:
    probe = path / ".wpt-write-test"
    try:
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def check_msi_sources(report: Report, env: Environment) -> None:
    vendor = msi_mod.find_extracted_msis(env.prefix)
    everything = msi_mod.find_extracted_msis(env.prefix, include_installer_cache=True)
    cache_only = [m for m in everything if m not in set(vendor)]
    report.add(
        "MSI sources",
        OK if everything else WARN,
        f"{len(vendor)} in vendor folders, {len(cache_only)} more in Wine's installer cache",
        "" if everything else "No cached MSI means file sizes cannot be verified: run a vendor "
                              "installer once, or install one of the plugins with this tool.",
    )
    unreadable: list[Path] = []
    for msi in vendor[:20]:
        try:
            msi_mod.identity(msi)
        except Exception:  # noqa: BLE001 - that is the point of the check
            unreadable.append(msi)
    if unreadable:
        report.add("MSI readability", WARN, f"{len(unreadable)} vendor MSI(s) cannot be read",
                   "Reinstall that product, or ignore it - those files show as 'unverified'.")
    elif vendor:
        report.add("MSI readability", OK, f"all {len(vendor)} vendor MSI(s) readable by msitools")


def check_inventory(report: Report, env: Environment) -> None:
    try:
        from . import inventory as inventory_mod

        inv = inventory_mod.build(env)
    except Exception as exc:  # noqa: BLE001 - a broken listing is exactly what he would report
        report.add("inventory", FAIL, f"could not list the prefix: {exc}")
        return
    if not inv.entries:
        report.add("inventory", WARN, "no plugin files found in this prefix",
                   "Is this the prefix the plugins went into?")
        return
    unowned = len(inv.unowned_standalone)
    report.add(
        "inventory",
        OK if not inv.broken else WARN,
        f"{len(inv.plugins)} plugin file(s): {len(inv.disabled)} disabled, "
        f"{len(inv.broken)} broken, {unowned} other .exe hidden",
        "Run 'wpt repair' for anything broken." if inv.broken else "",
    )
    unverified = [e for e in inv.plugins if e.integrity == "unverified" and e.msi is None]
    if unverified:
        report.add(
            "verifiability", WARN,
            f"{len(unverified)} plugin file(s) have no MSI to check them against "
            f"(e.g. {unverified[0].name})",
            "Normal for hand-placed files; a vendor MSI in the prefix would let them be verified.",
        )


def check_wrappers(report: Report) -> None:
    present = wrappers_mod.unpacker_available()
    families = [
        family.name for family in wrappers_mod.FAMILIES
        if not family.needs_wine and any(shutil.which(tool) for tool in family.tools)
    ]
    report.add(
        "wrapper support",
        OK if present else WARN,
        f"unpackers: {', '.join(present) or 'none'} -> Linux route available for: "
        f"{', '.join(families) or 'nothing (Wine only)'}",
        "" if present else "Install 7z or cabextract to unpack vendor .exe installers without Wine.",
    )
    report.add("driver detection", OK,
               "packages declaring no plugin payload are refused, not installed blindly")


def check_scratch(report: Report, scratch: Path | None = None) -> None:
    target = Path(scratch or "/tmp/wpt-extract").expanduser()
    if not _writable(target if target.is_dir() else target.parent):
        report.add("scratch dir", FAIL, f"{target} (or its parent) is not writable",
                   "Pass --scratch <writable dir>.")
        return
    leftovers = []
    sibling = target.parent / f"{target.name}-unpack"
    for candidate in (target, sibling):
        if candidate.is_dir():
            leftovers.extend(candidate.iterdir())
    report.add(
        "scratch dir",
        OK,
        f"{target} is writable" + (f", {len(leftovers)} leftover entry(ies) from a previous run"
                                   if leftovers else ", clean"),
    )
    if leftovers:
        report.add("scratch leftovers", WARN,
                   f"stale files in {target} or {sibling}",
                   "Harmless (cleared before each extraction) - delete them to reclaim space.")


def check_gui(report: Report) -> None:
    try:
        import PySide6  # noqa: F401

        from PySide6 import __version__ as version

        report.add("graphical front end", OK, f"PySide6 {version} - 'wpt-gui' will run")
    except Exception:  # noqa: BLE001
        report.add("graphical front end", WARN, "PySide6 not installed - the CLI works, the GUI will not",
                   "Install PySide6 (the 'wpt-gui' command) if you want the window.")


def check_rescued_presets(report: Report, env: Environment) -> None:
    root = env.home / ".local" / "share" / "wpt" / "presets"
    if not root.is_dir():
        report.add("preset rescue store", OK, "nothing rescued yet (it appears after an uninstall)")
        return
    products = [p for p in root.iterdir() if p.is_dir()]
    newest = max((len(list(p.rglob("*.xml"))) for p in products), default=0)
    report.add("preset rescue store", OK,
               f"{len(products)} product(s), largest holding {newest} preset file(s) - keep this directory")


def run(env: Environment | None = None, scratch: Path | None = None) -> Report:
    report = Report()
    env = env or check_environment(report)
    if env is None:
        return report
    check_tools(report)
    check_directories(report, env)
    check_msi_sources(report, env)
    check_inventory(report, env)
    check_wrappers(report)
    check_scratch(report, scratch)
    check_gui(report)
    check_rescued_presets(report, env)
    return report


def render(report: Report) -> str:
    marks = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}
    width = max((len(c.name) for c in report.checks), default=0)
    lines = ["wpt doctor - what this machine has, and what is wrong with it", ""]
    for check in report.checks:
        lines.append(f"  {marks[check.status]}  {check.name.ljust(width)}  {check.detail}")
        if check.fix:
            lines.append(f"        {''.join(' ' * width)}  -> {check.fix}")
    lines.append("")
    if report.failures:
        lines.append(f"{len(report.failures)} thing(s) must be fixed, {len(report.warnings)} warning(s).")
    elif report.warnings:
        lines.append(f"nothing broken; {len(report.warnings)} optional thing(s) missing (see above).")
    else:
        lines.append("everything this tool needs is present and healthy.")
    return "\n".join(lines)


def as_json(report: Report) -> str:
    return json.dumps(
        {
            "checks": [c.as_dict() for c in report.checks],
            "failures": len(report.failures),
            "warnings": len(report.warnings),
            "exit_code": report.exit_code,
        },
        indent=2,
    )


def scratch_probe() -> Path:
    """A temp path used when the caller has no scratch setting to hand."""
    return Path(tempfile.gettempdir()) / "wpt-extract"
