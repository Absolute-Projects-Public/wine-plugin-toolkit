#!/usr/bin/env python3
"""The active launch profile changes WPT's stack without leaving stale inventory or switching live prefixes."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.environ.get("WPT_TREE", str(Path(__file__).resolve().parents[1])))

root_tmp = tempfile.TemporaryDirectory(prefix="wpt-gui-profiles-")
root = Path(root_tmp.name)
home = root / "home"
config_home = home / ".config"
config_home.mkdir(parents=True)
os.environ["HOME"] = str(home)
os.environ["XDG_CONFIG_HOME"] = str(config_home)
os.environ["XDG_CACHE_HOME"] = str(home / ".cache")
os.environ["WPT_NO_UPDATE_CHECK"] = "1"
os.environ["WINEPREFIX"] = ""

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402

from wpt.launch_profiles import LaunchProfile, ProfileStore, load_store, profile_config_path, save_store  # noqa: E402
from wpt.launch_profile_ui import LaunchProfilesDialog  # noqa: E402
from wpt.inventory import Inventory, PluginEntry  # noqa: E402
from wpt.standalone import StandaloneLaunch  # noqa: E402
import wpt.gui as gui_mod  # noqa: E402
from wpt.gui import MainWindow  # noqa: E402

checks = 0
failures = 0


def check(name, actual, expected):
    global checks, failures
    checks += 1
    if actual == expected:
        print(f"  ok  {name}")
    else:
        failures += 1
        print(f"FAIL {name}: got {actual!r}, expected {expected!r}")


def launch_environment(env):
    try:
        return env.wine_env(include_profile_overrides=True)
    except TypeError:
        return {}


def make_stack(name: str):
    tree = home / ".local" / "opt" / f"wine-d2d1-nspa-{name}"
    prefix = home / f".wine-{name}"
    (tree / "bin").mkdir(parents=True)
    for binary in ("wine", "wineserver"):
        path = tree / "bin" / binary
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    (prefix / "drive_c").mkdir(parents=True)
    return tree, prefix


def main() -> int:
    tree_a, prefix_a = make_stack("11.13")
    tree_b, prefix_b = make_stack("12.0")
    profile_a = LaunchProfile("Ableton", str(prefix_a), str(tree_a), {"PIPEWIRE_LATENCY": "128/48000"})
    profile_b = LaunchProfile("Mantra test", str(prefix_b), str(tree_b), {"PIPEWIRE_LATENCY": "256/48000"})
    save_store(ProfileStore((profile_a, profile_b), active="Ableton"), home=home)

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._watch_timer.stop()
    window._startup_catalogue_timer.stop()
    try:
        print("1. Saved profiles load into the Environment tab and resolve the selected stack")
        check("profile chooser includes Auto-detected and both saved profiles",
              [window.profile_combo.itemText(i) for i in range(window.profile_combo.count())],
              ["Auto-detected", "Ableton", "Mantra test"])
        check("saved profile starts selected", window.profile_combo.currentData(), "Ableton")
        check("selected prefix is the active WPT prefix", window.env.prefix, prefix_a)
        check("selected custom Wine tree is active", window.env.wine_tree, tree_a)
        check("profile override is scoped to standalone launch",
              launch_environment(window.env).get("PIPEWIRE_LATENCY"), "128/48000")

        print("2. Switching profiles persists selection and invalidates previous-prefix inventory")
        window.inv = object()
        window.plugin_table.insertRow(0)
        window.btn_repair.setEnabled(True)
        window.profile_combo.setCurrentIndex(2)
        app.processEvents()
        check("selected profile changes prefix", window.env.prefix, prefix_b)
        check("selected profile changes Wine tree", window.env.wine_tree, tree_b)
        check("selected profile changes standalone environment",
              launch_environment(window.env).get("PIPEWIRE_LATENCY"), "256/48000")
        check("inventory from old prefix is discarded", window.inv, None)
        check("old inventory rows are cleared", window.plugin_table.rowCount(), 0)
        check("prefix-write action is disabled after switch", window.btn_repair.isEnabled(), False)
        check("selection is persisted", window.profile_store.active, "Mantra test")

        print("3. Profile switching remains blocked if any concurrent standalone group is alive")
        assert window.env is not None
        dead_process = SimpleNamespace(pid=7653, poll=lambda: 0)
        dead_launch = StandaloneLaunch(
            process=dead_process,  # type: ignore[arg-type]
            log_path=root / "finished.log",
            size_matches=False,
        )
        live_process = SimpleNamespace(pid=7654, poll=lambda: None)
        live_launch = StandaloneLaunch(
            process=live_process,  # type: ignore[arg-type]
            log_path=root / "live.log",
            size_matches=False,
        )
        window._standalone_launches = [dead_launch, live_launch]
        with patch.object(gui_mod.os, "killpg", side_effect=ProcessLookupError):
            window.profile_combo.setCurrentIndex(1)
            app.processEvents()
        check("finished launch is pruned independently", window._standalone_launches, [live_launch])
        check("active prefix stays unchanged while another standalone runs", window.env.prefix, prefix_b)
        check("profile selection reverts while a standalone runs", window.profile_combo.currentData(), "Mantra test")
        check("refusal is visible", "close standalone" in window.env_note.text().lower(), True)
        second_process = SimpleNamespace(pid=7655, poll=lambda: None)
        second_live = StandaloneLaunch(
            process=second_process,  # type: ignore[arg-type]
            log_path=root / "second-live.log",
            size_matches=False,
        )
        window._standalone_launches = [live_launch, second_live]
        window.profile_combo.setCurrentIndex(1)
        app.processEvents()
        check("multiple active groups remain tracked", window._standalone_launches, [live_launch, second_live])
        check("profile remains unchanged with multiple active groups", window.env.prefix, prefix_b)
        window._standalone_launches.clear()

        print("4. Profile manager is available next to the selector")
        check("profile manager button exists", window.btn_manage_profiles.text(), "Manage profiles…")
        check("profile manager is enabled when idle", window.btn_manage_profiles.isEnabled(), True)

        print("5. Profile manager creates and saves a usable custom profile")
        tree_c, prefix_c = make_stack("12.1")

        def add_profile_from_dialog():
            manager = app.activeModalWidget()
            if not isinstance(manager, LaunchProfilesDialog):
                return

            def fill_editor():
                editor = app.activeModalWidget()
                editor.name_edit.setText("PipeASIO custom")
                editor.prefix_edit.setText(str(prefix_c))
                editor.tree_edit.setText(str(tree_c))
                editor.environment_edit.setPlainText("PIPEWIRE_LATENCY=512/48000")
                editor._save()

            QTimer.singleShot(0, fill_editor)
            manager.btn_new.click()
            QTimer.singleShot(0, manager._accept_profiles)

        QTimer.singleShot(0, add_profile_from_dialog)
        window.manage_launch_profiles()
        check("new profile is saved", "PipeASIO custom" in [p.name for p in window.profile_store.profiles], True)
        check("saved profile appears in selector", window.profile_combo.findData("PipeASIO custom") >= 0, True)
        new_index = window.profile_combo.findData("PipeASIO custom")
        window.profile_combo.setCurrentIndex(new_index)
        app.processEvents()
        check("custom profile is used by standalone loader stack", window.env.prefix, prefix_c)
        check("custom profile override reaches standalone environment",
              launch_environment(window.env).get("PIPEWIRE_LATENCY"), "512/48000")

        plugin_path = prefix_c / "drive_c/Program Files/Common Files/VST3/Mantra.vst3"
        standalone_path = prefix_c / "drive_c/Program Files/Neural DSP/Mantra/Mantra.exe"
        plugin_path.parent.mkdir(parents=True)
        standalone_path.parent.mkdir(parents=True)
        plugin_path.write_bytes(b"plugin")
        standalone_path.write_bytes(b"app")
        plugin = PluginEntry("Mantra.vst3", plugin_path, "vst3", 6, 0.0)
        standalone_entry = PluginEntry("Mantra.exe", standalone_path, "standalone", 3, 0.0)
        window.inv = Inventory(entries=[plugin, standalone_entry])
        fake_launch = StandaloneLaunch(
            process=SimpleNamespace(pid=9123, poll=lambda: None),
            log_path=root / "launch.log",
            size_matches=False,
        )
        with patch("wpt.gui.standalone_mod.launch_standalone", return_value=fake_launch) as launch:
            window._launch_standalone(plugin)
        check("Run in Standalone receives the selected profile environment",
              launch.call_args.args[0].profile_name, "PipeASIO custom")
        check("Run in Standalone receives selected profile override",
              launch_environment(launch.call_args.args[0]).get("PIPEWIRE_LATENCY"), "512/48000")
        window._standalone_launches.clear()

        print("6. Profile switching is refused while a prefix worker is registered")
        window._workers = [SimpleNamespace(isRunning=lambda: True)]
        window.profile_combo.setCurrentIndex(window.profile_combo.findData("Ableton"))
        app.processEvents()
        check("prefix stays unchanged while a worker is registered", window.env.prefix, prefix_c)
        check("selection reverts while a worker is registered",
              window.profile_combo.currentData(), "PipeASIO custom")
        window._workers.clear()
        window._update_workers = [SimpleNamespace(isRunning=lambda: True)]
        window.profile_combo.setCurrentIndex(window.profile_combo.findData("Ableton"))
        app.processEvents()
        check("prefix stays unchanged while an updater worker is registered", window.env.prefix, prefix_c)
        check("selection reverts while an updater worker is registered",
              window.profile_combo.currentData(), "PipeASIO custom")
        window._update_workers.clear()

        print("7. Profile-manager modal rechecks the worker guard before saving")
        tree_d, prefix_d = make_stack("12.2")
        config_path = profile_config_path(home)
        before_modal = config_path.read_bytes()

        def edit_active_while_worker_starts():
            manager = app.activeModalWidget()
            active_row = next(
                i for i in range(manager.profile_list.count())
                if manager.profile_list.item(i).text() == "PipeASIO custom"
            )
            manager.profile_list.setCurrentRow(active_row)

            def fill_active_editor():
                editor = app.activeModalWidget()
                editor.prefix_edit.setText(str(prefix_d))
                editor.tree_edit.setText(str(tree_d))
                editor.environment_edit.setPlainText("PIPEWIRE_LATENCY=1024/48000")
                window._workers.append(SimpleNamespace(isRunning=lambda: True))
                editor._save()

            QTimer.singleShot(0, fill_active_editor)
            manager.btn_edit.click()
            QTimer.singleShot(0, manager._accept_profiles)

        QTimer.singleShot(0, edit_active_while_worker_starts)
        window.manage_launch_profiles()
        check("profile edit is not written after a worker starts during the modal dialog",
              config_path.read_bytes(), before_modal)
        check("live environment stays on the original prefix", window.env.prefix, prefix_c)
        check("busy-modal refusal is visible", "not saved" in window.env_note.text().lower(), True)
        window._workers.clear()
        window.refresh_env()
        config_path.write_bytes(before_modal)
        window.refresh_env()

        print("8. Renaming the active profile keeps that profile selected")
        before_rename = config_path.read_bytes()

        def rename_active_profile():
            manager = app.activeModalWidget()
            active_row = next(
                i for i in range(manager.profile_list.count())
                if manager.profile_list.item(i).text() == "PipeASIO custom"
            )
            manager.profile_list.setCurrentRow(active_row)

            def fill_renamed_editor():
                editor = app.activeModalWidget()
                editor.name_edit.setText("PipeASIO launch")
                editor._save()

            QTimer.singleShot(0, fill_renamed_editor)
            manager.btn_edit.click()
            QTimer.singleShot(0, manager._accept_profiles)

        QTimer.singleShot(0, rename_active_profile)
        window.manage_launch_profiles()
        check("renamed active profile remains active", window.profile_store.active, "PipeASIO launch")
        check("renamed profile keeps its prefix", getattr(window.env, "prefix", None), prefix_c)
        check("environment reports the renamed profile",
              getattr(window.env, "profile_name", None), "PipeASIO launch")
        config_path.write_bytes(before_rename)
        window.refresh_env()

        print("9. Removing the active profile clearly reports the Auto-detected fallback")
        before_remove = config_path.read_bytes()

        def remove_active_profile():
            manager = app.activeModalWidget()
            active_row = next(
                i for i in range(manager.profile_list.count())
                if manager.profile_list.item(i).text() == "PipeASIO custom"
            )
            manager.profile_list.setCurrentRow(active_row)
            manager.btn_remove.click()
            QTimer.singleShot(0, manager._accept_profiles)

        QTimer.singleShot(0, remove_active_profile)
        window.manage_launch_profiles()
        check("removed active profile switches to Auto-detected", window.profile_store.active, None)
        check("active-profile removal warns about fallback",
              "active profile was removed" in window.env_note.text().lower(), True)
        config_path.write_bytes(before_remove)
        window.refresh_env()

        print("10. A stale profile window cannot overwrite another window's save")
        external_tree, external_prefix = make_stack("12.3")
        newer = load_store(home=home)
        external_profile = LaunchProfile("External profile", str(external_prefix), str(external_tree), {})
        save_store(replace(newer, profiles=newer.profiles + (external_profile,)), home=home)
        window.profile_combo.setCurrentIndex(window.profile_combo.findData("Ableton"))
        app.processEvents()
        check("stale GUI selection is refused", window.profile_combo.currentData(), "PipeASIO custom")
        check("external profile survives stale GUI save",
              "External profile" in [p.name for p in load_store(home=home).profiles], True)
        check("stale GUI save explains the conflict", "changed since" in window.env_note.text().lower(), True)
        window.refresh_env()

        print("11. Re-detect blocks on a live standalone and invalidates every prefix-derived view")
        window.inv = object()
        window.plugin_table.insertRow(0)
        window.btn_repair.setEnabled(True)
        window.msi_combo.addItem(str(prefix_c / "cached-old.msi"))
        window.install_table.insertRow(0)
        window.pending_table.insertRow(0)
        window.pending_summary.setText("old prefix pending status")
        window.btn_install_pending.setEnabled(True)
        window.scan_table.insertRow(0)
        window.scan_summary.setText("old prefix scan summary")
        window.scan_products.setPlainText("old prefix scan output")
        window.download_table.insertRow(0)
        window._msi_names = {"old-prefix.msi"}
        window._installed_products = {"old-prefix-product"}
        window._downloads = {"old-prefix-product": prefix_c / "cached-old.msi"}
        external = load_store(home=home)
        save_store(replace(external, active="Ableton"), home=home)

        live_process = SimpleNamespace(pid=8123, poll=lambda: None)
        window._standalone_launches = [
            StandaloneLaunch(process=live_process, log_path=root / "external-live.log", size_matches=False)
        ]
        window.refresh_env()
        check("Re-detect keeps old stack while its standalone group is alive", window.env.prefix, prefix_c)
        check("Re-detect refuses visibly while a standalone group is alive",
              "close standalone" in window.env_note.text().lower(), True)
        window._standalone_launches.clear()

        window.refresh_env()
        check("Re-detect applies the externally selected profile when idle", window.env.prefix, prefix_a)
        check("stale plugin inventory is discarded", window.inv, None)
        check("cached-prefix MSI choices are cleared", window.msi_combo.count(), 0)
        check("old install plan is cleared", window.install_table.rowCount(), 0)
        check("old pending-install results are cleared", window.pending_table.rowCount(), 0)
        check("pending install action is disabled", window.btn_install_pending.isEnabled(), False)
        check("old diagnostics results are cleared", window.scan_table.rowCount(), 0)
        check("old diagnostic report text is cleared", "old prefix scan output" in window.scan_products.toPlainText(), False)
        check("download matcher caches are cleared",
              (window._msi_names, window._installed_products, window._downloads), (set(), set(), {}))
        check("old catalogue rows are cleared", window.download_table.rowCount(), 0)
        download_empty = getattr(window, "download_empty", None)
        check("download empty-state exists after a profile change", download_empty is not None, True)
        if download_empty is not None:
            check("download empty-state is visible", download_empty.isHidden(), False)

        print("12. Broken saved profiles fail closed rather than silently choosing another prefix")
        window.close()
        app.processEvents()
        config_path = profile_config_path(home)
        config_path.write_text("not valid json", encoding="utf-8")
        broken = MainWindow()
        check("invalid profile configuration disables the environment", broken.env, None)
        check("config error is visible", "config error" in broken.env_note.text().lower(), True)
        check("auto-detected option remains visible for repair", broken.profile_combo.itemText(0), "Auto-detected")
        before = config_path.read_text(encoding="utf-8")
        broken.manage_launch_profiles()
        check("profile manager refuses to overwrite malformed config", config_path.read_text(encoding="utf-8"), before)
        broken.close()
        app.processEvents()
    finally:
        window.close()
        app.processEvents()
        root_tmp.cleanup()

    print(f"GUI launch profile checks: {checks - failures}/{checks} passed")
    return 1 if failures else 0


raise SystemExit(main())
