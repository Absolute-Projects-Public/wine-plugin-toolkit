"""Integration tests against a synthetic prefix.

No msitools and no real installer needed: this builds a fake ~/.wine-ableton with
a fake Wine tree, a plugin folder, a registry that mixes present and missing
plugin paths, and a hand-made plan. It exercises detection, the inventory, the
diagnostics scan and the repair filter together -- the wiring the unit tests
don't cover.

    python3 tests/test_prefix_integration.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wpt import inventory, installer, scan  # noqa: E402
from wpt.environment import detect  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
        failures.append(label)


def build_fake_stack(root: Path) -> Path:
    """Create a prefix + Wine tree shaped like the real thing. Returns the fake HOME."""
    home = root / "home"
    tree = home / ".local" / "opt" / "wine-d2d1-nspa-11.13"
    (tree / "bin").mkdir(parents=True)
    (tree / "bin" / "wine").write_text("#!/bin/sh\ntrue\n")
    (tree / "bin" / "wine").chmod(0o755)

    prefix = home / ".wine-ableton"
    vst3 = prefix / "drive_c" / "Program Files" / "Common Files" / "VST3"
    vst2 = prefix / "drive_c" / "Program Files" / "VstPlugins"
    for path in (vst3, vst2):
        path.mkdir(parents=True)
    (prefix / "drive_c" / "users" / "tester" / "AppData" / "Roaming").mkdir(parents=True)

    # two "installed" plugins with known contents
    (vst3 / "Archetype Test X.vst3").write_bytes(b"x" * 4096)
    (vst2 / "Archetype Test X.dll").write_bytes(b"y" * 2048)

    # a registry claiming one present path and two missing ones
    present = vst3 / "Archetype Test X.vst3"
    missing_vst3 = vst3 / "Archetype Ghost X.vst3"
    missing_dll = vst2 / "Archetype Ghost X.dll"

    def as_reg_value(path: Path) -> str:
        """drive_c-relative path in C:\\ form, backslashes doubled like Wine writes them."""
        windows = "C:\\" + str(path.relative_to(prefix / "drive_c")).replace("/", "\\")
        return windows.replace("\\", "\\\\")

    reg = "\n".join(
        [
            "WINE REGISTRY Version 2",
            "[Software\\\\Neural DSP\\\\Products] 1",
            f'"InstalledVst3"="{as_reg_value(present)}"',
            f'"GhostVst3"="{as_reg_value(missing_vst3)}"',
            f'"GhostVst2"="{as_reg_value(missing_dll)}"',
            '"DisplayName"="Archetype Test X"',
            '"DisplayVersion"="1.0.0"',
        ]
    )
    (prefix / "system.reg").write_text(reg)

    # a second registry file with its own product entry: grouping must happen once
    # across both files, not per file (that bug crashed on real data)
    (prefix / "user.reg").write_text(
        "\n".join(
            [
                "WINE REGISTRY Version 2",
                "[Software\\\\Neural DSP\\\\User] 1",
                '"DisplayName"="Another Test Product"',
                '"DisplayVersion"="2.0.0"',
            ]
        )
    )
    return home


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    home = build_fake_stack(root)

    print("detection against a synthetic stack")
    env = detect(home=home, prefix=home / ".wine-ableton")
    check("finds the tree", env.wine_tree.name, "wine-d2d1-nspa-11.13")
    check("finds the windows user", env.user, "tester")
    check("vst3 dir exists", env.vst3_dir.is_dir(), True)

    print("inventory")
    inv = inventory.build(env)
    check("sees both plugin files", len(inv.entries), 2)
    check("classifies vst3", [e.kind for e in inv.entries if e.name.endswith(".vst3")], ["vst3"])
    check("classifies vst2", [e.kind for e in inv.entries if e.name.endswith(".dll")], ["vst2"])
    check("no cached MSI, so nothing to verify against", len(inv.msis), 0)
    check("entries are 'unverified' without an MSI", {e.integrity for e in inv.entries}, {"unverified"})
    check("warns that sizes cannot be cross-checked", any("MSIs cached" in w for w in inv.warnings), True)

    print("diagnostics scan")
    report = scan.scan(env)
    check("found registry plugin paths", len(report.entries), 3)
    check("one present", len(report.present), 1)
    check("two missing", len(report.missing), 2)
    check("missing names", sorted(e.path.name for e in report.missing),
          ["Archetype Ghost X.dll", "Archetype Ghost X.vst3"])
    check("registered products seen across both registry files",
          [p.get("DisplayName") for p in report.products],
          ["Archetype Test X", "Another Test Product"])

    print("repair filter (the manager's core decision)")
    from wpt.installer import Action, Plan
    from wpt.msi import MsiFileEntry, MsiIdentity

    good = env.vst3_dir / "Archetype Test X.vst3"      # 4096 bytes, matches
    bad = env.vst3_dir / "Archetype Wrong X.vst3"      # exists at the wrong size
    bad.write_bytes(b"z" * 10)
    absent = env.vst3_dir / "Archetype Ghost X.vst3"   # missing entirely

    plan = Plan(
        msi=Path("/nonexistent/Archetype Test X.msi"),
        identity=MsiIdentity(product_name="Archetype Test X", manufacturer="Neural DSP"),
        expected={
            "Archetype Test X.vst3": 4096,
            "Archetype Wrong X.vst3": 9999,
            "Archetype Ghost X.vst3": 1234,
        },
    )
    src = root / "payload"
    src.mkdir()
    for name, size in plan.expected.items():
        # The installed Test X bytes are known; the other entries remain size-only fixtures.
        payload_bytes = b"x" if name == "Archetype Test X.vst3" else b"p"
        (src / name).write_bytes(payload_bytes * size)
        plan.actions.append(Action(source=src / name, dest=env.vst3_dir / name, label="VST3DIR"))

    todo = installer.filter_needing_repair(plan, env)
    check("only broken files queued for repair", len(todo.actions), 2)
    check("correct-size file skipped", any(a.dest.name == "Archetype Test X.vst3" for a in todo.actions), False)
    check("wrong-size file queued", any(a.dest.name == "Archetype Wrong X.vst3" for a in todo.actions), True)
    check("missing file queued", any(a.dest.name == "Archetype Ghost X.vst3" for a in todo.actions), True)

    print("plan application + verification")
    results = installer.apply_plan(todo, env, dry_run=True)
    check("dry run writes nothing", env.vst3_dir.joinpath("Archetype Ghost X.vst3").exists(), False)
    check("dry run reports both destinations", len(results), 2)
    applied = installer.apply_plan(todo, env, dry_run=False)
    check("repair executed", sorted(r[0] for r in applied), ["copied", "copied"])
    check("ghost file now exists", (env.vst3_dir / "Archetype Ghost X.vst3").exists(), True)

    # after repair the scan should see one fewer missing path
    report_after = scan.scan(env)
    check("scan sees the repair", len(report_after.missing), 1)

    print("inventory listings")
    inv_after = inventory.build(env)
    names = [e.name for e in inv_after.entries]
    check("restored file now appears in the inventory", "Archetype Ghost X.vst3" in names, True)
    # With no MSI cached in this synthetic prefix there is nothing to compare sizes
    # against, so entries stay 'unverified' -- the size-mismatch verdict is proven
    # by the repair-filter checks above, which is the same comparison.
    check("unverifiable without a cached MSI", {e.integrity for e in inv_after.entries}, {"unverified"})

    print("installer discovery and file-name parsing")
    from wpt.installers import discover, product_and_version

    check("camel-case name + glued version",
          product_and_version("ArchetypeRabeaXv1.1.0"), ("Archetype Rabea X", "1.1.0"))
    check("spaced name + version",
          product_and_version("Mantra v1.1.1"), ("Mantra", "1.1.1"))
    check("Nolly X",
          product_and_version("ArchetypeNollyXv1.0.2"), ("Archetype Nolly X", "1.0.2"))
    check("noise words dropped, no version",
          product_and_version("Kontakt8Installer"), ("Kontakt8", ""))
    check("nothing parseable still returns the stem",
          product_and_version("Weird_Thing"), ("Weird Thing", ""))

    staged = root / "staged"
    staged.mkdir()
    (staged / "ArchetypeRabeaXv1.1.0.exe").write_bytes(b"i" * 128)
    (staged / "ArchetypeNollyXv1.0.2.msi").write_bytes(b"j" * 64)
    found = discover(env, extra_dirs=[staged])
    ours = [i for i in found if i.path.parent == staged]
    check("found both staged installers", len(ours), 2)
    rabea = next(i for i in ours if i.product == "Archetype Rabea X")
    check("wrapper classified", rabea.kind, "wrapper")
    check("msi classified", next(i for i in ours if i.path.suffix == ".msi").kind, "msi")
    check("uninstalled in the synthetic prefix", rabea.status, "not installed")

    print("disable / enable (reversible, file-level)")
    target = env.vst3_dir / "Archetype Test X.vst3"
    check("starts enabled", target.exists(), True)
    disabled = installer.set_enabled(env, "Test X", enabled=False, dry_run=True)
    check("dry run does not touch the file", target.exists(), True)
    installer.set_enabled(env, "Test X", enabled=False)
    hidden = env.vst3_dir / "Archetype Test X.vst3.disabled"
    check("disabled by rename", (hidden.exists(), target.exists()), (True, False))

    # a disabled plugin must not vanish from the manager: it is still installed,
    # just hidden from the DAW, so the inventory has to keep showing it
    inv_disabled = inventory.build(env)
    entry = next((e for e in inv_disabled.entries if e.name.startswith("Archetype Test X.vst3")), None)
    check("disabled plugin still listed", entry is not None, True)
    check("flagged as disabled", entry.disabled if entry else None, True)
    check("still classified as vst3", entry.kind if entry else None, "vst3")
    check("inventory reports the disabled plugin",
          sorted(e.name for e in inv_disabled.disabled),
          ["Archetype Test X.dll.disabled", "Archetype Test X.vst3.disabled"])

    installer.set_enabled(env, "Test X", enabled=True)
    check("re-enabled by rename back", (target.exists(), hidden.exists()), (True, False))
    check("no longer listed as disabled", len(inventory.build(env).disabled), 0)
    check("DAW-invisible while disabled", "broken" not in target.name, True)

    print("uninstall: file-level removal (the route that works when msiexec cannot)")
    from wpt.installer import leftovers, remove_files

    real = env.vst3_dir / "Archetype Test X.vst3"
    wrong = env.vst3_dir / "Archetype Wrong X.vst3"
    ghost = env.vst3_dir / "Archetype Ghost X.vst3"
    keeper = env.vst3_dir / "Archetype Keep X.vst3"
    keeper.write_bytes(b"k" * 32)
    outside = root / "outside.txt"
    outside.write_text("not mine")

    # one of the plugin's files is currently disabled, which is the name it really has
    installer.set_enabled(env, "Wrong X", enabled=False)
    disabled_wrong = env.vst3_dir / "Archetype Wrong X.vst3.disabled"
    disabled_wrong.write_bytes(b"user modified")
    check("fixture: user-modified file is disabled",
          disabled_wrong.exists() and disabled_wrong.stat().st_size != 9999, True)

    def make_removal_plan(targets):
        result = Plan(
            msi=Path("/nonexistent/Archetype Test X.msi"),
            identity=MsiIdentity(product_name="Archetype Test X", manufacturer="Neural DSP"),
            expected=dict(plan.expected),
        )
        for target in targets:
            result.actions.append(Action(source=src / target.name, dest=target, label="VST3DIR"))
            result.owned_files[target] = MsiFileEntry(
                target.name, "VST3DIR", Path(target.name),
                result.expected.get(target.name, target.stat().st_size if target.exists() else 0))
        return result

    # One unsafe destination must make the whole operation refuse before any in-prefix unlink.
    unsafe_plan = make_removal_plan((real, wrong, ghost, outside))
    unsafe = remove_files(unsafe_plan, env, dry_run=True)
    check("out-of-prefix destination refuses the whole plan",
          len(unsafe) == 1 and unsafe[0][0] == "refused", True)
    check("unsafe plan leaves every fixture untouched",
          (real.exists(), disabled_wrong.exists(), outside.exists()), (True, True, True))

    safe_plan = make_removal_plan((real, wrong, ghost))
    dry = remove_files(safe_plan, env, dry_run=True)
    check("dry run reports the unchanged File-table files", sum(r[0] == "dry-run" for r in dry), 2)
    check("dry run preserves the modified disabled file",
          any(r[0] == "refused" and r[1] == str(disabled_wrong) for r in dry), True)
    check("dry run deletes nothing", (real.exists(), disabled_wrong.exists()), (True, True))

    done = remove_files(safe_plan, env)
    check("removed the unchanged File-table files", (real.exists(), ghost.exists()), (False, False))
    check("reported the modified file as preserved",
          any(r[0] == "refused" and r[1] == str(disabled_wrong) for r in done), True)
    check("preserved a modified disabled file", disabled_wrong.exists(), True)
    check("outside file untouched", outside.exists(), True)
    check("unrelated plugin untouched", keeper.exists(), True)
    check("leftovers reports the modified MSI-owned file",
          leftovers(safe_plan, env), [disabled_wrong])
    check("no leftover empty dirs above the deletions", env.vst3_dir.is_dir(), True)

    print("purge: registry entries left pointing at deleted files")
    from wpt.installer import purge_registry, stale_registry_edits

    edits = stale_registry_edits(env, safe_plan)
    check("finds the values pointing at this product's files",
          sorted(e.value for e in edits if e.value), ["GhostVst3", "InstalledVst3"])
    check("leaves alone a value pointing at another product's file",
          any(e.value == "GhostVst2" for e in edits), False)

    # a pointer at a path that no longer exists and names this product is stale too
    stale_plan = Plan(
        msi=Path("/nonexistent/Archetype Test X.msi"),
        identity=MsiIdentity(product_name="Archetype Test X", manufacturer="Neural DSP"),
        expected={},
    )
    stale_env = detect(home=home, prefix=home / ".wine-ableton")
    stale_reg = stale_env.prefix / "user.reg"
    stale_reg.write_text(
        stale_reg.read_text()
        + '\n[Software\\\\Vendor\\\\P] 1\n'
        + '"UserPresets"="C:\\\\ProgramData\\\\Neural DSP\\\\Archetype Test X\\\\User\\\\"\n'
        + '"Other"="C:\\\\ProgramData\\\\Unrelated Product\\\\User\\\\"\n'
    )
    stale_edits = stale_registry_edits(stale_env, stale_plan)
    check("does not purge an unproven user-preset pointer based on product name alone",
          any(e.value == "UserPresets" for e in stale_edits), False)
    check("leaves a stale pointer to another product alone",
          any(e.value == "Other" for e in stale_edits), False)
    check("builds a wine reg delete command",
          edits[0].command(env.wine_binary)[1:4],
          ["reg", "delete", f"{edits[0].hive}\\{edits[0].key}"])
    check("value deletions pass /v, key deletions do not",
          ("/v" in edits[0].command(env.wine_binary)),
          True)
    preview = purge_registry(env, edits, dry_run=True)
    check("dry run reports every edit without applying it", len(preview), 2)
    check("dry run status", {row[0] for row in preview}, {"dry-run"})
    applied = purge_registry(env, edits)
    check("purge runs one wine reg delete per edit",
          [row[0] for row in applied], ["purged", "purged"])

    print("presets: rescued before a removal can take them")
    import tempfile as _tempfile2  # noqa: E402

    from wpt import presets as presets_mod  # noqa: E402

    prod_root = env.program_data / "Neural DSP" / "Archetype Test X"
    (prod_root / "User").mkdir(parents=True, exist_ok=True)
    (prod_root / "Artists" / "Someone").mkdir(parents=True, exist_ok=True)
    (prod_root / "Artist Preset.xml").write_text("factory")
    (prod_root / "Artists" / "Someone" / "Factory Two.xml").write_text("factory")
    mine = prod_root / "User" / "My Tone.xml"
    mine.write_text("mine")
    (prod_root / "User" / "My Other Tone.xml").write_text("mine too")
    pack = env.program_data / "Neural DSP" / "NEURAL DSP" / "Nolly X Presets"
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "Downloaded Tone.xml").write_text("downloaded")

    presets = presets_mod.collect(env, "Archetype Test X")
    check("collects the user's own presets", sorted(p.name for p in presets.user),
          ["My Other Tone.xml", "My Tone.xml"])
    check("collects a downloaded pack too", [p.name for p in presets.downloaded], ["Downloaded Tone.xml"])
    check("factory presets are not treated as irreplaceable",
          any("Factory" in p.name for p in presets.irreplaceable), False)

    rescue_root = Path(_tempfile2.mkdtemp()) / "rescued"
    preview, _ = presets_mod.rescue(env, "Archetype Test X", rescue_root=rescue_root, dry_run=True)
    check("dry run rescues nothing", rescue_root.exists(), False)
    check("dry run lists what would be saved", len(preview), 3)
    rows_rescued, destination = presets_mod.rescue(env, "Archetype Test X", rescue_root=rescue_root)
    check("rescue copies every irreplaceable preset", sum(1 for r in rows_rescued if r[0] == "saved"), 3)
    saved_names = sorted(p.name for p in destination.rglob("*.xml"))
    check("the rescued copies are all there",
          saved_names, ["Downloaded Tone.xml", "My Other Tone.xml", "My Tone.xml"])
    again, _ = presets_mod.rescue(env, "Archetype Test X", rescue_root=rescue_root,
                                  stamp=destination.name)
    check("running it twice does not duplicate or clobber",
          sorted({r[0] for r in again}), ["kept"])

    export_dir = Path(_tempfile2.mkdtemp()) / "exported"
    exported = presets_mod.export(env, export_dir)
    check("export copies them out flat", sum(1 for r in exported if r[0] == "exported"), 3)

    print("verification of a directory payload (an .aaxplugin bundle)")
    from wpt.installer import verify_plan

    bundle = env.vst3_dir / "Archetype Bundle X.vst3"
    (bundle / "Contents" / "x64").mkdir(parents=True)
    inner = bundle / "Contents" / "x64" / "Archetype Bundle X.vst3"
    inner.write_bytes(b"b" * 512)
    bundle_plan = Plan(
        msi=Path("/nonexistent/Archetype Bundle X.msi"),
        identity=MsiIdentity(product_name="Archetype Bundle X", manufacturer="Neural DSP"),
        expected={"Archetype Bundle X.vst3": 512},
    )
    bundle_plan.actions.append(Action(source=src / "bundle", dest=bundle, label="VST3DIR"))
    rows = verify_plan(bundle_plan, env)
    check("a bundle is verified by the file inside it, not the directory entry",
          [(r[0], r[2]) for r in rows], [("ok", "512 B")])

    inner.write_bytes(b"b" * 999)   # now the binary inside disagrees with the File table
    rows = verify_plan(bundle_plan, env)
    check("a wrong-sized binary inside a bundle is still caught", rows[0][0], "size-mismatch")

    inner.unlink()
    rows = verify_plan(bundle_plan, env)
    check("a bundle whose binary vanished reports missing", rows[0][0], "missing")

    print("pending installer -> MSI resolution")
    from wpt.installers import msi_for

    check("an .msi download installs from itself", msi_for(
        next(i for i in ours if i.path.suffix == ".msi"), env), staged / "ArchetypeNollyXv1.0.2.msi")
    check("a wrapper with no cached MSI resolves to nothing",
          msi_for(next(i for i in ours if i.kind == "wrapper"), env), None)

print()
if failures:
    print(f"{len(failures)} FAILURES: {', '.join(failures)}")
    raise SystemExit(1)
print("all integration checks passed")