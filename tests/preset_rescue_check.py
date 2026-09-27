"""The preset rescue, and the two smaller safety fixes, without a GUI.

Covers what the review found:

* `rescue_for_plan` rescues the user's own presets whatever the MSI calls itself. `rescue()` takes
  a product *name*, and the name is not dependable - a package can declare none, or name itself
  differently from the folder its own installer created - so an uninstall could delete the user's
  presets while the rescue silently saved nothing.
* `_removal_targets` covers the `.disabled` rename of a bundle directory too, not just of a plain
  file, so an uninstall of a currently-disabled plugin cleans up after itself.
* `stage_msi` gives each staged MSI its own directory, so two packages' sidecars cannot overwrite
  each other in the shared cache, and it brings the cabinet the MSI *names* (not only a file that
  happens to sit beside it), because Wine's installer cache keeps the MSI without its payload.
* a cabinet that is genuinely gone is refused by name, with nothing touched.

    python3 tests/preset_rescue_check.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wpt import installer, msi as msi_mod, presets as presets_mod  # noqa: E402
from wpt.environment import detect  # noqa: E402
from wpt.installer import Action, Plan  # noqa: E402
from wpt.msi import MsiIdentity  # noqa: E402

checks = 0
failures: list[str] = []


def check(name: str, got, want) -> None:
    global checks
    checks += 1
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if got == want else 'FAIL'}  {name}")


def build_prefix(root: Path) -> Path:
    home = root / "home"
    tree = home / ".local" / "opt" / "wine-d2d1-nspa-11.13"
    (tree / "bin").mkdir(parents=True)
    (tree / "bin" / "wine").write_text("#!/bin/sh\ntrue\n")
    (tree / "bin" / "wine").chmod(0o755)
    prefix = home / ".wine-ableton"
    (prefix / "drive_c" / "Program Files" / "Common Files" / "VST3").mkdir(parents=True)
    (prefix / "drive_c" / "users" / "tester" / "AppData" / "Roaming").mkdir(parents=True)
    return home


def plant_user_presets(env, product: str, count: int = 3) -> Path:
    """The user's own presets, where the vendor's installer put them, plus a pack folder."""
    product_root = env.program_data / "Neural DSP" / product
    user = product_root / "User"
    user.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (user / f"My Sound {i}.xml").write_text("<preset/>")
    # deliberately not named "*preset*": a downloaded pack does not have to advertise itself
    pack = product_root / "Packs"
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "Downloaded Pack.xml").write_text("<preset/>")
    return product_root


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    home = build_prefix(root)
    env = detect(home=home, prefix=home / ".wine-ableton")
    product_root = plant_user_presets(env, "Archetype Test X")
    user_dir = product_root / "User"
    # ALWAYS an explicit rescue_root: DEFAULT_RESCUE_ROOT is under the *real* home, and a test that
    # omits it writes into the user's own rescue store (this one did, once).
    rescue_root = root / "rescue"

    def plan_for(dest: Path, product_name: str):
        payload = root / "extracted"
        payload.mkdir(exist_ok=True)
        return Plan(
            msi=root / "package.msi",
            identity=MsiIdentity(product_name=product_name, manufacturer="Neural DSP"),
            expected={},
            actions=[Action(source=payload, dest=dest, label="PREDIR")],
        )

    print("1. an MSI that declares no ProductName still gets the presets rescued")
    plan = plan_for(user_dir, "")
    rows, saved_for, looked_at = presets_mod.rescue_for_plan(
        env, plan, rescue_root=rescue_root, stamp="demo")
    check("the rescue ran at all", bool(rows), True)
    check("and looked at the product the plan is about to touch",
          "Archetype Test X" in looked_at, True)
    check("something was copied", sum(1 for r in rows if r[0] in ("saved", "kept")), 4)
    check("including the pack whose folder is not named 'preset'",
          any("Downloaded Pack.xml" in r[1] or "Packs" in r[2] for r in rows), True)

    print("2. those copies really are copies: the removal still deletes the originals")
    removed = installer.remove_files(plan, env)
    check("the user's own folder was deleted", user_dir.exists(), False)
    check("the removal reported it", any(r[0] == "removed" for r in removed), True)
    rescued = sorted(p.name for p in Path(saved_for[0][1]).rglob("*.xml"))
    check("and every preset is under ~/.local/share/wpt/presets",
          rescued, ["Downloaded Pack.xml", "My Sound 0.xml", "My Sound 1.xml", "My Sound 2.xml"])

    print("3. an MSI whose ProductName differs from the folder is rescued too")
    plant_user_presets(env, "Archetype Test X")
    plan = plan_for(product_root / "User", "Archetype Test X Plugin")
    rows, saved_for, _ = presets_mod.rescue_for_plan(
        env, plan, rescue_root=rescue_root, stamp="demo2")
    check("the presets were copied despite the mismatched name",
          sum(1 for r in rows if r[0] in ("saved", "kept")), 4)

    print("4. nothing to rescue is reported as looked-at, not as silence")
    empty_home = build_prefix(root / "empty")
    empty_env = detect(home=empty_home, prefix=empty_home / ".wine-ableton")
    nowhere = Plan(msi=root / "package.msi",
                   identity=MsiIdentity(product_name="", manufacturer="Neural DSP"),
                   expected={}, actions=[]
                   )
    rows, saved_for, looked_at = presets_mod.rescue_for_plan(
        empty_env, nowhere, rescue_root=rescue_root)
    check("no rows", rows, [])
    check("and nothing found to look at", looked_at, [])
    check("so the caller can say it looked and found nothing", len(looked_at), 0)

    print("5. a disabled bundle directory is a removal target, and is reported as a leftover")
    prefix = home / ".wine-ableton"
    vst3 = env.vst3_dir
    bundle = vst3 / "Archetype Test X.vst3"
    renamed = vst3 / "Archetype Test X.vst3.disabled"
    renamed.mkdir(parents=True)
    (renamed / "module.bin").write_bytes(b"x" * 128)
    source = root / "bundle-source"
    source.mkdir(exist_ok=True)
    plan = Plan(msi=root / "package.msi", identity=MsiIdentity(product_name="X"), expected={},
                actions=[Action(source=source, dest=bundle, label="VST3DIR")])
    check("both names are removal targets",
          [p.name for p in installer._removal_targets(plan.actions[0])],
          ["Archetype Test X.vst3", "Archetype Test X.vst3.disabled"])
    check("the left-behind renamed bundle is visible before the removal",
          [p.name for p in installer.leftovers(plan, env)], ["Archetype Test X.vst3.disabled"])
    rows = installer.remove_files(plan, env)
    check("and it is removed", renamed.exists(), False)
    check("reported as a removed directory",
          [r[0] for r in rows if "disabled" in r[1]], ["removed"])

    print("6. two same-named MSIs from different products stage into separate directories")
    scratch = home / ".cache" / "wpt" / "extract"
    product_a = root / "a"
    product_b = root / "b"
    for folder, size in ((product_a, 20), (product_b, 40)):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "setup.msi").write_bytes(b"m" * size)
        (folder / "setup.cab").write_bytes(b"c" * size)
    staged_a = msi_mod.stage_msi(product_a / "setup.msi", scratch)
    staged_b = msi_mod.stage_msi(product_b / "setup.msi", scratch)
    check("the two staged copies are in different directories",
          staged_a.parent == staged_b.parent, False)
    check("each kept its own cabinet",
          ((staged_a.parent / "setup.cab").stat().st_size,
           (staged_b.parent / "setup.cab").stat().st_size), (20, 40))
    check("and each staged MSI matches its own source",
          ((staged_a.stat().st_size, staged_b.stat().st_size)), (20, 40))
    check("re-staging the same MSI reuses its directory",
          msi_mod.stage_msi(product_a / "setup.msi", scratch), staged_a)

    print("7. a package's cabinet is found where it actually is, not only beside the MSI")
    # Wine's installer cache keeps the MSI without its payload: the cabinet stays in the wrapper's
    # install directory. A cached copy used to be unreadable for want of a cabinet sitting in the
    # same prefix, so the search has to cover the prefix - found on a real one, 2026-09-27.
    cabinet_prefix = root / "prefix"
    installer_cache = cabinet_prefix / "drive_c" / "windows" / "Installer"
    onthedisk = cabinet_prefix / "drive_c" / "users" / "tester" / "AppData" / "Roaming" / "Neural DSP" / "Product 1.0" / "install"
    installer_cache.mkdir(parents=True)
    onthedisk.mkdir(parents=True)
    cached_msi = installer_cache / "abba.msi"
    cached_msi.write_bytes(b"an msi that needs a cabinet")
    payload_cab = onthedisk / "Product1.cab"
    payload_cab.write_bytes(b"c" * 4096)

    check("the prefix is discovered from the MSI's own path",
          msi_mod.cabinet_search_roots(cached_msi)[1:],
          [installer_cache, cabinet_prefix / "drive_c" / "ProgramData", cabinet_prefix / "drive_c" / "users"])
    check("and the cabinet is found in it",
          msi_mod.find_cabinet("Product1.cab", msi_mod.cabinet_search_roots(cached_msi)[1:]),
          payload_cab)

    staging_scratch = cabinet_prefix / "scratch"
    staged_cached = msi_mod.stage_msi(cached_msi, staging_scratch, cabinets=["Product1.cab"])
    check("staging brings the named cabinet with the MSI",
          (staged_cached.parent / "Product1.cab").is_file(), True)
    check("and it is byte-for-byte the same",
          (staged_cached.parent / "Product1.cab").read_bytes(), payload_cab.read_bytes())

    print("8. a cabinet that is genuinely gone is refused, by name")
    try:
        msi_mod.stage_msi(cached_msi, staging_scratch, cabinets=["Vanished1.cab"])
        check("it raised", "no exception", "MsiError")
    except msi_mod.MsiError as exc:
        check("it raised an MsiError", True, True)
        check("naming the cabinet that is missing", "Vanished1.cab" in str(exc), True)
        check("saying nothing was removed", "nothing has been removed" in str(exc), True)

print(f"\npreset rescue checks: {checks - len(failures)}/{checks} passed")
if failures:
    for line in failures:
        print("  FAILED:", line)
    sys.exit(1)
