#!/usr/bin/env python3
"""Malformed MSI paths and symlinked ancestors must never delete user data.

All paths are disposable. This is a direct last-line-of-defence test for remove_files: even if a
planner produces a root destination, the removal must refuse it rather than recursively delete it.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt import msi as msi_mod  # noqa: E402
from wpt.environment import Environment  # noqa: E402
from wpt.installer import (Action, Plan, build_plan_from_tables, remove_files,
                           unmapped_destination_warnings)  # noqa: E402
from wpt.msi import MsiFileEntry, MsiIdentity  # noqa: E402


def shared_vst_root_is_never_a_removal_target() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-root-guard-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        other = env.vst3_dir / "Other.vst3" / "plugin.dll"
        wanted = env.vst3_dir / "Wanted.vst3" / "plugin.dll"
        for path in (other, wanted):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"valuable")
        plan = Plan(msi=base / "untrusted.msi", identity=MsiIdentity(product_name="Wanted"),
                    actions=[Action(source=base / "payload", dest=env.vst3_dir, label="VST3DIR")])
        rows = remove_files(plan, env)
        assert any(row[0] == "refused" for row in rows), rows
        assert other.read_bytes() == b"valuable", rows
        assert wanted.read_bytes() == b"valuable", rows
        assert env.vst3_dir.is_dir(), rows
    print("ok a shared VST root is refused and both products survive")


def self_referential_directory_refuses_plan() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-root-plan-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        with (patch.object(msi_mod, "payload_directories", return_value={"VST3DIR": [(".", True)]}),
              patch.object(msi_mod, "identity", return_value=MsiIdentity(product_name="Wanted")),
              patch.object(msi_mod, "expected_sizes", return_value={}),
              patch.object(msi_mod, "declares_plugin_payload", return_value=True)):
            try:
                plan = build_plan_from_tables(base / "untrusted.msi", env)
            except msi_mod.MsiError as exc:
                assert "shared root" in str(exc), str(exc)
            else:
                raise AssertionError(f"planner accepted shared root: {[str(a.dest) for a in plan.actions]}")
    print("ok a nested '.' entry cannot become a shared-root action")


def target_directory_ignores_source_half() -> None:
    tables = {
        "Directory": [
            ["TARGETDIR", "", ""],
            ["PREDIR", "TARGETDIR", "PREDIR"],
            ["FactoryDir", "PREDIR", "FACT~1|Factory:SrcFactory"],
        ],
        "Component": [["C1", "{guid}", "FactoryDir"]],
        "File": [["Tone", "C1", "Tone.xml", "7"]],
    }
    with patch.object(msi_mod, "export_table", side_effect=lambda _msi, table: tables[table]):
        tree = msi_mod.payload_directories(Path("fixture.msi"))
    assert tree["PREDIR"] == [("Factory", True)], tree
    print("ok Directory target:source uses only the target name")


def fallback_destinations_reject_traversing_msi_identity() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-identity-path-guard-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        cases = (
            ("PREDIR", MsiIdentity(product_name="System32", manufacturer="../Windows"),
             [MsiFileEntry("Tone", "PREDIR", Path("User/Tone.xml"), 1)]),
            ("APPDIR", MsiIdentity(product_name="System32", manufacturer="../Windows"),
             [MsiFileEntry("Helper", "APPDIR", Path("helper.exe"), 1)]),
            ("PREDIR", MsiIdentity(product_name="../../Windows/System32", manufacturer="Acme"),
             [MsiFileEntry("Tone", "PREDIR", Path("User/Tone.xml"), 1)]),
            ("APPDIR", MsiIdentity(product_name="../../Windows/System32", manufacturer="Acme"),
             [MsiFileEntry("Helper", "APPDIR", Path("helper.exe"), 1)]),
        )
        for root_name, identity, manifest in cases:
            tree = {root_name: [("User", True)] if root_name == "PREDIR" else [("helper.exe", False)]}
            with (patch.object(msi_mod, "payload_directories", return_value=tree),
                  patch.object(msi_mod, "identity", return_value=identity),
                  patch.object(msi_mod, "expected_sizes", return_value={}),
                  patch.object(msi_mod, "declares_plugin_payload", return_value=True),
                  patch.object(msi_mod, "file_manifest", return_value=manifest)):
                try:
                    build_plan_from_tables(base / "untrusted.msi", env)
                except msi_mod.MsiError as exc:
                    assert "unsafe" in str(exc).lower(), str(exc)
                else:
                    raise AssertionError(f"accepted unsafe {root_name} identity: {identity}")
    print("ok fallback ProgramData/Program Files paths reject MSI traversal labels")


def direct_targetdir_file_rows_are_not_silently_dropped() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-targetdir-file-guard-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        tree = {"VST3DIR": [("Plugin.vst3", True)]}
        manifest = [
            MsiFileEntry("Plugin", "VST3DIR", Path("Plugin.vst3/plugin.dll"), 1),
            MsiFileEntry("Loose", "TARGETDIR", Path("loose.dat"), 1),
        ]
        with (patch.object(msi_mod, "payload_directories", return_value=tree),
              patch.object(msi_mod, "identity", return_value=MsiIdentity(
                  product_name="Product", manufacturer="Acme")),
              patch.object(msi_mod, "expected_sizes", return_value={}),
              patch.object(msi_mod, "declares_plugin_payload", return_value=True),
              patch.object(msi_mod, "file_manifest", return_value=manifest)):
            plan = build_plan_from_tables(base / "untrusted.msi", env, include_aax=True)
        unknown = unmapped_destination_warnings(plan)
        assert any("TARGETDIR" in warning for warning in unknown), (plan.warnings, unknown)
    print("ok direct TARGETDIR File rows are reported as unmapped")


def vendor_folder_payload_root_does_not_duplicate_vendor() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-programfiles-vendor-root-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        tree = {"PROGRAMFILES64FOLDER": [("Acme Audio", True)]}
        manifest = [MsiFileEntry("App", "PROGRAMFILES64FOLDER",
                                 Path("Acme Audio/Product/app.exe"), 1)]
        with (patch.object(msi_mod, "payload_directories", return_value=tree),
              patch.object(msi_mod, "identity", return_value=MsiIdentity(
                  product_name="Product", manufacturer="Acme Audio")),
              patch.object(msi_mod, "expected_sizes", return_value={}),
              patch.object(msi_mod, "declares_plugin_payload", return_value=True),
              patch.object(msi_mod, "file_manifest", return_value=manifest)):
            plan = build_plan_from_tables(base / "fixture.msi", env, include_aax=True)
        assert len(plan.actions) == 1 and plan.actions[0].dest == env.program_files / "Acme Audio", plan.actions
    print("ok a vendor-folder payload maps to Program Files without duplicating the vendor")


def uninstall_refuses_symlinked_bundle_ancestor() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-remove-symlink-ancestor-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        bundle = env.vst3_dir / "Plugin.vst3"
        backup = env.drive_c / "users" / "tester" / "Documents" / "MyBackup" / "Plugin.vst3"
        relative = Path("Contents/x86_64-win/Plugin.vst3")
        data = b"the user's backup copy"
        backup_file = backup / relative
        backup_file.parent.mkdir(parents=True)
        backup_file.write_bytes(data)
        source = base / "payload" / "Plugin.vst3"
        (source / relative).parent.mkdir(parents=True)
        (source / relative).write_bytes(data)
        bundle.parent.mkdir(parents=True)
        bundle.symlink_to(backup, target_is_directory=True)
        owned_path = bundle / relative
        plan = Plan(
            msi=base / "fixture.msi",
            identity=MsiIdentity(product_name="Plugin", manufacturer="Vendor"),
            actions=[Action(source, bundle, "VST3DIR")],
            owned_files={owned_path: MsiFileEntry(
                "F", "VST3DIR", Path("Plugin.vst3") / relative, len(data))},
        )

        rows = remove_files(plan, env)

        assert backup_file.read_bytes() == data, rows
        assert bundle.is_symlink(), rows
        assert any(status == "refused" for status, _path, _note in rows), rows
    print("ok uninstall refuses a symlinked bundle ancestor and preserves the backup")


if __name__ == "__main__":
    shared_vst_root_is_never_a_removal_target()
    self_referential_directory_refuses_plan()
    target_directory_ignores_source_half()
    fallback_destinations_reject_traversing_msi_identity()
    direct_targetdir_file_rows_are_not_silently_dropped()
    vendor_folder_payload_root_does_not_duplicate_vendor()
    uninstall_refuses_symlinked_bundle_ancestor()
    print("plan root safety check: 7/7 passed")
