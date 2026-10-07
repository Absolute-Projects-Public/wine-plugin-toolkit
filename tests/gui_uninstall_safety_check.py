#!/usr/bin/env python3
"""GUI uninstall must rescue user presets before the vendor's msiexec callback.

The window is offscreen. Every path is disposable and all MSI/Wine calls are stubbed; the simulated
vendor callback deletes the original preset, so a late rescue cannot pass this check.
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["WPT_NO_UPDATE_CHECK"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402
from wpt import gui as gui_mod, msi, presets  # noqa: E402
from wpt.environment import Environment  # noqa: E402
from wpt.installer import Action, Plan  # noqa: E402


def run(fail_rescue: bool = False, *, shared: bool = False, table_only: bool = False,
        unmapped: bool = False, fail_msiexec: bool = False, timeout_msiexec: bool = False,
        vendor_124: bool = False, still_registered: bool = False,
        registration_unreadable: bool = False, registered: bool = True) -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-gui-uninstall-") as tmp:
        base = Path(tmp)
        home = base / "home"
        env = Environment(home=home, prefix=home / ".wine-ableton", user="tester",
                          wine_tree=home / ".local/opt/wine-d2d1-nspa-test")
        folder = env.program_data / "Neural DSP" / "Product" / "User"
        folder.mkdir(parents=True)
        preset = folder / "My Sound.xml"
        preset.write_text("my work")
        fake_msi = base / "Product.msi"; fake_msi.write_bytes(b"fixture")
        identity = msi.MsiIdentity(product_name="Product", manufacturer="Neural DSP",
                                   product_code="{fixture}")
        plan = Plan(msi=fake_msi, identity=identity,
                    actions=[Action(folder, folder, "PREDIR")],
                    destinations_only=table_only,
                    warnings=["no destination known for AppDataFolder"] if unmapped else [])
        app = QApplication.instance() or QApplication([])
        events: list[str] = []
        messages: list[str] = []
        actual_rescue = presets.rescue_for_plan

        def rescue(env_arg, plan_arg, *a, **kw):
            events.append("rescue")
            if fail_rescue:
                return [("failed", str(preset), "disk full")], [], ["Product"]
            return actual_rescue(env_arg, plan_arg, rescue_root=base / "rescue", stamp="fixture")

        def vendor_uninstall(*_a, **_kw):
            events.append("msiexec")
            if preset.exists():
                preset.unlink()
            if timeout_msiexec:
                return 124, (f"{gui_mod.installer_mod.UNINSTALL_TIMEOUT_DETAIL_PREFIX} "
                             "wine msiexec /x exceeded 600s; uninstall state is unknown. "
                             "Wine processes for this prefix may still be active. "
                             "Confirm they have stopped before inspecting the product and its files or retrying.")
            if vendor_124:
                return 124, "vendor uninstall returned status 124"
            if fail_msiexec:
                return 1603, "simulated vendor uninstall failure"
            return 0, "simulated vendor removal"

        def confirm(_parent, _title, text, *_a, **_kw):
            messages.append(text)
            return QMessageBox.StandardButton.Yes

        def mark_shared(plan_arg, _env):
            if shared:
                plan_arg.shared_paths.add("fixture-shared-path")
                return set(plan_arg.shared_paths)
            return set()

        post_registration = (
            OSError("fixture registry unreadable") if registration_unreadable
            else (registered if still_registered else False)
        )
        with (patch.object(gui_mod, "detect", return_value=env),
              patch.object(gui_mod.QMessageBox, "question", side_effect=confirm),
              patch.object(gui_mod.scan_mod, "is_registered",
                           side_effect=[registered, post_registration]),
              patch.object(gui_mod.msi_mod, "stage_msi", return_value=fake_msi),
              patch.object(gui_mod.msi_mod, "extract", return_value=None),
              patch.object(gui_mod, "build_plan", return_value=plan),
              patch.object(gui_mod.inventory_mod, "mark_cross_product_claims", side_effect=mark_shared),
              patch.object(gui_mod, "uninstall_product", side_effect=vendor_uninstall),
              patch.object(gui_mod.presets_mod, "rescue_for_plan", side_effect=rescue),
              patch.object(gui_mod.installer_mod, "remove_files",
                           side_effect=lambda *_a, **_kw: (events.append("direct-remove"), [])[1]),
              patch.object(gui_mod.installer_mod, "leftovers", return_value=[]),
              patch.object(gui_mod.installer_mod, "stale_registry_edits", return_value=[object()]) as stale,
              patch.object(gui_mod.installer_mod, "purge_registry",
                           side_effect=lambda *_a, **_kw: (events.append("purge"), [])[1]) as purge):
            window = gui_mod.MainWindow()
            app.processEvents()
            window.env = env
            window.refresh_plugins = lambda: None  # inventory rescan is unrelated to ordering
            window.cb_purge.setChecked(fail_msiexec or timeout_msiexec or vendor_124)
            window._confirm_uninstall(fake_msi, identity, registered=registered)
            deadline = time.monotonic() + 20
            while window._jobs_running() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.02)
            app.processEvents()
            assert not window._jobs_running(), ("uninstall worker did not finish", events,
                                                window.plugin_log.toPlainText()[-700:], len(window._workers))
            saved = list((base / "rescue").rglob("My Sound.xml"))
            assert messages and "Back up your own data" in messages[0]
            if shared or table_only or unmapped:
                assert events == [] and preset.exists(), (events, window.plugin_log.toPlainText())
                assert "uninstall refused" in window.plugin_log.toPlainText()
            elif fail_rescue:
                assert events == ["rescue"] and preset.exists(), (events, window.plugin_log.toPlainText())
                assert "preset rescue failed before msiexec" in window.plugin_log.toPlainText()
            elif not registered:
                assert events == ["rescue", "direct-remove"] and preset.exists(), (
                    events, window.plugin_log.toPlainText())
                assert "skipping msiexec /x" in window.plugin_log.toPlainText()
            elif fail_msiexec or timeout_msiexec or vendor_124:
                assert events == ["rescue", "msiexec"], events
                assert not stale.called and not purge.called
                log_text = window.plugin_log.toPlainText()
                assert "uninstall failed" in log_text
                assert "direct file removal and registry purge were skipped" in log_text
                if timeout_msiexec:
                    assert "msiexec timed out (WPT code 124)" in log_text, log_text
                    assert "msiexec exited 124" not in log_text, log_text
                if vendor_124:
                    assert "msiexec exited 124" in log_text, log_text
                    assert "msiexec timed out (WPT code 124)" not in log_text, log_text
                assert saved and saved[0].read_text() == "my work", saved
            elif still_registered:
                assert events == ["rescue", "msiexec"] and not stale.called and not purge.called, events
                assert "still registered" in window.plugin_log.toPlainText()
            elif registration_unreadable:
                assert events == ["rescue", "msiexec"] and not stale.called and not purge.called, events
                assert "could not verify registration" in window.plugin_log.toPlainText()
            else:
                assert events.index("rescue") < events.index("msiexec"), events
                assert len(saved) == 1 and saved[0].read_text() == "my work", (events, saved)
            window.close()
    label = ("failed rescue aborts" if fail_rescue else
             "timeout remains fail-closed" if timeout_msiexec else
             "vendor 124 remains an exit code" if vendor_124 else
             "rescue precedes vendor removal")
    print("ok GUI " + label)


def symlinked_destination_aborts_before_vendor_uninstall() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-symlink-uninstall-gui-") as tmp:
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
        identity = msi.MsiIdentity(product_name="Plugin", product_code="{fixture}")
        plan = Plan(msi=fake_msi, identity=identity,
                    actions=[Action(source, destination, "VST3DIR")],
                    owned_files={destination / relative: msi.MsiFileEntry(
                        "F", "VST3DIR", Path("Plugin.vst3") / relative, 11)})
        app = QApplication.instance() or QApplication([])
        events: list[str] = []
        output_messages: list[str] = []

        with (patch.object(gui_mod, "detect", return_value=env),
              patch.object(gui_mod.QMessageBox, "question",
                           side_effect=lambda *_a, **_kw: QMessageBox.StandardButton.Yes),
              patch.object(gui_mod.msi_mod, "stage_msi", return_value=fake_msi),
              patch.object(gui_mod.msi_mod, "extract", return_value=None),
              patch.object(gui_mod, "build_plan", return_value=plan),
              patch.object(gui_mod.scan_mod, "is_registered", return_value=True),
              patch.object(gui_mod.inventory_mod, "mark_cross_product_claims", return_value=set()),
              patch.object(gui_mod, "uninstall_product",
                           side_effect=lambda *_a, **_kw: (events.append("msiexec"), (0, ""))[1]),
              patch.object(gui_mod.presets_mod, "rescue_for_plan",
                           side_effect=lambda *_a, **_kw: (events.append("rescue"), ([], [], []))[1]),
              patch.object(gui_mod.installer_mod, "remove_files",
                           side_effect=lambda *_a, **_kw: (events.append("direct-remove"), [])[1]),
              patch.object(gui_mod.installer_mod, "leftovers", return_value=[])):
            window = gui_mod.MainWindow()
            app.processEvents()
            window.env = env
            window.refresh_plugins = lambda: None
            window._confirm_uninstall(fake_msi, identity, registered=True)
            deadline = time.monotonic() + 20
            while window._jobs_running() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.02)
            app.processEvents()
            output_messages.append(window.plugin_log.toPlainText())
            assert not window._jobs_running(), output_messages
            assert events == [], (events, output_messages)
            assert backup_file.read_bytes() == b"user backup"
            assert destination.is_symlink()
            assert "symlink" in output_messages[0].lower(), output_messages
            window.close()
    print("ok GUI refuses a symlinked destination before rescue or msiexec")


if __name__ == "__main__":
    run()
    run(fail_rescue=True)
    run(fail_msiexec=True)
    run(timeout_msiexec=True)
    run(vendor_124=True)
    run(shared=True)
    run(table_only=True)
    run(unmapped=True)
    run(still_registered=True)
    run(registration_unreadable=True)
    run(registered=False)
    symlinked_destination_aborts_before_vendor_uninstall()
    print("GUI uninstall safety: 12/12 passed (offscreen, simulated vendor)")
