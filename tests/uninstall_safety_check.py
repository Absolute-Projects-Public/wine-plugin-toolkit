#!/usr/bin/env python3
"""A simulated vendor uninstall must not get the first chance to delete user presets.

No Wine is run. Every file is under TemporaryDirectory; the fake msiexec deletes the original
preset to model a vendor that removes it. The toolkit must rescue a copy BEFORE that callback.
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt import cli, msi, presets  # noqa: E402
from wpt.environment import Environment  # noqa: E402
from wpt.installer import Action, Plan  # noqa: E402


def rescue_precedes_vendor_removal() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-uninstall-order-") as tmp:
        base = Path(tmp)
        home = base / "home"
        prefix = home / ".wine-ableton"
        env = Environment(home=home, prefix=prefix, user="tester",
                          wine_tree=home / ".local/opt/wine-d2d1-nspa-test")
        folder = env.program_data / "Neural DSP" / "Product" / "User"
        folder.mkdir(parents=True)
        preset = folder / "My Sound.xml"
        preset.write_text("my work")
        fake_msi = base / "Product.msi"
        fake_msi.write_bytes(b"fixture")
        plan = Plan(msi=fake_msi, identity=msi.MsiIdentity(product_name="Product",
                    manufacturer="Neural DSP"), actions=[Action(folder, folder, "PREDIR")])
        args = SimpleNamespace(scratch=str(base / "scratch"), product_code="{fixture}",
                               no_files=False, files_only=False, rescue_presets=True,
                               purge=False, dry_run=False, vendor="Neural DSP", product=None)
        events = []
        actual_rescue = presets.rescue_for_plan

        def rescue(env_arg, plan_arg, *, vendor, dry_run):
            events.append("rescue")
            return actual_rescue(env_arg, plan_arg, vendor=vendor,
                                 rescue_root=base / "rescue", dry_run=dry_run, stamp="fixture")

        def msiexec(*_a, **_kw):
            events.append("msiexec")
            if preset.exists():
                preset.unlink()  # model a vendor deleting it before the toolkit runs
            return 0, "vendor removed the original"

        output = io.StringIO()
        with (patch.object(cli, "_env", return_value=env),
              patch.object(cli, "_pick_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "stage_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "extract", return_value=None),
              patch.object(cli, "build_plan", return_value=plan),
              patch.object(cli.scan_mod, "is_registered", side_effect=[True, False]),
              patch.object(cli, "uninstall_product", side_effect=msiexec),
              patch.object(cli.presets_mod, "rescue_for_plan", side_effect=rescue),
              patch.object(cli.installer_mod, "remove_files", return_value=[]),
              patch.object(cli.installer_mod, "leftovers", return_value=[]),
              contextlib.redirect_stdout(output)):
            result = cli.cmd_uninstall(args)
        saved = list((base / "rescue").rglob("My Sound.xml"))
        assert events.index("rescue") < events.index("msiexec"), events
        assert len(saved) == 1 and saved[0].read_text() == "my work", (events, saved, output.getvalue())
        assert result == 0, (result, output.getvalue())
    print("ok user preset rescued before simulated vendor removal")


def failed_rescue_refuses_vendor_removal() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-uninstall-failure-") as tmp:
        base = Path(tmp)
        env = Environment(home=base, prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        fake_msi = base / "Product.msi"; fake_msi.write_bytes(b"fixture")
        dest = env.program_data / "Neural DSP" / "Product" / "User"
        plan = Plan(msi=fake_msi, identity=msi.MsiIdentity(product_name="Product"),
                    actions=[Action(dest, dest, "PREDIR")])
        args = SimpleNamespace(scratch=str(base / "scratch"), product_code="{fixture}",
                               no_files=False, files_only=False, rescue_presets=True,
                               purge=False, dry_run=False, vendor="Neural DSP", product=None)
        events = []

        def msiexec(*_a, **_kw):
            events.append("msiexec")
            return 0, ""

        with (patch.object(cli, "_env", return_value=env),
              patch.object(cli, "_pick_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "stage_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "extract", return_value=None),
              patch.object(cli, "build_plan", return_value=plan),
              patch.object(cli.scan_mod, "is_registered", return_value=True),
              patch.object(cli, "uninstall_product", side_effect=msiexec),
              patch.object(cli.presets_mod, "rescue_for_plan",
                           return_value=([("failed", str(dest / "custom.xml"), "disk full")], [], ["Product"])),
              contextlib.redirect_stdout(io.StringIO())):
            result = cli.cmd_uninstall(args)
        assert result != 0 and events == [], (result, events)
    print("ok failed rescue aborts before vendor removal")


def registration_only_warns_that_rescue_is_skipped() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-registration-only-") as tmp:
        base = Path(tmp)
        env = Environment(home=base, prefix=base / "prefix", user="tester", wine_tree=base / "tree")
        args = SimpleNamespace(scratch=str(base / "scratch"), product_code="{fixture}",
                               no_files=True, files_only=False, rescue_presets=True,
                               purge=False, dry_run=True, vendor="Neural DSP", product=None)
        output = io.StringIO()
        with (patch.object(cli, "_env", return_value=env),
              patch.object(cli, "_pick_msi", return_value=None),
              patch.object(cli.scan_mod, "is_registered", return_value=True),
              patch.object(cli, "uninstall_product", return_value=(0, "would run msiexec")),
              contextlib.redirect_stdout(output)):
            result = cli.cmd_uninstall(args)
        assert result == 0
        assert "--no-files skips preset rescue" in output.getvalue(), output.getvalue()
        assert "shared files" in output.getvalue().lower(), output.getvalue()
    print("ok registration-only path warns that Wine may remove files without rescue")


def refused_removal_is_not_a_success() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-removal-refused-") as tmp:
        base = Path(tmp)
        env = Environment(home=base, prefix=base / "prefix", user="tester", wine_tree=base / "tree")
        fake_msi = base / "Product.msi"; fake_msi.write_bytes(b"fixture")
        dest = env.vst3_dir / "Missing.vst3"
        plan = Plan(msi=fake_msi, identity=msi.MsiIdentity(product_name="Product"),
                    actions=[Action(dest, dest, "VST3DIR")])
        args = SimpleNamespace(scratch=str(base / "scratch"), product_code="{fixture}",
                               no_files=False, files_only=True, rescue_presets=False,
                               purge=False, dry_run=False, vendor="Neural DSP", product=None)
        with (patch.object(cli, "_env", return_value=env),
              patch.object(cli, "_pick_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "stage_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "extract", return_value=None),
              patch.object(cli, "build_plan", return_value=plan),
              patch.object(cli.scan_mod, "is_registered", return_value=False),
              contextlib.redirect_stdout(io.StringIO())):
            result = cli.cmd_uninstall(args)
        assert result != 0, "refused removal returned success merely because its target is absent"
    print("ok a refused File-table deletion is an uninstall failure")


def unregistered_product_skips_msiexec() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-unregistered-skip-") as tmp:
        base = Path(tmp)
        env = Environment(home=base, prefix=base / "prefix", user="tester", wine_tree=base / "tree")
        fake_msi = base / "Product.msi"; fake_msi.write_bytes(b"fixture")
        plan = Plan(msi=fake_msi, identity=msi.MsiIdentity(product_name="Product"))
        args = SimpleNamespace(scratch=str(base / "scratch"), product_code="{fixture}",
                               no_files=False, files_only=False, rescue_presets=False,
                               purge=False, dry_run=False, vendor="Neural DSP", product=None)
        output = io.StringIO()
        with (patch.object(cli, "_env", return_value=env),
              patch.object(cli, "_pick_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "stage_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "extract", return_value=None),
              patch.object(cli, "build_plan", return_value=plan),
              patch.object(cli.scan_mod, "is_registered", return_value=False),
              patch.object(cli.inventory_mod, "mark_cross_product_claims", return_value=set()),
              patch.object(cli, "uninstall_product") as vendor,
              patch.object(cli.installer_mod, "remove_files", return_value=[]),
              patch.object(cli.installer_mod, "leftovers", return_value=[]),
              contextlib.redirect_stdout(output)):
            result = cli.cmd_uninstall(args)
        assert result == 0
        assert not vendor.called, "unregistered product reached the external MSI engine"
        assert "msiexec skipped" in output.getvalue(), output.getvalue()
    print("ok an unregistered product skips the external MSI engine")


def failed_vendor_uninstall_skips_registry_purge(
        code: int = 1603, detail: str = "vendor uninstall failed",
        expected_status: str = "msiexec exited 1603") -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-purge-msiexec-fail-") as tmp:
        base = Path(tmp)
        env = Environment(home=base, prefix=base / "prefix", user="tester", wine_tree=base / "tree")
        fake_msi = base / "Product.msi"; fake_msi.write_bytes(b"fixture")
        plan = Plan(msi=fake_msi, identity=msi.MsiIdentity(product_name="Product",
                    product_code="{fixture}"))
        args = SimpleNamespace(scratch=str(base / "scratch"), product_code="{fixture}",
                               no_files=False, files_only=False, rescue_presets=False,
                               purge=True, dry_run=False, vendor="Neural DSP", product=None)
        edit = SimpleNamespace(hive="HKCU", key="Software\\Acme\\Product", value="", reason="stale")
        output, error = io.StringIO(), io.StringIO()
        with (patch.object(cli, "_env", return_value=env),
              patch.object(cli, "_pick_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "stage_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "extract", return_value=None),
              patch.object(cli, "build_plan", return_value=plan),
              patch.object(cli.scan_mod, "is_registered", return_value=True),
              patch.object(cli.inventory_mod, "mark_cross_product_claims", return_value=set()),
              patch.object(cli, "uninstall_product", return_value=(code, detail)),
              patch.object(cli.presets_mod, "rescue_for_plan", return_value=([], [], [])),
              patch.object(cli.installer_mod, "remove_files", return_value=[]),
              patch.object(cli.installer_mod, "leftovers", return_value=[]),
              patch.object(cli.installer_mod, "stale_registry_edits", return_value=[edit]) as stale,
              patch.object(cli.installer_mod, "purge_registry", return_value=[("removed", "key", "")]) as purge,
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(error)):
            result = cli.cmd_uninstall(args)
        assert result != 0
        assert not stale.called and not purge.called, (stale.call_args_list, purge.call_args_list)
        assert expected_status in error.getvalue(), error.getvalue()
        if expected_status == "msiexec timed out (WPT code 124)":
            assert "msiexec exited 124" not in error.getvalue(), error.getvalue()
    print("ok failed vendor uninstall skips direct removal and registry purge")


def successful_msiexec_must_clear_registration_before_followup() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-registration-persists-") as tmp:
        base = Path(tmp)
        env = Environment(home=base, prefix=base / "prefix", user="tester", wine_tree=base / "tree")
        fake_msi = base / "Product.msi"; fake_msi.write_bytes(b"fixture")
        dest = env.vst3_dir / "Product.vst3"
        plan = Plan(msi=fake_msi, identity=msi.MsiIdentity(product_name="Product"),
                    actions=[Action(dest, dest, "VST3DIR")])
        args = SimpleNamespace(scratch=str(base / "scratch"), product_code="{fixture}",
                               no_files=False, files_only=False, rescue_presets=True,
                               purge=True, dry_run=False, vendor="Neural DSP", product=None)
        events: list[str] = []
        error = io.StringIO()
        with (patch.object(cli, "_env", return_value=env),
              patch.object(cli, "_pick_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "stage_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "extract", return_value=None),
              patch.object(cli, "build_plan", return_value=plan),
              patch.object(cli.scan_mod, "is_registered", side_effect=[True, True]),
              patch.object(cli.inventory_mod, "mark_cross_product_claims", return_value=set()),
              patch.object(cli.presets_mod, "rescue_for_plan",
                           side_effect=lambda *_a, **_k: events.append("rescue") or ([], [], [])),
              patch.object(cli, "uninstall_product",
                           side_effect=lambda *_a, **_k: events.append("msiexec") or (0, "")),
              patch.object(cli.installer_mod, "remove_files",
                           side_effect=lambda *_a, **_k: events.append("remove") or []),
              patch.object(cli.installer_mod, "leftovers", return_value=[]),
              patch.object(cli.installer_mod, "stale_registry_edits", return_value=[]) as stale,
              patch.object(cli.installer_mod, "purge_registry",
                           side_effect=lambda *_a, **_k: events.append("purge") or []) as purge,
              contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(error)):
            result = cli.cmd_uninstall(args)
        assert result != 0 and events == ["rescue", "msiexec"], (result, events, error.getvalue())
        assert not stale.called and not purge.called
        assert "still registered" in error.getvalue().lower(), error.getvalue()
    print("ok a zero msiexec exit cannot trigger direct removal while registration remains")


def unmapped_msi_root_refuses_registered_vendor_uninstall() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-unmapped-root-") as tmp:
        base = Path(tmp)
        env = Environment(home=base, prefix=base / "prefix", user="tester", wine_tree=base / "tree")
        fake_msi = base / "Product.msi"; fake_msi.write_bytes(b"fixture")
        plan = Plan(msi=fake_msi, identity=msi.MsiIdentity(product_name="Product"),
                    warnings=["no destination known for VendorData - skipped"])
        args = SimpleNamespace(scratch=str(base / "scratch"), product_code="{fixture}",
                               no_files=False, files_only=False, rescue_presets=True,
                               purge=True, dry_run=False, vendor="Neural DSP", product=None)
        events = []
        error = io.StringIO()
        with (patch.object(cli, "_env", return_value=env),
              patch.object(cli, "_pick_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "stage_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "extract", return_value=None),
              patch.object(cli, "build_plan", return_value=plan),
              patch.object(cli.scan_mod, "is_registered", return_value=True),
              patch.object(cli.inventory_mod, "mark_cross_product_claims",
                           side_effect=lambda *_: events.append("ownership") or set()),
              patch.object(cli.presets_mod, "rescue_for_plan",
                           side_effect=lambda *_a, **_k: events.append("rescue") or ([], [], [])),
              patch.object(cli, "uninstall_product",
                           side_effect=lambda *_a, **_k: events.append("msiexec") or (0, "")),
              patch.object(cli.installer_mod, "remove_files",
                           side_effect=lambda *_a, **_k: events.append("remove") or []),
              patch.object(cli.installer_mod, "stale_registry_edits",
                           side_effect=lambda *_: events.append("stale") or []),
              patch.object(cli.installer_mod, "purge_registry",
                           side_effect=lambda *_: events.append("purge") or []),
              contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(error)):
            result = cli.cmd_uninstall(args)
        assert result != 0 and not events, (result, events, error.getvalue())
        assert "unmapped" in error.getvalue().lower(), error.getvalue()
    print("ok an unmapped MSI root blocks registered msiexec and follow-on work")


def invalid_manifest_aborts_before_vendor_removal() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-invalid-manifest-") as tmp:
        base = Path(tmp)
        env = Environment(home=base, prefix=base / "prefix", user="tester", wine_tree=base / "tree")
        fake_msi = base / "Product.msi"; fake_msi.write_bytes(b"fixture")
        args = SimpleNamespace(scratch=str(base / "scratch"), product_code="{fixture}",
                               no_files=False, files_only=False, rescue_presets=True,
                               purge=False, dry_run=False, vendor="Neural DSP", product=None)
        with (patch.object(cli, "_env", return_value=env),
              patch.object(cli, "_pick_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "stage_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "extract", return_value=None),
              patch.object(cli, "build_plan", side_effect=msi.MsiError("ambiguous File rows")),
              patch.object(cli, "uninstall_product") as vendor,
              contextlib.redirect_stdout(io.StringIO())):
            result = cli.cmd_uninstall(args)
        assert result != 0 and not vendor.called, "untrusted plan reached msiexec"
    print("ok invalid manifest aborts before vendor uninstall")


def table_only_plan_preserves_data_before_registered_uninstall() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-table-only-cli-") as tmp:
        base = Path(tmp)
        env = Environment(home=base, prefix=base / "prefix", user="tester", wine_tree=base / "tree")
        fake_msi = base / "Product.msi"
        fake_msi.write_bytes(b"fixture")
        dest = env.vst3_dir / "Product.vst3"
        plan = Plan(msi=fake_msi, identity=msi.MsiIdentity(product_name="Product"),
                    actions=[Action(base / "nominal", dest, "VST3DIR")], destinations_only=True)
        args = SimpleNamespace(scratch=str(base / "scratch"), product_code="{fixture}",
                               no_files=False, files_only=False, rescue_presets=True,
                               purge=False, dry_run=False, vendor="Neural DSP", product=None)
        output, error = io.StringIO(), io.StringIO()
        with (patch.object(cli, "_env", return_value=env),
              patch.object(cli, "_pick_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "stage_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "extract", side_effect=msi.MsiError("missing cabinet")),
              patch.object(cli, "build_plan_from_tables", return_value=plan),
              patch.object(cli.scan_mod, "is_registered", return_value=True),
              patch.object(cli, "uninstall_product", return_value=(0, "")) as vendor,
              patch.object(cli.presets_mod, "rescue_for_plan", return_value=([], [], [])) as rescue,
              patch.object(cli.installer_mod, "remove_files", return_value=[]) as remove,
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(error)):
            result = cli.cmd_uninstall(args)
        assert result != 0, (result, output.getvalue(), error.getvalue())
        assert not vendor.called and not rescue.called and not remove.called, (
            vendor.call_args_list, rescue.call_args_list, remove.call_args_list)
        assert "payload is unavailable" in error.getvalue(), error.getvalue()
    print("ok table-only plan aborts before msiexec, rescue, or direct removal")


def shared_product_path_aborts_before_registered_uninstall() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-cross-product-cli-") as tmp:
        base = Path(tmp)
        env = Environment(home=base, prefix=base / "prefix", user="tester", wine_tree=base / "tree")
        fake_msi = base / "Product.msi"
        fake_msi.write_bytes(b"fixture")
        bundle = env.vst3_dir / "Shared.vst3"
        owned = bundle / "Contents" / "plugin.dll"
        plan = Plan(msi=fake_msi,
                    identity=msi.MsiIdentity(product_code="{CURRENT}", product_name="Product"),
                    actions=[Action(base / "payload", bundle, "VST3DIR")],
                    owned_files={owned: msi.MsiFileEntry(
                        "F", "VST3DIR", Path("Shared.vst3/Contents/plugin.dll"), 11)})
        args = SimpleNamespace(scratch=str(base / "scratch"), product_code="{CURRENT}",
                               no_files=False, files_only=False, rescue_presets=True,
                               purge=True, dry_run=False, vendor="Neural DSP", product=None)
        output, error = io.StringIO(), io.StringIO()

        def mark_shared(plan_arg, _env):
            plan_arg.shared_paths.add(str(owned.resolve()).casefold())
            return set(plan_arg.shared_paths)

        with (patch.object(cli, "_env", return_value=env),
              patch.object(cli, "_pick_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "stage_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "extract", return_value=None),
              patch.object(cli, "build_plan", return_value=plan),
              patch.object(cli.inventory_mod, "mark_cross_product_claims", side_effect=mark_shared),
              patch.object(cli.scan_mod, "is_registered", return_value=True),
              patch.object(cli, "uninstall_product", return_value=(0, "")) as vendor,
              patch.object(cli.presets_mod, "rescue_for_plan", return_value=([], [], [])) as rescue,
              patch.object(cli.installer_mod, "remove_files", return_value=[]) as remove,
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(error)):
            result = cli.cmd_uninstall(args)

        assert result != 0, (result, output.getvalue(), error.getvalue())
        assert not vendor.called and not rescue.called and not remove.called
        assert "claimed by another cached MSI" in error.getvalue(), error.getvalue()
    print("ok shared-product path aborts before vendor uninstall or purge")


def symlinked_destination_aborts_before_registered_uninstall() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-symlink-uninstall-cli-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        relative = Path("Contents/x86_64-win/Plugin.vst3")
        backup = env.drive_c / "users" / "tester" / "Documents" / "MyBackup" / "Plugin.vst3"
        backup_file = backup / relative
        backup_file.parent.mkdir(parents=True)
        backup_file.write_bytes(b"user backup")
        source = base / "payload" / "Plugin.vst3"
        (source / relative).parent.mkdir(parents=True)
        (source / relative).write_bytes(b"user backup")
        destination = env.vst3_dir / "Plugin.vst3"
        destination.parent.mkdir(parents=True)
        destination.symlink_to(backup, target_is_directory=True)
        fake_msi = base / "Product.msi"
        fake_msi.write_bytes(b"fixture")
        plan = Plan(msi=fake_msi,
                    identity=msi.MsiIdentity(product_code="{fixture}", product_name="Plugin"),
                    actions=[Action(source, destination, "VST3DIR")],
                    owned_files={destination / relative: msi.MsiFileEntry(
                        "F", "VST3DIR", Path("Plugin.vst3") / relative, 11)})
        args = SimpleNamespace(scratch=str(base / "scratch"), product_code="{fixture}",
                               no_files=False, files_only=False, rescue_presets=True,
                               purge=False, dry_run=False, vendor="Vendor", product=None)
        output, error = io.StringIO(), io.StringIO()

        with (patch.object(cli, "_env", return_value=env),
              patch.object(cli, "_pick_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "stage_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "extract", return_value=None),
              patch.object(cli, "build_plan", return_value=plan),
              patch.object(cli.scan_mod, "is_registered", return_value=True),
              patch.object(cli.inventory_mod, "mark_cross_product_claims", return_value=set()),
              patch.object(cli, "uninstall_product", return_value=(0, "")) as vendor,
              patch.object(cli.presets_mod, "rescue_for_plan", return_value=([], [], [])) as rescue,
              patch.object(cli.installer_mod, "remove_files") as remove,
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(error)):
            result = cli.cmd_uninstall(args)

        assert result != 0, (result, output.getvalue(), error.getvalue())
        assert not vendor.called and not rescue.called and not remove.called
        assert backup_file.read_bytes() == b"user backup"
        assert destination.is_symlink()
        assert "symlink" in error.getvalue().lower(), error.getvalue()
    print("ok symlinked uninstall destination aborts before rescue, msiexec, or deletion")


if __name__ == "__main__":
    rescue_precedes_vendor_removal()
    failed_rescue_refuses_vendor_removal()
    registration_only_warns_that_rescue_is_skipped()
    refused_removal_is_not_a_success()
    unregistered_product_skips_msiexec()
    failed_vendor_uninstall_skips_registry_purge()
    failed_vendor_uninstall_skips_registry_purge(
        124, f"{cli.installer_mod.UNINSTALL_TIMEOUT_DETAIL_PREFIX} wine msiexec /x exceeded 600s; "
             "uninstall state is unknown", "msiexec timed out (WPT code 124)")
    failed_vendor_uninstall_skips_registry_purge(
        124, "vendor uninstall returned status 124", "msiexec exited 124")
    successful_msiexec_must_clear_registration_before_followup()
    unmapped_msi_root_refuses_registered_vendor_uninstall()
    invalid_manifest_aborts_before_vendor_removal()
    table_only_plan_preserves_data_before_registered_uninstall()
    shared_product_path_aborts_before_registered_uninstall()
    symlinked_destination_aborts_before_registered_uninstall()
    print("uninstall safety check: 14/14 passed")
