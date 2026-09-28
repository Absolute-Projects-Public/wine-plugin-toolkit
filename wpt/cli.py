"""Command line interface: wpt <command>

Kept deliberately scriptable -- every command prints plain text and exits
non-zero on failure, so the GUI is a thin wrapper over the same functions
rather than a separate implementation.
"""

from __future__ import annotations

import argparse
import tempfile
import os
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from . import catalogue as catalogue_mod
from . import completions as completions_mod
from . import updates as updates_mod
from . import doctor as doctor_mod
from . import installer as installer_mod
from . import installers as installers_mod
from . import inventory as inventory_mod
from . import msi as msi_mod
from . import presets as presets_mod
from . import products as products_mod
from . import scan as scan_mod
from . import sources as sources_mod
from . import wrappers as wrappers_mod
from .environment import EnvironmentError_, detect
from .installer import (
    apply_plan,
    build_plan,
    build_plan_from_tables,
    filter_needing_repair,
    render_plan,
    set_enabled as set_plugin_enabled,
    uninstall as uninstall_product,
    verify_plan,
)
from .scan import render as render_scan
from .scan import scan as scan_prefix

DEFAULT_SCRATCH = "/tmp/wpt-extract"

PURGE_WARNING = """\
--purge also removes matching product registry entries that point at planned files:

  - selected values pointing at planned destinations (which may now be gone)
  - the product's own Uninstall / InstallProperties entry, if it has one

This can clear stale pointers reported by `wpt scan`; rerun it to see what remains.
It does NOT free an activation. Files can be deleted; activations cannot:

  * If this plugin was activated here (iLok / PACE, or a vendor account) and you will
    not use this prefix again, deleting the files does not return the licence slot.
  * Deactivate it in iLok License Manager -- or, when the location is unreachable or
    already broken, use 'Report as Unusable' there so the slot comes back.
  * Otherwise the slot stays consumed and the plugin may refuse to authorise on the
    machine you actually use.

The uninstall may already have affected user files: msiexec /x runs before the
toolkit's best-effort preset rescue, and planned directories are removed
recursively. The rescue copies recognised presets only; --no-rescue skips it.
Use a separate backup before any uninstall, not just this rescue store."""


def _env(args):
    return detect(home=args.home, prefix=args.prefix, wine_tree=args.tree, user=args.user)


def cmd_env(args) -> int:
    env = _env(args)
    width = max(len(k) for k in env.describe())
    print("Wine plugin stack")
    print("-" * 40)
    for key, value in env.describe().items():
        print(f"{key.ljust(width)} : {value}")
    if msi_mod.missing_tools():
        print(f"\n! msitools missing: {', '.join(msi_mod.missing_tools())}")
        print("  Arch/CachyOS:  sudo pacman -S msitools")
        print("  Debian/Ubuntu: sudo apt install msitools")
    return 0


def cmd_find_msi(args) -> int:
    env = _env(args)
    found = msi_mod.find_extracted_msis(
        env.prefix, hint=args.product, include_installer_cache=True
    )
    if not found:
        print("no vendor MSIs found inside the prefix (has the wrapper been run at least once?)")
        return 1
    for path in found:
        try:
            ident = msi_mod.identity(path)
            label = ident.label
        except Exception:  # noqa: BLE001 - a bare listing must not fail on one bad MSI
            label = "?"
        print(f"{label:32}  {path}")
    return 0


def cmd_inspect(args) -> int:
    msi = Path(args.msi).expanduser()
    if not msi.is_file():
        print(f"not a file: {msi}", file=sys.stderr)
        return 2
    ident = msi_mod.identity(msi)
    print(f"Product       {ident.product_name} {ident.product_version}")
    print(f"Manufacturer  {ident.manufacturer}")
    print(f"ProductCode   {ident.product_code}")
    print(f"UpgradeCode   {ident.upgrade_code}")
    print("\nDeclared targets:")
    for key, value in sorted(msi_mod.directory_properties(msi).items()):
        print(f"  {key:12} {value}")

    conditions = msi_mod.launch_conditions(msi)
    print(f"\nLaunch conditions ({len(conditions)}):")
    for condition, description in conditions:
        print(f"  {condition}")
        print(f"      {description}" if description else "")
    bootstrap = msi_mod.bootstrap_only(msi)
    if bootstrap:
        print("\n! BOOTSTRAPPER-ONLY CONDITION PRESENT")
        print(f"  {bootstrap[0]}")
        print("  A bare 'msiexec /i' cannot satisfy this and will fail with 1603.")
        print("  Install the MSI with msiextract instead (wpt plan / wpt install).")

    files = msi_mod.payload_files(msi)
    print(f"\nPayload ({len(files)} files):")
    for item in sorted(files, key=lambda f: -f.size)[:12]:
        print(f"  {item.size:>12,}  {item.name}")
    return 0


def cmd_plan(args) -> int:
    env = _env(args)
    msi = Path(args.msi).expanduser()
    scratch = Path(args.scratch).expanduser()
    print(f"extracting {msi.name} -> {scratch} ...")
    msi_mod.extract(msi, scratch)
    plan = build_plan(
        msi,
        env,
        scratch,
        include_vst2=not args.no_vst2,
        include_aax=args.aax,
        include_standalone=not args.no_standalone,
        include_presets=not args.no_presets,
    )
    print()
    print(render_plan(plan))
    print("\nnothing was written (this was a plan). Add --apply to execute it.")
    return 0


