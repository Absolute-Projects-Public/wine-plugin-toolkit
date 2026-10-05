#!/usr/bin/env python3
"""Uninstall must preserve an exact File-table path claimed by another product MSI."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt import inventory, msi  # noqa: E402
from wpt.environment import Environment  # noqa: E402
from wpt.installer import Action, Plan, leftovers, remove_files  # noqa: E402


def shared_file_is_marked_and_preserved() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-cross-product-owner-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        bundle = env.vst3_dir / "Shared.vst3"
        owned = bundle / "Contents" / "plugin.dll"
        owned.parent.mkdir(parents=True)
        owned.write_bytes(b"shared bytes")
        current_msi, other_msi = base / "current.msi", base / "other.msi"
        current = Plan(msi=current_msi,
                       identity=msi.MsiIdentity(product_code="{CURRENT}", product_name="Current"),
                       actions=[Action(base / "current-payload", bundle, "VST3DIR")],
                       owned_files={owned: msi.MsiFileEntry(
                           "F", "VST3DIR", Path("Shared.vst3/Contents/plugin.dll"),
                           len(b"shared bytes"))})
        case_variant = Path(str(owned).replace("Shared.vst3", "shared.vst3").replace("plugin.dll", "PLUGIN.DLL"))
        other = Plan(msi=other_msi,
                     identity=msi.MsiIdentity(product_code="{OTHER}", product_name="Other"),
                     owned_files={case_variant: msi.MsiFileEntry(
                         "F", "VST3DIR", Path("shared.vst3/Contents/PLUGIN.DLL"),
                         len(b"shared bytes"))},
                     destinations_only=True)
        plans = {current_msi: current, other_msi: other}

        with (patch.object(msi, "find_extracted_msis", return_value=[other_msi]),
              patch.object(inventory, "build_plan_from_tables", side_effect=lambda path, *_a, **_kw: plans[path])):
            foreign_owner = inventory.mark_cross_product_claims(current, env)
        assert foreign_owner == {str(owned).casefold()}, foreign_owner

        current.shared_paths.clear()
        with (patch.object(msi, "find_extracted_msis", return_value=[current_msi, other_msi]),
              patch.object(inventory, "build_plan_from_tables", side_effect=lambda path, *_a, **_kw: plans[path])):
            conflicts = inventory.mark_cross_product_claims(current, env)
            rows = remove_files(current, env)

        assert conflicts == {str(owned).casefold()}, conflicts
        assert any(status == "refused" and str(owned) in path
                   for status, path, _note in rows), rows
        assert owned.read_bytes() == b"shared bytes"
        assert owned in leftovers(current, env)

    print("ok a cross-product shared file is refused and preserved")


def same_product_code_is_not_foreign_ownership() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-same-product-owner-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        owned = env.vst3_dir / "Plugin.vst3" / "plugin.dll"
        owned.parent.mkdir(parents=True)
        owned.write_bytes(b"payload")
        current_msi, cached_msi = base / "current.msi", base / "cached.msi"
        current = Plan(msi=current_msi,
                       identity=msi.MsiIdentity(product_code="{SAME}", product_name="Plugin"),
                       actions=[Action(base / "payload", owned, "VST3DIR")],
                       owned_files={owned: msi.MsiFileEntry("F", "VST3DIR", Path("plugin.dll"), 7)})
        cached = Plan(msi=cached_msi,
                      identity=msi.MsiIdentity(product_code="{SAME}", product_name="Plugin"),
                      owned_files={owned: msi.MsiFileEntry("F", "VST3DIR", Path("plugin.dll"), 7)},
                      destinations_only=True)
        plans = {current_msi: current, cached_msi: cached}

        with (patch.object(msi, "find_extracted_msis", return_value=[current_msi, cached_msi]),
              patch.object(inventory, "build_plan_from_tables", create=True,
                           side_effect=lambda path, *_a, **_kw: plans[path])):
            conflicts = inventory.mark_cross_product_claims(current, env)

        assert not conflicts, conflicts

    print("ok a cached copy of the same product is not treated as foreign")


def unreadable_cached_msi_fails_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-unreadable-owner-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        owned = env.vst3_dir / "Plugin.vst3" / "plugin.dll"
        owned.parent.mkdir(parents=True)
        owned.write_bytes(b"payload")
        current_msi, unreadable_msi = base / "current.msi", base / "unreadable.msi"
        current = Plan(msi=current_msi,
                       identity=msi.MsiIdentity(product_code="{CURRENT}", product_name="Plugin"),
                       actions=[Action(base / "payload", owned, "VST3DIR")],
                       owned_files={owned: msi.MsiFileEntry("F", "VST3DIR", Path("plugin.dll"), 7)})

        with (patch.object(msi, "find_extracted_msis", return_value=[unreadable_msi]),
              patch.object(inventory, "build_plan_from_tables", side_effect=msi.MsiError("bad tables"))):
            conflicts = inventory.mark_cross_product_claims(current, env)
            rows = remove_files(current, env)

        key = str(owned.resolve()).casefold()
        assert conflicts == {key}, conflicts
        assert current.warnings and "failed closed" in current.warnings[-1], current.warnings
        assert any(status == "refused" and str(owned) in path
                   for status, path, _note in rows), rows
        assert owned in leftovers(current, env)

    print("ok unreadable MSI ownership index preserves every planned path")


def non_plugin_app_msi_claims_are_indexed() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-app-owner-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        owned = env.program_files / "Acme Audio" / "Amp X" / "helper.dll"
        owned.parent.mkdir(parents=True)
        owned.write_bytes(b"shared helper")
        current_msi, app_msi = base / "plugin.msi", base / "control-panel.msi"
        current = Plan(
            msi=current_msi,
            identity=msi.MsiIdentity(product_code="{PLUGIN}", product_name="Amp X",
                                     manufacturer="Acme Audio"),
            actions=[Action(base / "plugin-payload", owned.parent, "APPDIR")],
            owned_files={owned: msi.MsiFileEntry("F", "APPDIR", Path("helper.dll"), 13)},
        )
        app_owner = Plan(
            msi=app_msi,
            identity=msi.MsiIdentity(product_code="{APP}", product_name="Control Panel",
                                     manufacturer="Acme Audio"),
            actions=[Action(base / "app-payload", owned.parent, "APPDIR")],
            owned_files={owned: msi.MsiFileEntry("F", "APPDIR", Path("helper.dll"), 13)},
            destinations_only=True,
        )
        empty_app_plan = Plan(msi=app_msi, identity=app_owner.identity, destinations_only=True)

        def build_plan(path, *_args, **kwargs):
            if path == app_msi:
                # Before the fix, ownership indexing built application MSIs with the install-time
                # default include_app_files=False and therefore saw no app-owned File rows.
                return app_owner if kwargs.get("include_app_files") else empty_app_plan
            return current

        with (patch.object(msi, "find_extracted_msis", return_value=[app_msi]),
              patch.object(inventory, "build_plan_from_tables", side_effect=build_plan)):
            conflicts = inventory.mark_cross_product_claims(current, env)
            removal = remove_files(current, env)

        assert conflicts == {inventory.ownership_key(owned)}, conflicts
        assert any(status == "refused" and path == str(owned)
                   for status, path, _note in removal), removal
        assert owned.read_bytes() == b"shared helper"
    print("ok a non-plugin app MSI's shared file is indexed and preserved")


def non_plugin_installer_cache_msi_is_discovered() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-non-plugin-cache-") as tmp:
        prefix = Path(tmp) / "prefix"
        installer_cache = prefix / "drive_c" / "windows" / "Installer"
        installer_cache.mkdir(parents=True)
        runtime_msi = installer_cache / "runtime.msi"
        runtime_msi.write_bytes(b"synthetic MSI")
        with patch.object(msi, "declares_plugin_payload", return_value=False):
            normal_inventory = msi.find_extracted_msis(prefix, include_installer_cache=True)
            ownership_scan = msi.find_extracted_msis(
                prefix, include_installer_cache=True, include_non_plugin_installer_cache=True
            )
        assert runtime_msi not in normal_inventory, normal_inventory
        assert runtime_msi in ownership_scan, ownership_scan
    print("ok non-plugin MSI cache entries remain visible to ownership checks")


def skipped_app_roots_are_still_checked_before_registered_uninstall() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-skipped-app-owner-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        plugin_path = env.vst3_dir / "Amp X.vst3" / "plugin.dll"
        app_path = env.program_files / "Acme Audio" / "Amp X" / "helper.dll"
        plugin_path.parent.mkdir(parents=True)
        app_path.parent.mkdir(parents=True)
        plugin_path.write_bytes(b"plugin")
        app_path.write_bytes(b"shared helper")
        current_msi, other_msi = base / "plugin.msi", base / "other-app.msi"
        current = Plan(
            msi=current_msi,
            identity=msi.MsiIdentity(product_code="{PLUGIN}", product_name="Amp X"),
            actions=[Action(base / "plugin-payload", plugin_path.parent, "VST3DIR")],
            owned_files={plugin_path: msi.MsiFileEntry("FPlugin", "VST3DIR",
                                                       Path("Amp X.vst3/plugin.dll"), 6)},
            skipped_app=[("APPDIR", app_path.parent)],
        )
        current_with_app = Plan(
            msi=current_msi,
            identity=current.identity,
            owned_files={
                **current.owned_files,
                app_path: msi.MsiFileEntry("FHelper", "APPDIR", Path("helper.dll"), 13),
            },
            destinations_only=True,
        )
        other_owner = Plan(
            msi=other_msi,
            identity=msi.MsiIdentity(product_code="{OTHER}", product_name="Control Panel"),
            owned_files={app_path: msi.MsiFileEntry("FHelper", "APPDIR", Path("helper.dll"), 13)},
            destinations_only=True,
        )
        plans = {current_msi: current_with_app, other_msi: other_owner}

        with (patch.object(msi, "find_extracted_msis", return_value=[current_msi, other_msi]),
              patch.object(inventory, "build_plan_from_tables",
                           side_effect=lambda path, *_a, **_kw: plans[path])):
            conflicts = inventory.mark_cross_product_claims(current, env)

        assert inventory.ownership_key(app_path) in conflicts, conflicts
        assert inventory.ownership_key(app_path) in current.shared_paths, current.shared_paths
    print("ok registered uninstall ownership includes its skipped application roots")


if __name__ == "__main__":
    shared_file_is_marked_and_preserved()
    same_product_code_is_not_foreign_ownership()
    unreadable_cached_msi_fails_closed()
    non_plugin_app_msi_claims_are_indexed()
    non_plugin_installer_cache_msi_is_discovered()
    skipped_app_roots_are_still_checked_before_registered_uninstall()
    print("cross-product ownership check: 6/6 passed")
