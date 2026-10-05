"""Removing a product whose payload media is gone: the tables must describe the same files.

`build_plan()` reads the payload msiextract wrote out, and msiextract needs the cabinet. Wine's
installer cache keeps only the MSI, and a vendor bootstrapper deletes the payload it unpacked, so
an installed, working product can end up with its media gone -- and then an uninstall was
impossible even though the MSI's own tables name every file it placed. (Fortin Cali Suite on the
review machine, 2026-09-27.)

`build_plan_from_tables()` reads those tables instead. This checks the two things that make that
safe: the tables describe the same destinations an extraction would, and a plan with no payload
behind it can never be used to install.

    python3 tests/tables_plan_check.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wpt import installer  # noqa: E402
from wpt import msi as msi_mod  # noqa: E402
from wpt.environment import Environment  # noqa: E402
from wpt.msi import MsiIdentity  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
        failures.append(label)


# --------------------------------------------------------------------- the tables, without payload

_DIRECTORY_ROWS = [
    ["TARGETDIR", "", ""],
    ["VST3DIR", "TARGETDIR", "VST3DIR"],
    ["APPDIR", "TARGETDIR", "APPDIR:."],
    ["PREDIR", "TARGETDIR", "PREDIR"],
    ["SHORTCUTDIR", "TARGETDIR", "SHORTC~1|SHORTCUTDIR"],
    ["User", "PREDIR", "User"],
    ["Artists_Dir", "PREDIR", "ARTIST~1|Artists"],
    ["Nolly_Dir", "Artists_Dir", "NOLLYG~1|Nolly Getgood"],
]
_COMPONENT_ROWS = [
    ["Thing.vst3", "{1}", "VST3DIR"],
    ["Thing.exe", "{2}", "APPDIR"],
    ["Default.xml", "{3}", "PREDIR"],
    ["NollyTone.xml", "{4}", "Nolly_Dir"],
]
_FILE_ROWS = [
    ["Thing.vst3", "Thing.vst3", "THING~1.VST|Thing.vst3"],
    ["Thing.exe", "Thing.exe", "THING~1.EXE|Thing.exe"],
    ["Default.xml", "Default.xml", "Default.xml"],
    ["NollyTone.xml", "NollyTone.xml", "NOLLY~1.XML|Nolly Tone.xml"],
]
_FAKE_TABLES = {
    "Directory": _DIRECTORY_ROWS,
    "Component": _COMPONENT_ROWS,
    "File": _FILE_ROWS,
}

_real_export_table = msi_mod.export_table
msi_mod.export_table = lambda msi, table, *a, **k: _FAKE_TABLES.get(table, [])

print("the MSI's tables, read with no cabinet in sight")
tree = msi_mod.payload_directories(Path("/m/tables-only.msi"))
check("only payload roots appear", sorted(tree), ["APPDIR", "PREDIR", "VST3DIR"])
check("long names come out of the short|long cells",
      tree["PREDIR"], [("Artists", True), ("Default.xml", False)])
check("a declared but empty folder is not listed", "User" in [n for n, _d in tree["PREDIR"]], False)
check("nor is an empty root", "SHORTCUTDIR" in tree, False)
check("a folder with files beneath it is listed as a directory", tree["PREDIR"][0][1], True)
check("and a file as a file", tree["APPDIR"], [("Thing.exe", False)])


# --------------------------------------------------------------------- same destinations, either way

_TABLE_TREE = {
    "VST3DIR": [("Thing.vst3", False)],
    "VSTDIR": [("Thing.dll", False)],
    "AAXDIR": [("Thing.aaxplugin", True)],
    "APPDIR": [("Thing.exe", False), ("readme.pdf", False)],
    "PREDIR": [("Artists", True), ("Default.xml", False)],
}
_EXTRACTED_TREE = {
    "VST3DIR": [("Thing.vst3", False)],
    "VSTDIR": [("Thing.dll", False)],
    "AAXDIR": [("Thing.aaxplugin", True)],
    "APPDIR": [("Thing.exe", False), ("readme.pdf", False)],
    "PREDIR": [("Artists", True), ("Default.xml", False)],
}

msi_mod.export_table = _real_export_table
msi_mod.payload_directories = lambda msi, *a, **k: _TABLE_TREE
_real_identity = msi_mod.identity
_real_sizes = msi_mod.expected_sizes
_real_declares = msi_mod.declares_plugin_payload
_real_manifest = msi_mod.file_manifest
msi_mod.identity = lambda msi, *a, **k: MsiIdentity(
    product_name="Thing", manufacturer="Neural DSP", properties={"VST3DIR": r"C:\Program Files\Common Files\VST3"}
)
msi_mod.expected_sizes = lambda msi, *a, **k: {}
msi_mod.declares_plugin_payload = lambda msi, *a, **k: True
msi_mod.file_manifest = lambda msi, *a, **k: [
    msi_mod.MsiFileEntry("vst3", "VST3DIR", Path("Thing.vst3"), 900),
    msi_mod.MsiFileEntry("vst2", "VSTDIR", Path("Thing.dll"), 900),
    msi_mod.MsiFileEntry("aax", "AAXDIR", Path("Thing.aaxplugin/inside.bin"), 9),
    msi_mod.MsiFileEntry("app", "APPDIR", Path("Thing.exe"), 900),
    msi_mod.MsiFileEntry("pdf", "APPDIR", Path("readme.pdf"), 900),
    msi_mod.MsiFileEntry("artist", "PREDIR", Path("Artists/Nolly Tone.xml"), 900),
    msi_mod.MsiFileEntry("default", "PREDIR", Path("Default.xml"), 900),
]

work = Path(tempfile.mkdtemp(prefix="wpt-tables-check-"))
env = Environment(
    home=work / "home",
    wine_tree=work / "home/.local/opt/wine-d2d1-nspa-11.13",
    prefix=work / "home/.wine-ableton",
    user="tester",
)

payload_root = work / "unpack"
for name, entries in _EXTRACTED_TREE.items():
    (payload_root / name).mkdir(parents=True, exist_ok=True)
    for entry, is_dir in entries:
        target = payload_root / name / entry
        if is_dir:
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.write_bytes(b"installed" * 100)
(payload_root / "AAXDIR/Thing.aaxplugin/inside.bin").write_bytes(b"installed")
(payload_root / "PREDIR/Artists/Nolly Tone.xml").write_bytes(b"installed" * 100)

payload_plan = installer.build_plan(Path("/m/thing.msi"), env, payload_root, include_aax=True)
tables_plan = installer.build_plan_from_tables(Path("/m/thing.msi"), env, include_aax=True)

_payload_dests = sorted(str(a.dest) for a in payload_plan.actions)
_tables_dests = sorted(str(a.dest) for a in tables_plan.actions)

print()
print("a removal read from the tables describes what an extraction describes")
check("the same number of destinations", len(_tables_dests), len(_payload_dests))
check("and exactly the same ones", _tables_dests, _payload_dests)
check("the AAX bundle is one destination, not its contents",
      any(d.endswith("Thing.aaxplugin") for d in _tables_dests), True)
check("nothing points outside the prefix",
      [d for d in _tables_dests if not d.startswith(str(env.drive_c))], [])
check("the empty User folder is not a destination",
      [d for d in _tables_dests if d.endswith("/User")], [])

print()
print("a plan with no payload behind it cannot install")
check("it is marked as destinations-only", tables_plan.destinations_only, True)
check("an extracted plan is not", payload_plan.destinations_only, False)
check("it claims no bytes to copy", tables_plan.total_bytes(), 0)
try:
    installer.apply_plan(tables_plan, env)
    check("apply_plan refuses it", "no error", "MsiError")
except msi_mod.MsiError as exc:
    check("apply_plan refuses it", "cabinet is not on disk" in str(exc), True)
check("while the extracted plan still applies (dry run)",
      [r[0] for r in installer.apply_plan(payload_plan, env, dry_run=True)][:1], ["dry-run"])

print()
print("and table-only removal preserves files because payload bytes are unavailable")
# Create the installed files exactly where the plan says, with the kind (file or directory) the
# MSI's tables gave, so a bundle is tested as a bundle rather than as a file with a long suffix.
_kind: dict[str, bool] = {}
for _root_name, _entries in _EXTRACTED_TREE.items():
    _dest_root = installer._destination_for(
        _root_name,
        msi_mod.identity(Path("/m/thing.msi")),
        env,
        children=[installer.PayloadEntry(n, d) for n, d in _entries],
    )
    for _entry, _is_dir in _entries:
        _kind[str(_dest_root / _entry)] = _is_dir
for dest in _tables_dests:
    path = Path(dest)
    if _kind.get(dest):
        child = "inside.bin" if path.name.endswith(".aaxplugin") else "Nolly Tone.xml"
        (path / child).parent.mkdir(parents=True, exist_ok=True)
        (path / child).write_bytes(b"installed" if child == "inside.bin" else b"installed" * 100)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"installed" * 100)

dry = installer.remove_files(tables_plan, env, dry_run=True)
check("a dry run refuses every unverified File-table file",
      len([r for r in dry if r[0] == "refused"]), len(tables_plan.owned_files))
check("and deletes none of them", all(Path(d).exists() for d in _tables_dests), True)

rows = installer.remove_files(tables_plan, env)
check("the real run refuses unverified deletions",
      len([r for r in rows if r[0] == "refused"]), len(tables_plan.owned_files))
check("and leaves every declared file for manual review",
      set(installer.leftovers(tables_plan, env)), set(tables_plan.owned_files))
check("the plugin directory itself survives its last file",
      (env.drive_c / "Program Files/Common Files/VST3").is_dir(), True)
check("the bundle directory is retained for review",
      (env.drive_c / "Program Files/Common Files/VST3/Thing.vst3").exists(), True)
check("the AAX bundle is retained for review",
      (env.drive_c / "Program Files/Common Files/Avid/Audio/Plug-Ins/Thing.aaxplugin").exists(), True)
check("the preset file named by the MSI is retained for review",
      (env.drive_c / "ProgramData/Neural DSP/Thing/Default.xml").exists(), True)

# The `.disabled` rename must be covered here too: an uninstall of a disabled plugin has to
# remove the renamed copy, or it silently stays on disk.
disabled = Path(_tables_dests[0]) / ("inside.bin" + installer.DISABLED_SUFFIX)
disabled.parent.mkdir(parents=True, exist_ok=True)
disabled.write_bytes(b"installed")  # a rename preserves the File-table byte size
check("a disabled inner copy is included in leftovers",
      str(disabled) in {str(p) for p in installer.leftovers(tables_plan, env)}, True)
disabled_rows = installer.remove_files(tables_plan, env)
check("the disabled copy is refused without payload bytes",
      any(status == "refused" and str(disabled) in path for status, path, _note in disabled_rows), True)
check("and remains for manual review", disabled.exists(), True)


# Staging must still protect the MSI itself -- an uninstall stages it out of msiexec's reach -- but
# it must not refuse over a cabinet a removal does not need. Installing still does refuse.
print()
print("staging: the MSI, not its media, is what a removal needs")
_media = {
    "Media": [
        ["DiskId", "LastSequence", "DiskPrompt", "Cabinet", "VolumeLabel", "Source"],
        ["1", "394", "Disk1", "Gone1.cab", "Disk1", ""],
    ]
}
msi_mod.export_table = lambda msi, table, *a, **k: _media.get(table, [])
_fake_msi = work / "stage/Thing.msi"
_fake_msi.parent.mkdir(parents=True, exist_ok=True)
_fake_msi.write_bytes(b"msi bytes")
try:
    msi_mod.stage_msi(_fake_msi, work / "scratch")
    check("staging refuses a missing cabinet by default", "no error", "MsiError")
except msi_mod.MsiError as exc:
    check("staging refuses a missing cabinet by default", "Gone1.cab" in str(exc), True)
try:
    _staged_anyway = msi_mod.stage_msi(_fake_msi, work / "scratch", require_cabinets=False)
    check("an uninstall can stage it anyway", _staged_anyway.is_file(), True)
    check("and the copy is the MSI", _staged_anyway.read_bytes(), b"msi bytes")
except msi_mod.MsiError as exc:
    check("an uninstall can stage it anyway", f"raised {exc}", "staged")

msi_mod.export_table = _real_export_table
msi_mod.identity = _real_identity
msi_mod.expected_sizes = _real_sizes
msi_mod.declares_plugin_payload = _real_declares
msi_mod.file_manifest = _real_manifest

print()
if failures:
    print(f"tables plan checks: {len(failures)} FAILED")
    for name in failures:
        print(f"  - {name}")
    raise SystemExit(1)
print("tables plan checks: all passed")