def cmd_install(args) -> int:
    env = _env(args)
    scratch = Path(args.scratch).expanduser()
    if args.msi:
        candidate = Path(args.msi).expanduser()
        if not candidate.is_file():
            print(f"not a file: {candidate}", file=sys.stderr)
            return 2
        # the vendor ships a .exe wrapper around the MSI; bridge the two here so nobody
        # has to know or care which one they downloaded
        prepared = wrappers_mod.prepare_msi(
            env,
            candidate,
            scratch,
            product_hint=args.product or candidate.stem,
            allow_wine=args.run_wrapper,
            log=lambda message: print(f"  {message}"),
        )
        print(f"  {prepared.detail}")
        if not prepared.ok:
            print("! no MSI to install from", file=sys.stderr)
            return 2
        msi = prepared.msi
    else:
        # no MSI given: use a vendor-cached one. Wine's msiexec cache is searched too, but
        # only as a fallback - picking the newest MSI in the system cache could install a
        # runtime rather than the plugin the caller had in mind.
        candidates = [p for p in msi_mod.find_extracted_msis(env.prefix, hint=args.product)]
        if not candidates:
            candidates = [
                p for p in msi_mod.find_extracted_msis(
                    env.prefix, hint=args.product, include_installer_cache=True
                )
            ]
        if not candidates:
            print("no MSI given and none found in the prefix", file=sys.stderr)
            return 2
        msi = candidates[0]
        print(f"using MSI found in the prefix: {msi}")

    msi_mod.extract(msi, scratch)
    plan = build_plan(
        msi,
        env,
        scratch,
        include_vst2=not args.no_vst2,
        include_aax=args.aax,
        include_standalone=not args.no_standalone,
        include_presets=not args.no_presets,
        include_app_files=getattr(args, "include_app_files", False),
    )
    print(render_plan(plan))
    print()

    if not plan.actions:
        if plan.skipped_app:
            print(
                "nothing a DAW can use: this package is an application or a driver, not a plugin.",
                file=sys.stderr,
            )
            for name, dest in plan.skipped_app:
                print(f"  {name} payload would go to {dest}", file=sys.stderr)
            print(
                "  Its own installer (or the vendor's .exe under Wine) is what installs it\n"
                "  properly - drivers need their INF and service registration, which placing\n"
                "  files by hand does not do. Pass --include-app-files to place them anyway.",
                file=sys.stderr,
            )
        else:
            print("nothing to place: this MSI declares no payload this toolkit recognises.", file=sys.stderr)
        return 1

    if args.dry_run:
        results = apply_plan(plan, env, dry_run=True)
        for status, path, note in results:
            print(f"  {status:8} {path}")
        print(f"\ndry run: {len(results)} destinations would be written. Nothing changed.")
        return 0

    results = apply_plan(plan, env, dry_run=False)
    failures = 0
    for status, path, note in results:
        if status == "failed":
            failures += 1
        print(f"  {status:8} {path}   {note}")

    print("\nverifying against the MSI File table ...")
    checks = verify_plan(plan)
    bad = 0
    for status, path, note in checks:
        if status != "ok":
            bad += 1
            print(f"  {status:14} {path}   {note}")
    print(f"  {sum(1 for c in checks if c[0] == 'ok')} files verified byte-for-byte")
    if failures or bad:
        print(f"\n! {failures} copy failures, {bad} verification failures", file=sys.stderr)
        return 1
    print("\ninstall complete - rescan plugins in your DAW")
    return 0


def open_in_browser(url: str, timeout: int = 15) -> bool:
    """Open a URL with the desktop's handler, honestly.

    `xdg-open` is not guaranteed to exist, and it can block; a command that prints "opened"
    regardless is worse than one that says it could not. The GUI uses QDesktopServices, whose
    result is checked, and this is the CLI's equivalent.
    """
    import webbrowser
    try:
        if webbrowser.open(url):
            return True
    except Exception:  # noqa: BLE001 - any failure here means "could not open"
        pass
    opener = shutil.which("xdg-open")
    if not opener:
        return False
    try:
        proc = subprocess.run([opener, url], timeout=timeout, capture_output=True)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def cmd_catalogue(args) -> int:
    if getattr(args, "open_page", False):
        import webbrowser

        opened = webbrowser.open(catalogue_mod.DOWNLOADS_URL)
        print(f"{'opened' if opened else 'could not open'} {catalogue_mod.DOWNLOADS_URL}")
        return 0

    try:
        catalogue = (catalogue_mod.refresh(log=print) if args.refresh
                     else catalogue_mod.load_snapshot())
    except catalogue_mod.CatalogueError as exc:
        print(f"could not read the catalogue: {exc}", file=sys.stderr)
        return 1
    if not catalogue.releases:
        print("no catalogue available (bundled snapshot missing) - try --refresh", file=sys.stderr)
        return 1

    if args.open:
        matches = catalogue.find(args.open)
        if not matches:
            print(f"no product matching '{args.open}' in the catalogue", file=sys.stderr)
            return 1
        release = matches[0]
        print(f"{release.product} {release.version} ({release.released})")
        print(f"  {release.windows}")
        if release.needs_sign_in:
            print("  Neural DSP requires you to be signed in before it hands over the file.")
        if not open_in_browser(release.windows):
            print(f"  could not open a browser - open it yourself: {release.windows}", file=sys.stderr)
            return 1
        print("  opened in your browser; the download lands in ~/Downloads")
        return 0

    if args.download:
        matches = catalogue.find(args.download)
        if not matches:
            print(f"no product matching '{args.download}' in the catalogue", file=sys.stderr)
            return 1
        release = matches[0]
        if not release.direct_download:
            print(f"{release.product} needs a signed-in download, so wpt cannot fetch it:")
            print(f"  {release.windows}")
            print("  open that in your browser (`wpt catalogue --open`) and let it land in ~/Downloads,")
            print("  then `wpt install <the file>` - or use the Pending Install tab in the GUI.")
            return 1
        destination = Path(args.directory).expanduser()
        try:
            path = catalogue_mod.download(release.windows, destination, log=print)
        except catalogue_mod.CatalogueError as exc:
            print(f"download failed: {exc}", file=sys.stderr)
            return 1
        print(f"\n{path}")
        return 0

    if args.json:
        import json

        print(
            json.dumps(
                [
                    {
                        "product": r.product,
                        "slug": r.slug,
                        "version": r.version,
                        "released": r.released,
                        "group": r.group,
                        "windows": r.windows,
                        "macos": r.macos,
                        "manual": r.manual,
                        "needs_sign_in": r.needs_sign_in,
                        "direct_download": r.direct_download,
                    }
                    for r in catalogue.releases
                ],
                indent=2,
            )
        )
        return 0

    print(catalogue_mod.render(catalogue, limit=args.limit))
    return 0


def cmd_presets(args) -> int:
    # where to get *more* presets: nothing is fetched, the page opens in your own browser
    if getattr(args, "sources", False) or getattr(args, "open", None):
        if args.open:
            opened, url = sources_mod.open_source(args.open, args.product or "")
            if not url:
                print(f"no source called '{args.open}' - try: wpt presets --sources", file=sys.stderr)
                return 2
            print(f"{'opened' if opened else 'could not open'} {url}")
            return 0
        print(sources_mod.render())
        return 0

    env = _env(args)
    if args.export:
        destination = Path(args.export).expanduser()
        rows = presets_mod.export(env, destination, vendor=args.vendor)
        if not rows:
            print("nothing to export: no user presets or downloaded packs in this prefix")
            return 0
        for status, source, note in rows:
            print(f"  {status:8} {source}   {note}")
        print(f"\n{sum(1 for r in rows if r[0] == 'exported')} file(s) exported to {destination}")
        return 0
    print(presets_mod.render(env, vendor=args.vendor))
    return 0


def cmd_products(args) -> int:
    env = _env(args)
    reports = products_mod.triage(env, home=Path(args.home).expanduser() if args.home else None)
    if args.json:
        import json

        print(
            json.dumps(
                [
                    {
                        "prefix": str(r.path),
                        "kind": r.kind,
                        "plugin_files": len(r.plugin_files),
                        "registry_paths": len(r.registry_plugin_paths),
                        "products": [
                            {
                                "name": p.name,
                                "version": p.version,
                                "publisher": p.publisher,
                                "verdict": p.verdict,
                                "installed_at": p.installed_at,
                                "date": p.date if p.installed_at else None,
                                "system": p.system,
                                "registered_paths": [str(x) for x in p.registered_paths],
                                "missing_paths": [str(x) for x in p.missing_paths],
                                "on_disk": [str(x) for x in p.on_disk],
                                "elsewhere": [{"path": str(x), "prefix": str(o)} for x, o in p.elsewhere],
                            }
                            for p in r.products
                            if args.all_products or not p.system
                        ],
                    }
                    for r in reports
                ],
                indent=2,
            )
        )
        return 0
    print(products_mod.render(reports, all_products=args.all_products, limit=args.limit))
    return 0


def cmd_scan(args) -> int:
    env = _env(args)
    report = scan_prefix(env, include_other=args.include_other)
    print(render_scan(report, limit=args.limit, all_products=args.all_products))
    return 1 if report.missing else 0


def cmd_list(args) -> int:
    env = _env(args)
    inv = inventory_mod.build(env, include_standalone=not args.no_standalone)
    if args.json:
        import json

        print(
            json.dumps(
                {
                    "entries": [
                        {
                            "name": e.name,
                            "kind": e.kind,
                            "path": str(e.path),
                            "size": e.size,
                            "expected_size": e.expected_size,
                            "integrity": e.integrity,
                            "msi": str(e.msi) if e.msi else None,
                        }
                        for e in inv.entries
                    ],
                    "cached_msis": [str(m) for m in inv.msis],
                    "warnings": inv.warnings,
                },
                indent=2,
            )
        )
        return 1 if inv.broken else 0
    print(
        inventory_mod.render(
            inv, limit=args.limit, all_standalone=getattr(args, "all_standalone", False)
        )
    )
    return 1 if inv.broken else 0


def _pick_msi(env, args):
    """Resolve which MSI to work on: explicit path, substring match, or newest."""
    if getattr(args, "msi", None):
        return Path(args.msi).expanduser()
    candidates = msi_mod.find_extracted_msis(
        env.prefix, hint=getattr(args, "product", None), include_installer_cache=True
    )
    if not candidates:
        return None
    return candidates[0]


def cmd_repair(args) -> int:
    env = _env(args)
    msi = _pick_msi(env, args)
    if msi is None:
        print("no MSI found for that selection (nothing to repair from)", file=sys.stderr)
        return 2
    print(f"repairing {msi.name}")

    scratch = Path(args.scratch).expanduser()
    msi_mod.extract(msi, scratch)
    plan = build_plan(
        msi,
        env,
        scratch,
        include_vst2=not args.no_vst2,
        include_aax=args.aax,
        include_standalone=not args.no_standalone,
        include_presets=not args.no_presets,
    )
    todo = filter_needing_repair(plan)
    if not todo.actions:
        print("no repair actions selected by the current size matcher; this is not a complete file audit")
        return 0
    print(f"{len(todo.actions)} destination(s) missing or wrong size:")
    for action in todo.actions:
        print(f"  {action.dest}")

    results = apply_plan(todo, env, dry_run=args.dry_run)
    failures = 0
    for status, path, note in results:
        if status == "failed":
            failures += 1
        print(f"  {status:8} {path}   {note}")
    if args.dry_run:
        print("\ndry run - nothing changed")
        return 0

    print("\nverifying ...")
    bad = [c for c in verify_plan(todo) if c[0] != "ok"]
    for status, path, note in bad:
        print(f"  {status:14} {path}   {note}")
    if failures or bad:
        print(f"! {failures} copy failures, {len(bad)} verification failures", file=sys.stderr)
        return 1
    print("repaired and verified")
    return 0


def cmd_uninstall(args) -> int:
    """Remove a product: Windows Installer registration, then the files its MSI placed.

    **Order matters, and it cost us a real uninstall to learn it.** `msiexec /x` deletes the copy
    of the package that Windows Installer keeps in `drive_c/windows/Installer/`, and on this
    stack that cache is often the only copy a product has (a wrapper installed via Wine never
    writes one into its own vendor folder). So everything the MSI can tell us, its ProductCode,
    its File table, the plan, is read **first**, a copy of the MSI is staged out of reach, and
    only then is msiexec run. If the MSI cannot be read at all, this fails before touching
    anything rather than halfway through.
    """
    env = _env(args)
    scratch = Path(args.scratch).expanduser()
    msi = _pick_msi(env, args)
    product_code = args.product_code
    plan = None

    # ------------------------------------------------------------------ read first
    if msi is not None and not args.no_files:
        try:
            staged = msi_mod.stage_msi(msi, scratch, require_cabinets=False)
        except (OSError, msi_mod.MsiError) as exc:
            print(
                f"cannot read {msi.name}: {exc}\n"
                "  nothing has been touched. If Windows Installer has already removed its cached "
                "copy of this MSI, pass --product-code and use --no-files to clear the "
                "registration alone.",
                file=sys.stderr,
            )
            return 4
        if staged != msi:
            print(
                f"reading {msi.name} before msiexec runs (a copy, with any sidecar cabinets, is kept in "
                f"{staged.parent})"
            )
        msi = staged

    if not product_code:
        if msi is None:
            print("need an MSI (for its ProductCode) or --product-code", file=sys.stderr)
            if args.product:
                # the dead end: a product whose MSI has already been consumed by msiexec
                stale = installer_mod.product_leftovers(env, args.product, vendor=args.vendor)
                print(
                    f"\nno MSI for '{args.product}' is left in the prefix. Windows Installer deletes\n"
                    "its cached copy of the package as part of removing a product, so if that was the\n"
                    "only copy there is nothing left to derive a file list from.",
                    file=sys.stderr,
                )
                if stale:
                    print(f"\nwhat the prefix still holds under that name ({len(stale)}):", file=sys.stderr)
                    for path in stale:
                        print(f"    {path}", file=sys.stderr)
                    print(
                        "\nRemove them by hand if you want them gone, or run the vendor's installer once\n"
                        "to put a fresh MSI in the prefix (then this command can do it properly).",
                        file=sys.stderr,
                    )
                else:
                    print("  nothing under that name is on disk - the uninstall left no traces.", file=sys.stderr)
            return 2
        product_code = msi_mod.identity(msi).product_code
        if not product_code:
            print(f"no ProductCode found in {msi}", file=sys.stderr)
            return 2
        print(f"{msi_mod.identity(msi).label}  ->  {product_code}")

    if not args.no_files:
        if msi is None:
            print("no MSI available, so the file list is unknown - pass one to remove files", file=sys.stderr)
            return 2
        options = {
            "include_vst2": True,
            "include_aax": True,
            "include_standalone": True,
            "include_presets": True,
        }
        try:
            msi_mod.extract(msi, scratch)
        except msi_mod.MsiError as exc:
            # The cabinet is what extracting needs, and Wine's installer cache does not keep it.
            # The MSI's own tables name every file it placed, so the removal can still be exact --
            # this is the Fortin Cali Suite case: installed, working, its media gone, and an
            # uninstall used to be impossible.
            try:
                plan = build_plan_from_tables(msi, env, **options)
            except msi_mod.MsiError:
                print(f"cannot read the file list out of {msi.name}: {exc}", file=sys.stderr)
                print("  nothing has been touched yet", file=sys.stderr)
                return 4
            print(f"the payload media for {msi.name} is gone:")
            print(f"  {exc}")
            print("  the file list comes from the MSI's own tables instead - the removal below is")
            print("  unchanged. Toolkit file removal targets the prefix, but Wine and scratch may")
            print("  affect other paths. Running the vendor's installer once puts its payload back.")
        else:
            plan = build_plan(msi, env, scratch, **options)
        print(f"this MSI describes {len(plan.actions)} destination(s)")

    # `msiexec /x` only works if Windows Installer knows the product. Say so up
    # front rather than reporting a removal that never happened: the exit code of
    # msiexec on an unregistered product is not a reliable signal either way.
    registered = scan_mod.is_registered(env, product_code)
    if registered:
        print("registered with Windows Installer in this prefix -> msiexec /x")
    else:
        print("not registered with Windows Installer in this prefix:")
        print("  msiexec /x has nothing to remove, so the files have to go directly")

    print("warning: back up your own files first. msiexec /x may run before preset rescue; "
          "planned directories are removed recursively, including user-added files.")
    print("deleting plugin files does not return an iLok activation; deactivate it separately.")
    failures = 0

    if not args.files_only:
        code, detail = uninstall_product(env, product_code, dry_run=args.dry_run)
        if detail:
            print(detail)
        if not args.dry_run and code != 0:
            if registered:
                failures += 1
                print(f"msiexec exited {code} although the product was registered", file=sys.stderr)
            else:
                print(f"msiexec exited {code}, as expected with no registration to clear")

    # The real work on this stack: delete exactly what the MSI's File table placed.
    if not args.no_files and plan is not None:
        if args.rescue_presets:
            # Same rule as the GUI: the products to look at come from the plan and the vendor tree,
            # not from the MSI's ProductName, which can be absent or differ from the folder the
            # vendor's installer created. `--product` selects an MSI and is not a product name.
            saved, saved_for, looked_at = presets_mod.rescue_for_plan(
                env,
                plan,
                vendor=args.vendor,
                dry_run=args.dry_run,
            )
            if saved:
                copied = sum(1 for r in saved if r[0] in ("saved", "dry-run"))
                print(
                    f"\npresets: {copied} file(s) "
                    + ("would be copied" if args.dry_run else "copied")
                    + f", {sum(1 for r in saved if r[0] == 'kept')} already rescued"
                )
                for status, source, note in saved[:12]:
                    print(f"  {status:8} {source}   {note}")
                if len(saved) > 12:
                    print(f"  ... and {len(saved) - 12} more")
                for _product, rescue_dir in saved_for:
                    print(f"  kept in: {rescue_dir}")
            else:
                # "none found" has to be told apart from "we did not look", and it must not read as
                # reassurance: the removal below deletes whatever the MSI's File table names.
                print("\npresets: nothing found to rescue, and the files below are still deleted")
                print("  looked at: "
                      + (", ".join(looked_at) if looked_at else "no product folder in this prefix"))
                print("  if this product keeps your own presets here, rescue them by hand first")

        rows = installer_mod.remove_files(plan, env, dry_run=args.dry_run)
        for status, path, note in rows:
            print(f"  {status:8} {path}   {note}" if path else f"  {status:8} {note}")
        if not rows:
            print("  nothing to remove: no file this MSI describes is on disk")
            if registered and not args.files_only:
                print("  (msiexec removed them itself - a registered product's uninstall does that)")

        if not args.dry_run:
            remaining = installer_mod.leftovers(plan, env)
            if remaining:
                failures += 1
                print(f"\n! {len(remaining)} path(s) this MSI describes are still on disk:", file=sys.stderr)
                for path in remaining:
                    print(f"    {path}", file=sys.stderr)
            else:
                print("\nno planned destination remains on disk; nested/unowned files are not audited")

        if args.purge:
            print()
            print(PURGE_WARNING)
            edits = installer_mod.stale_registry_edits(env, plan, product_code)
            if not edits:
                print("  nothing to purge: no registry entry points at this product's files")
            for edit in edits:
                print(f"    {edit.hive}\\{edit.key}" + (f"  [{edit.value}]" if edit.value else ""))
                print(f"        {edit.reason}")
            if edits and not args.dry_run:
                purged = installer_mod.purge_registry(env, edits)
                for status, target, note in purged:
                    print(f"  {status:8} {target}   {note}")
                failed = [r for r in purged if r[0] == "failed"]
                if failed:
                    failures += 1
                    print(f"! {len(failed)} registry edit(s) failed", file=sys.stderr)
                else:
                    print(f"  {len(purged)} registry entr{'y' if len(purged) == 1 else 'ies'} removed")
            elif edits:
                print("  (dry run: none of the above was applied)")

    if args.dry_run:
        print("\ndry run: nothing was deleted and no registry entry was changed.")
        return 0
    if failures:
        return 1
    print("done. Re-run 'wpt scan' to confirm nothing is left pointing at the removed files.")
    return 0


def cmd_pending(args) -> int:
    env = _env(args)
    extra = [Path(p).expanduser() for p in (args.dir or [])]
    found = installers_mod.discover(env, extra_dirs=extra)
    print(installers_mod.render(found))
    if args.json:
        import json

        print()
        print(
            json.dumps(
                [
                    {
                        "path": str(i.path),
                        "product": i.product,
                        "version": i.version,
                        "kind": i.kind,
                        "size": i.size,
                        "installed": i.installed,
                        "status": i.status,
                    }
                    for i in found
                ],
                indent=2,
            )
        )
    return 0


def cmd_toggle(args) -> int:
    env = _env(args)
    rows = set_plugin_enabled(env, args.name, enabled=args.enable, dry_run=args.dry_run)
    if not rows:
        # Distinguish "nothing matched that name" (a typo - worth a non-zero exit) from "it is
        # already in the state you asked for" (nothing to do, and exit 1 for that is just noise).
        inventory = inventory_mod.build(env, include_standalone=False)
        matches = [e for e in inventory.entries if args.name.lower() in e.name.lower()]
        if matches:
            state = "enabled" if args.enable else "disabled"
            print(f"'{args.name}' is already {state} - nothing to change")
            return 0
        print(f"no {'disabled ' if args.enable else ''}plugin matching '{args.name}' found", file=sys.stderr)
        return 1
    for status, path, note in rows:
        print(f"  {status:8} {path}   {note}")
    if not args.dry_run:
        print("\nrescan plugins in your DAW for the change to take effect")
    return 0


def cmd_update(args) -> int:
    """Ask GitHub whether a newer release exists, and optionally fetch it for pacman."""
    if os.environ.get("WPT_NO_UPDATE_CHECK") and not args.force:
        print("update check disabled (WPT_NO_UPDATE_CHECK is set; --force overrides)")
        return 0

    release, error = None, None
    if not args.cached:
        try:
            release = updates_mod.latest_release()
        except updates_mod.UpdateError as exc:
            error = str(exc)
        updates_mod.write_cache(release, error)
    else:
        cached = updates_mod.read_cache()
        if cached is None:
            print("no cached result yet - run 'wpt update' (without --cached) once")
            return 0
        error = cached.get("error")
        if cached.get("tag"):
            release = updates_mod.Release(tag=cached["tag"],
                                          version=tuple(cached.get("version") or ()),
                                          html_url=cached.get("html_url") or "")

    stale = bool(error) or release is None or         not updates_mod.is_newer(release.version, updates_mod.parse_version(updates_mod.__version__))

    if args.json:
        print(json.dumps({
            "current": updates_mod.__version__,
            "latest": release.tag if release else None,
            "latest_version": release.version_text if release else None,
            "release_url": release.html_url if release else None,
            "assets": [a.name for a in release.assets] if release else [],
            "update_available": not stale,
            "error": error,
            "repository": updates_mod.REPO,
        }, indent=2))
    else:
        for line in updates_mod.status_lines(release, error, updates_mod.is_arch_family()):
            print(line)
        if not stale:
            print("  " + updates_mod.disable_hint())

    if stale or not args.install:
        return 0 if not error else 1

    # --- an update is available and was asked for
    if not updates_mod.is_arch_family():
        print(f"\nThis is not an Arch-family system, so there is no package to install.\n"
              f"Source tarball and instructions: {release.html_url}")
        return 1

    asset = release.asset(package=True)
    if asset is None:
        print(f"\n{release.tag} publishes no Arch package - see {release.html_url}")
        return 1

    dest = Path(args.download_dir or tempfile.mkdtemp(prefix="wpt-update-")).expanduser()
    print(f"\ndownloading {asset.name} to {dest}")
    try:
        path = updates_mod.download(asset, dest,
                                    progress=lambda done, total: print(
                                        f"  {done / 1048576:.1f}/{total / 1048576:.1f} MB" if total else
                                        f"  {done / 1048576:.1f} MB", end="\r", flush=True))
        print()
        updates_mod.validate_package(path)
        want = updates_mod.expected_sha256(release, asset, dest)
        if want:
            got = updates_mod.sha256(path)
            if got != want:
                path.unlink(missing_ok=True)
                print(f"checksum mismatch: release says {want}, file is {got} - deleted, nothing installed")
                return 1
            print(f"sha256 {got[:16]}... matches the release")
        else:
            print(f"sha256 {updates_mod.sha256(path)[:16]}... (the release publishes no checksum)")
    except updates_mod.UpdateError as exc:
        print(f"update failed: {exc}")
        return 1

    if args.install == "show":
        print(f"\ninstall it with:\n  sudo pacman -U {path}")
        return 0

    if args.install == "run":
        ok, message = updates_mod.launch_install(path, restart=not args.no_restart)
        print(("\n" if ok else "\nupdate failed: ") + message)
        return 0 if ok else 1

    print(f"\ninstall it with:\n  sudo pacman -U {path}")
    return 0


def cmd_doctor(args) -> int:
    """Say what this machine has, and what is wrong with it. Read-only, ever."""
    report = doctor_mod.run(scratch=Path(args.scratch).expanduser() if args.scratch else None)
    print(doctor_mod.as_json(report) if args.json else doctor_mod.render(report))
    return report.exit_code


def cmd_completions(args) -> int:
    """Print a completion script for your shell, generated from the real parser."""
    if args.shell not in completions_mod.SHELLS:
        print(f"unknown shell '{args.shell}' - try one of: {', '.join(completions_mod.SHELLS)}", file=sys.stderr)
        return 2
    print(completions_mod.generate(args.shell, build_parser(), program="wpt"), end="")
    return 0


def cmd_wrappers(args) -> int:
    """Identify vendor .exe wrappers, and say which route would yield their MSI."""
    targets: list[Path] = []
    if args.file:
        candidate = Path(args.file).expanduser()
        if not candidate.is_file():
            print(f"not a file: {candidate}", file=sys.stderr)
            return 2
        targets = [candidate]
    else:
        env = _env(args)
        search = [Path.home() / "Downloads", env.drive_c]
        for extra in args.dir or []:
            search.append(Path(extra).expanduser())
        for directory in search:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.exe")):
                try:
                    if path.stat().st_size <= args.min_size * 1024 * 1024:
                        continue
                except OSError:
                    continue        # gone since the listing
                targets.append(path)
        if not targets:
            print(f"no .exe wrappers over {args.min_size} MB in ~/Downloads or the prefix root")
            return 0

    infos = [wrappers_mod.identify(path) for path in targets]
    if args.json:
        payload = [
            {
                "file": str(info.path),
                "size": info.size,
                "is_msi": info.is_msi,
                "family": info.name,
                "linux_route_available": not info.needs_wine,
                "tools_present": [t for t in wrappers_mod.resolve_tools(info.tools) if shutil.which(t)],
                "tools_missing": [t for t in wrappers_mod.resolve_tools(info.tools) if not shutil.which(t)],
                "note": info.family.note if info.family else "",
            }
            for info in infos
        ]
        print(json.dumps({"wrappers": payload}, indent=2))
        return 0

    if args.file and not args.list:
        info = infos[0]
        print(wrappers_mod.render_info(info))
        missing = [t for t in info.tools if not shutil.which(t)]
        if missing:
            print(f"  missing     : {', '.join(missing)} (optional - only needed for this family)")
        if args.unpack:
            scratch = Path(args.scratch).expanduser()
            print("attempting to unpack on Linux (nothing is written outside the scratch dir) ...")
            result = wrappers_mod.unpack_on_linux(info.path, scratch, info=info)
            for tool, note in result.attempts:
                print(f"  {tool:<24} {note}")
            if result.ok:
                print(f"\nMSI: {result.msi}")
                print(f"install it with:  wpt install \"{result.msi}\"")
            else:
                print("\nno MSI could be obtained on Linux for this file"
                      + (f" (payload folders seen: {', '.join(result.payload_dirs)})" if result.payload_dirs else ""))
        return 0

    width = max(len(info.path.name) for info in infos)
    for info in infos:
        route = "wine only" if info.needs_wine else "linux ok"
        print(f"  {info.path.name.ljust(width)}  {info.size / 1_048_576:8.1f} MB  {info.name:<28} {route}")
    print()
    print("  wpt wrappers --file <path>   for one file in detail (add --unpack to try it)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wpt",
        description="Install and repair Windows audio plugins in an ableton-linux Wine prefix.",
    )
    parser.add_argument("--home", help="override $HOME")
    parser.add_argument("--prefix", help="Wine prefix (default $WINEPREFIX or ~/.wine-ableton)")
    parser.add_argument("--tree", help="Wine tree (default: newest ~/.local/opt/wine-d2d1-nspa-*)")
    parser.add_argument("--user", help="Windows user inside the prefix (default: auto-detect)")
    parser.add_argument("--version", action="version", version=f"wpt {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("env", help="show the detected stack").set_defaults(func=cmd_env)

    p_update = sub.add_parser("update", help="check GitHub for a newer release, and fetch it for pacman")
    p_update.add_argument("--install", nargs="?", const="show", choices=["show", "run"],
                          help="download the new package; 'run' opens a terminal to install it too")
    p_update.add_argument("--cached", action="store_true", help="use the result cached in the last 24 hours")
    p_update.add_argument("--json", action="store_true", help="machine-readable")
    p_update.add_argument("--force", action="store_true", help="check even if WPT_NO_UPDATE_CHECK is set")
    p_update.add_argument("--download-dir", help="where to put the downloaded package")
    p_update.add_argument("--no-restart", action="store_true",
                          help="do not reopen the toolkit after 'run' finishes installing")
    p_update.set_defaults(func=cmd_update)

    p_doctor = sub.add_parser("doctor", help="check everything this tool needs, and report what is wrong")
    p_doctor.add_argument("--scratch", help="the extraction dir to test (default /tmp/wpt-extract)")
    p_doctor.add_argument("--json", action="store_true", help="machine-readable report")
    p_doctor.set_defaults(func=cmd_doctor)

    p_complete = sub.add_parser("completions", help="print a shell completion script (fish, bash, zsh)")
    p_complete.add_argument("shell", choices=list(completions_mod.SHELLS))
    p_complete.set_defaults(func=cmd_completions)

    p_find = sub.add_parser("find-msi", help="list vendor MSIs unpacked inside the prefix")
    p_find.add_argument("--product", help="filter by substring, e.g. Rabea")
    p_find.set_defaults(func=cmd_find_msi)

    p_inspect = sub.add_parser("inspect", help="read an MSI: identity, targets, conditions, payload")
    p_inspect.add_argument("msi")
    p_inspect.set_defaults(func=cmd_inspect)

    p_wrap = sub.add_parser(
        "wrappers", help="identify vendor .exe wrappers and say which route yields their MSI"
    )
    p_wrap.add_argument("--file", help="one wrapper to inspect in detail")
    p_wrap.add_argument("--list", action="store_true", help="with --file, show the one-line summary instead")
    p_wrap.add_argument("--unpack", action="store_true", help="with --file, actually try unpacking it on Linux")
    p_wrap.add_argument("--dir", action="append", help="extra directory to scan (repeatable)")
    p_wrap.add_argument("--min-size", type=int, default=2, help="ignore .exe files under this many MB (default 2)")
    p_wrap.add_argument("--scratch", default=DEFAULT_SCRATCH, help=f"extraction dir ({DEFAULT_SCRATCH})")
    p_wrap.add_argument("--json", action="store_true")
    p_wrap.set_defaults(func=cmd_wrappers)

    p_plan = sub.add_parser("plan", help="extract and show exactly what would be installed")
    p_plan.add_argument("msi")
    p_plan.add_argument("--scratch", default=DEFAULT_SCRATCH, help=f"extraction dir ({DEFAULT_SCRATCH})")
    p_plan.add_argument("--no-vst2", action="store_true", help="skip the VST2 dll")
    p_plan.add_argument("--no-standalone", action="store_true", help="skip the standalone app")
    p_plan.add_argument("--no-presets", action="store_true", help="skip factory presets")
    p_plan.add_argument("--aax", action="store_true", help="include the AAX / Pro Tools plugin")
    p_plan.set_defaults(func=cmd_plan)

    p_install = sub.add_parser("install", help="extract, place, verify (accepts .msi or the vendor .exe wrapper)")
    p_install.add_argument("msi", nargs="?", help="MSI or vendor wrapper path; omit to use the newest found in the prefix")
    p_install.add_argument("--product", help="substring used to pick an MSI from the prefix")
    p_install.add_argument("--run-wrapper", action="store_true",
                           help="if the download is a .exe, run the vendor installer under Wine to get its MSI")
    p_install.add_argument("--scratch", default=DEFAULT_SCRATCH)
    p_install.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    p_install.add_argument("--no-vst2", action="store_true")
    p_install.add_argument("--no-standalone", action="store_true")
    p_install.add_argument("--no-presets", action="store_true")
    p_install.add_argument("--aax", action="store_true")
    p_install.add_argument("--include-app-files", action="store_true",
                           help="also place application/driver payloads (not plugins - a DAW will not see them)")
    p_install.set_defaults(func=cmd_install)

    p_scan = sub.add_parser("scan", help="find installs registered but missing from disk")
    p_scan.add_argument("--limit", type=int, default=40, help="rows per section")
    p_scan.add_argument("--include-other", action="store_true", help="also check non-plugin paths")
    p_scan.add_argument("--all-products", action="store_true", help="include runtime/system products")
    p_scan.set_defaults(func=cmd_scan)

    p_catalogue = sub.add_parser(
        "catalogue", help="the vendor's download list: what exists, current versions, where the installers are"
    )
    p_catalogue.add_argument("--refresh", action="store_true", help="re-read the vendor page (else the bundled snapshot)")
    p_catalogue.add_argument("--open", metavar="PRODUCT", help="open that product's download page in the browser")
    p_catalogue.add_argument("--open-page", action="store_true",
                             help="open the vendor's full downloads list (every plugin's page)")
    p_catalogue.add_argument("--download", metavar="PRODUCT", help="fetch a product whose link is public (hardware/manuals)")
    p_catalogue.add_argument("--directory", default=str(Path.home() / "Downloads"),
                             help="where --download saves (default ~/Downloads)")
    p_catalogue.add_argument("--limit", type=int, default=None)
    p_catalogue.add_argument("--json", action="store_true")
    p_catalogue.set_defaults(func=cmd_catalogue)

    p_presets = sub.add_parser(
        "presets", help="your presets and downloaded packs: what is here, and what has been rescued"
    )
    p_presets.add_argument("--export", help="copy every product's presets and packs out to this directory")
    p_presets.add_argument("--vendor", default="Neural DSP",
                           help="vendor folder under ProgramData (default: Neural DSP)")
    p_presets.add_argument("--sources", action="store_true",
                           help="where to get more presets and IRs: community sites, vaults, shops")
    p_presets.add_argument("--open", metavar="SOURCE",
                           help="open a preset source in your browser (loose name match)")
    p_presets.add_argument("--product", help="with --open: the plugin to search that source for")
    p_presets.set_defaults(func=cmd_presets)

    p_products = sub.add_parser(
        "products", help="triage every product recorded in every Wine prefix: real, files-only, fragment"
    )
    p_products.add_argument("--limit", type=int, default=200)
    p_products.add_argument("--all-products", action="store_true",
                            help="include runtimes and system entries (vcrun, services)")
    p_products.add_argument("--json", action="store_true", help="machine-readable triage report")
    p_products.set_defaults(func=cmd_products)

    p_list = sub.add_parser("list", help="inventory of plugins actually installed in the prefix")
    p_list.add_argument("--limit", type=int, default=200)
    p_list.add_argument("--no-standalone", action="store_true", help="skip standalone apps")
    p_list.add_argument("--all-standalone", action="store_true",
                        help="also list .exe files in Program Files that no plugin MSI describes")
    p_list.add_argument("--json", action="store_true", help="machine-readable output (shareable report)")
    p_list.set_defaults(func=cmd_list)

    p_pending = sub.add_parser("pending", help="installers downloaded but not installed")
    p_pending.add_argument("--dir", action="append", help="extra directory to search (repeatable)")
    p_pending.add_argument("--json", action="store_true")
    p_pending.set_defaults(func=cmd_pending)

    for verb, enable in (("disable", False), ("enable", True)):
        p = sub.add_parser(
            verb,
            help=f"{'hide a plugin from the DAW scanner' if not enable else 'bring a disabled plugin back'}",
        )
        p.add_argument("name", help="substring of the plugin name, e.g. 'Nolly'")
        p.add_argument("--dry-run", action="store_true")
        p.set_defaults(func=cmd_toggle, enable=enable)

    p_repair = sub.add_parser("repair", help="re-place missing or wrong-sized files from a cached MSI")
    p_repair.add_argument("msi", nargs="?", help="MSI path; omit to use --product / the newest found")
    p_repair.add_argument("--product", help="substring used to pick an MSI from the prefix")
    p_repair.add_argument("--scratch", default=DEFAULT_SCRATCH)
    p_repair.add_argument("--dry-run", action="store_true")
    p_repair.add_argument("--no-vst2", action="store_true")
    p_repair.add_argument("--no-standalone", action="store_true")
    p_repair.add_argument("--no-presets", action="store_true")
    p_repair.add_argument("--aax", action="store_true")
    p_repair.set_defaults(func=cmd_repair)

    p_rm = sub.add_parser("uninstall", help="remove a product: msiexec registration + the files its MSI placed")
    p_rm.add_argument("msi", nargs="?", help="MSI path to read the ProductCode and file list from")
    p_rm.add_argument("--product", help="substring used to pick an MSI from the prefix")
    p_rm.add_argument("--product-code", help="uninstall this ProductCode directly, e.g. '{1DACAA64-...}'")
    p_rm.add_argument("--scratch", default=DEFAULT_SCRATCH, help=f"extraction dir ({DEFAULT_SCRATCH})")
    p_rm.add_argument("--dry-run", action="store_true")
    p_rm.add_argument("--no-files", action="store_true",
                      help="only clear the msiexec registration, leave every file in place")
    p_rm.add_argument("--files-only", action="store_true",
                      help="skip msiexec (the usual case: the product was never registered)")
    p_rm.add_argument("--purge", action="store_true",
                      help="also remove registry entries pointing at this product's files (warns first)")
    p_rm.add_argument("--no-rescue", dest="rescue_presets", action="store_false",
                      help="do NOT copy your presets and downloaded packs out first (they will be lost)")
    p_rm.add_argument("--vendor", default="Neural DSP",
                      help="vendor folder under ProgramData to look for presets in (default: Neural DSP)")
    p_rm.set_defaults(func=cmd_uninstall, rescue_presets=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except EnvironmentError_ as exc:
        print(f"environment error: {exc}", file=sys.stderr)
        return 3
    except msi_mod.MsiError as exc:
        print(f"msi error: {exc}", file=sys.stderr)
        return 4
    except catalogue_mod.CatalogueError as exc:
        print(f"catalogue error: {exc}", file=sys.stderr)
        return 5
    except subprocess.TimeoutExpired as exc:
        # a hung external tool must not surface as a traceback
        print(f"timed out: {' '.join(str(part) for part in (exc.cmd or []))} "
              f"did not finish within {exc.timeout}s", file=sys.stderr)
        return 4
    except OSError as exc:
        print(f"system error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())