"""Right-click actions for installed plugins and custom-Wine standalone launch.

Run with PySide6: QT_QPA_PLATFORM=offscreen python3 tests/gui_plugin_context_menu_check.py
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["WPT_SCREENSHOT_MODE"] = "1"
os.environ["WPT_NO_UPDATE_CHECK"] = "1"

from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QCloseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QTableWidgetItem  # noqa: E402
import shiboken6  # noqa: E402

from wpt.environment import Environment  # noqa: E402
from wpt.inventory import Inventory, PluginEntry  # noqa: E402
import wpt.gui as gui_mod  # noqa: E402
from wpt.gui import MainWindow  # noqa: E402
from wpt.standalone import StandaloneLaunch  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
        failures.append(label)


def make_env(root: Path) -> Environment:
    home = root / "home"
    wine_tree = home / ".local/opt/wine-d2d1-nspa-11.13"
    (wine_tree / "bin").mkdir(parents=True)
    (wine_tree / "bin/wine").write_text("wine marker")
    (wine_tree / "bin/wineserver").write_text("wineserver marker")
    prefix = root / "prefix"
    (prefix / "drive_c/Program Files").mkdir(parents=True)
    return Environment(home=home, wine_tree=wine_tree, prefix=prefix, user="tester")


class FakeProcess:
    def __init__(self, pid: int = 1_000_000_000, returncode: int | None = 0):
        self.pid = pid
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode


def make_launch(root: Path, *, size_matches: bool = False) -> StandaloneLaunch:
    return StandaloneLaunch(
        process=FakeProcess(),
        log_path=root / "standalone.log",
        size_matches=size_matches,
    )


def find_action(menu, prefix: str):
    return next((action for action in menu.actions() if action.text().startswith(prefix)), None)


def dispose_menu(app: QApplication, menu) -> None:
    if shiboken6.isValid(menu):
        menu.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def main() -> int:
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix="wpt-menu-") as temp:
        root = Path(temp)
        env = make_env(root)
        vst3 = env.vst3_dir / "Archetype Rabea X.vst3"
        vst3.parent.mkdir(parents=True)
        vst3.write_bytes(b"0123456789")
        exe = env.program_files / "Neural DSP" / "Archetype Rabea X" / "Archetype Rabea X.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"0123456789")
        selected = PluginEntry(vst3.name, vst3, "vst3", 10, 0.0)
        app_entry = PluginEntry(exe.name, exe, "standalone", 10, 0.0)

        # Avoid environment discovery or update/network work; inject the disposable target.
        with patch.object(MainWindow, "refresh_env", lambda self: None):
            window = MainWindow()
        window.env = env
        window.inv = Inventory(entries=[selected, app_entry])

        print("1. The menu exposes safe, visible actions and an explicit unverified label")
        menu = window._build_plugin_menu(selected)
        actions = {action.text(): action for action in menu.actions()}
        check("browse action uses the user-facing label", "Browse local files" in actions, True)
        run = find_action(menu, "Run in Standalone")
        check("standalone action exists", run is not None, True)
        check("unverified state appears in the visible action name",
              run.text() if run else None, "Run in Standalone (unverified)")
        check("unique match enables launch", run.isEnabled() if run else None, True)
        check("menu tooltips are enabled", menu.toolTipsVisible(), True)
        check("licensing risk is disclosed in the tooltip",
              "licensing service" in run.toolTip().lower() if run else False, True)
        check("tracked launch groups are disclosed",
              "tracks launched groups" in run.toolTip().lower() if run else False, True)
        check("separate WPT windows are disclosed as uncoordinated",
              "will not rediscover them" in run.toolTip().lower() if run else False, True)
        check("helper and licensing-service caution is disclosed",
              "helpers or licensing services" in run.toolTip().lower() if run else False, True)
        print("2. The menu action delegates to the selected custom Wine target")
        launch = make_launch(root)
        with patch.object(gui_mod.standalone_mod, "launch_standalone", return_value=launch) as start:
            run.trigger()
        check("selected executable passed to launcher", start.call_args.args, (env, exe.resolve()))
        check("unverified launch has no trusted expected size",
              start.call_args.kwargs.get("expected_size"), None)
        check("launch log includes PID", str(launch.pid) in window.plugin_log.toPlainText(), True)
        check("launch log includes its diagnostic file", str(launch.log_path) in window.plugin_log.toPlainText(), True)
        check("unverified ownership remains explicit", "unverified" in window.plugin_log.toPlainText().lower(), True)
        dispose_menu(app, menu)
        window._standalone_launches.clear()

        print("3. A verified menu action is described as a scan-time check")
        msi = root / "Rabea.msi"
        msi.write_text("synthetic MSI")
        verified_plugin = PluginEntry(vst3.name, vst3, "vst3", 10, 0.0, expected_size=10, msi=msi)
        verified_app = PluginEntry(exe.name, exe, "standalone", 10, 0.0, expected_size=10, msi=msi)
        window.inv = Inventory(entries=[verified_plugin, verified_app])
        verified_menu = window._build_plugin_menu(verified_plugin)
        verified_run = find_action(verified_menu, "Run in Standalone")
        check("verified action tooltip says at last scan",
              "last inventory scan" in verified_run.toolTip().lower() if verified_run else False, True)
        check("size mismatch is documented as launch refusal",
              "refuses the run until inventory is refreshed" in verified_run.toolTip().lower()
              if verified_run else False, True)
        verified_launch = make_launch(root, size_matches=True)
        with patch.object(gui_mod.standalone_mod, "launch_standalone", return_value=verified_launch) as start:
            verified_run.trigger()
        check("expected size is passed for launch-time recheck",
              start.call_args.kwargs.get("expected_size"), 10)
        check("fresh size match is recorded", "size matches at launch" in window.plugin_log.toPlainText().lower(), True)
        window._standalone_launches.clear()
        launches_before = len(window._standalone_launches)
        with patch.object(
            gui_mod.standalone_mod, "launch_standalone",
            side_effect=ValueError("changed size since the last inventory scan"),
        ):
            window._launch_standalone(verified_plugin)
        check("changed launch-time size refuses launch",
              "changed size since the last inventory scan" in window.plugin_log.toPlainText().lower(), True)
        check("refused size mismatch adds no process handle",
              len(window._standalone_launches), launches_before)
        dispose_menu(app, verified_menu)

        print("4. Missing, ambiguous or unavailable environment disables the action with a visible reason")
        no_app = PluginEntry("No App.vst3", env.vst3_dir / "No App.vst3", "vst3", 10, 0.0)
        no_app_menu = window._build_plugin_menu(no_app)
        no_app_run = find_action(no_app_menu, "Run in Standalone")
        check("missing app action is disabled", no_app_run.isEnabled() if no_app_run else None, False)
        check("missing-app explanation is available", bool(no_app_run.toolTip()) if no_app_run else False, True)
        dispose_menu(app, no_app_menu)

        second = env.program_files / "Other" / "Archetype Rabea X.exe"
        second.parent.mkdir()
        second.write_bytes(b"second marker")
        window.inv = Inventory(entries=[selected, app_entry, PluginEntry(second.name, second, "standalone", 10, 0.0)])
        ambiguous_menu = window._build_plugin_menu(selected)
        ambiguous_run = find_action(ambiguous_menu, "Run in Standalone")
        check("ambiguous app action is disabled", ambiguous_run.isEnabled() if ambiguous_run else None, False)
        check("ambiguity is visible in the tooltip",
              "multiple" in ambiguous_run.toolTip().lower() if ambiguous_run else False, True)
        dispose_menu(app, ambiguous_menu)

        window.env = None
        window.inv = None
        unavailable_menu = window._build_plugin_menu(selected)
        unavailable_run = find_action(unavailable_menu, "Run in Standalone")
        check("no environment disables launch", unavailable_run.isEnabled() if unavailable_run else None, False)
        check("no environment reason is visible", bool(unavailable_run.toolTip()) if unavailable_run else False, True)
        dispose_menu(app, unavailable_menu)
        window.env = env
        window.inv = Inventory(entries=[selected, app_entry])

        print("5. A live standalone permits another launch, while prefix writes remain blocked")
        with patch.object(window, "_jobs_running", return_value=True), \
             patch.object(gui_mod.standalone_mod, "launch_standalone") as start:
            window._launch_standalone(selected)
        check("active prefix job blocks launch", start.called, False)
        check("prefix-job refusal is logged", "prefix job" in window.plugin_log.toPlainText().lower(), True)

        exited_with_child = StandaloneLaunch(
            process=FakeProcess(pid=778, returncode=0), log_path=root / "child.log", size_matches=False
        )
        window._standalone_launches = [exited_with_child]
        with patch.object(gui_mod.os, "killpg", return_value=None) as group_check:
            remaining = window._active_standalone_launches()
        check("live process group stays busy after launcher exit", remaining, [exited_with_child])
        check("surviving process group is queried without signaling", group_check.call_args.args, (778, 0))
        with patch.object(gui_mod.os, "killpg", side_effect=ProcessLookupError):
            remaining = window._active_standalone_launches()
        check("empty process group is pruned", remaining, [])

        permission_denied = StandaloneLaunch(
            process=FakeProcess(pid=779, returncode=0), log_path=root / "permission.log", size_matches=False
        )
        window._standalone_launches = [permission_denied]
        with patch.object(gui_mod.os, "killpg", side_effect=PermissionError):
            remaining = window._active_standalone_launches()
        check("permission error keeps the process group busy", remaining, [permission_denied])

        poll_error = StandaloneLaunch(
            process=FakeProcess(pid=780, returncode=0), log_path=root / "poll-error.log", size_matches=False
        )
        window._standalone_launches = [poll_error]
        with patch.object(poll_error.process, "poll", side_effect=OSError("poll failed")), \
             patch.object(gui_mod.os, "killpg") as no_group_probe:
            remaining = window._active_standalone_launches()
        check("poll error keeps launch busy", remaining, [poll_error])
        check("failed poll does not probe a process group", no_group_probe.called, False)

        first_live_launch = StandaloneLaunch(
            process=FakeProcess(pid=780, returncode=None), log_path=root / "running.log", size_matches=False
        )
        window._standalone_launches = [
            first_live_launch
        ]
        busy_menu = window._build_plugin_menu(selected)
        busy_run = find_action(busy_menu, "Run in Standalone")
        check("active standalone leaves a second launch enabled", busy_run.isEnabled() if busy_run else None, True)
        second_live_launch = StandaloneLaunch(
            process=FakeProcess(pid=781, returncode=None), log_path=root / "second-running.log", size_matches=False
        )
        with patch.object(
            gui_mod.standalone_mod, "launch_standalone", return_value=second_live_launch
        ) as second_start:
            window._launch_standalone(selected)
        check("active standalone permits a second launch", second_start.called, True)
        check("second launch keeps the window's selected environment",
              second_start.call_args.args[0] is env, True)
        check("both concurrent standalone groups remain tracked",
              window._standalone_launches, [first_live_launch, second_live_launch])
        dispose_menu(app, busy_menu)
        window.plugin_table.setRowCount(1)
        window.plugin_table.selectRow(0)
        cell = QTableWidgetItem(selected.name)
        cell.setData(Qt.ItemDataRole.UserRole, selected)
        window.plugin_table.setItem(0, 0, cell)
        with patch.object(gui_mod, "set_plugin_enabled") as rename:
            window.toggle_selected(enabled=False)
        check("active standalone blocks enable/disable", rename.called, False)
        check("write refusal asks to close the app first",
              "close" in window.plugin_log.toPlainText().lower(), True)

        window.msi_combo.addItem(str(msi))
        window.msi_combo.setCurrentText(str(msi))
        with patch.object(window, "_selected_entry", return_value=verified_plugin), \
             patch.object(window, "_spawn") as repair_spawn:
            window.repair_selected()
        check("active standalone blocks repair", repair_spawn.called, False)

        with patch.object(window, "_selected_entry", return_value=verified_plugin), \
             patch.object(window, "_spawn") as uninstall_spawn:
            window.uninstall_selected()
        check("active standalone blocks uninstall before MSI reads", uninstall_spawn.called, False)

        ident = SimpleNamespace(label="Rabea X", product_code="{TEST-PRODUCT}")
        with patch.object(gui_mod.QMessageBox, "question", return_value=gui_mod.QMessageBox.StandardButton.Yes) as question, \
             patch.object(window, "_spawn") as confirm_spawn:
            window._confirm_uninstall(msi, ident, registered=False)
        check("uninstall asks for confirmation", question.called, True)
        check("active standalone blocks uninstall after confirmation", confirm_spawn.called, False)

        pending = SimpleNamespace(product="Rabea X", path=root / "Setup.exe")
        with patch.object(window, "_selected_installer", return_value=pending), \
             patch.object(gui_mod.QMessageBox, "question", return_value=gui_mod.QMessageBox.StandardButton.No) as pending_question, \
             patch.object(window, "_spawn") as pending_spawn:
            window.install_pending_selected()
        check("active standalone blocks pending-wrapper install before prompt", pending_question.called, False)
        check("active standalone blocks pending-wrapper install", pending_spawn.called, False)

        with patch.object(window, "_spawn") as install_spawn, \
             patch.object(gui_mod.QMessageBox, "warning") as install_warning:
            window.run_install(dry_run=False)
        check("active standalone blocks MSI install", install_spawn.called, False)
        check("blocked MSI install does not show a modal warning", install_warning.called, False)

        def finish_preview(*_args, **kwargs):
            kwargs["restore_text"]()
        with patch.object(window, "_spawn", side_effect=finish_preview) as preview_spawn:
            window.run_install(dry_run=True)
        check("dry-run preview remains allowed during standalone", preview_spawn.called, True)
        check("active-standalone refusal is logged",
              "close standalone" in window.install_log.toPlainText().lower(), True)
        window._standalone_launches = []

        print("6. Launch failures are logged, and Browse local files opens the plugin folder")
        with patch.object(gui_mod.standalone_mod, "launch_standalone", side_effect=OSError("cannot execute")):
            window._launch_standalone(selected)
        check("launch OSError is reported", "cannot execute" in window.plugin_log.toPlainText(), True)
        with patch.object(gui_mod.standalone_mod, "launch_standalone", side_effect=ValueError("candidate changed")):
            window._launch_standalone(selected)
        check("launch ValueError is reported", "candidate changed" in window.plugin_log.toPlainText(), True)
        browse_menu = window._build_plugin_menu(selected)
        browse = browse_menu.actions()[[a.text() for a in browse_menu.actions()].index("Browse local files")]
        with patch.object(gui_mod.QDesktopServices, "openUrl", return_value=True) as open_url:
            browse.trigger()
        check("browse reveals the plugin's containing directory",
              open_url.call_args.args[0].toLocalFile(), str(vst3.parent))
        dispose_menu(app, browse_menu)

        print("7. Context-menu popups are disposed after their modal session")
        window.plugin_table.setRowCount(1)
        item = QTableWidgetItem(selected.name)
        item.setData(Qt.ItemDataRole.UserRole, selected)
        window.plugin_table.setItem(0, 0, item)
        window.plugin_table.selectRow(0)

        class FakeMenu:
            executed = False
            deleted = False

            def exec(self, _position):
                self.executed = True

            def deleteLater(self):
                self.deleted = True

        fake_menu = FakeMenu()
        with patch.object(window, "_build_plugin_menu", return_value=fake_menu):
            window._plugin_context_menu(QPoint(5, 5))
        check("context menu exec was called", fake_menu.executed, True)
        check("context menu is deleted after exec returns", fake_menu.deleted, True)

        print("8. The download-row context menu is disposed after its modal session")
        release = SimpleNamespace(product="Archetype Rabea X")
        window.download_table.setRowCount(1)
        release_item = QTableWidgetItem(release.product)
        release_item.setData(Qt.ItemDataRole.UserRole, release)
        window.download_table.setItem(0, 0, release_item)
        download_menu = FakeMenu()
        with patch.object(window, "_build_download_menu", return_value=download_menu):
            window._download_context_menu(QPoint(5, 5))
        check("download context menu exec was called", download_menu.executed, True)
        check("download context menu is deleted", download_menu.deleted, True)

        print("9. Dismissing the standalone close prompt keeps WPT open")
        window._standalone_launches = [
            StandaloneLaunch(process=FakeProcess(returncode=None), log_path=root / "active.log", size_matches=False)
        ]

        class FakeCloseBox:
            class Icon:
                Warning = "warning"

            class ButtonRole:
                AcceptRole = "accept"
                DestructiveRole = "destructive"

            instances = []
            choose_close = False

            def __init__(self, _parent):
                self.buttons = []
                self.clicked = None
                self.instances.append(self)

            def setIcon(self, _icon):
                pass

            def setWindowTitle(self, _title):
                pass

            def setText(self, _text):
                self.text = _text

            def addButton(self, label, _role):
                self.buttons.append(label)
                return label

            def setDefaultButton(self, button):
                self.default = button

            def setEscapeButton(self, button):
                self.escape = button

            def exec(self):
                if self.choose_close:
                    self.clicked = self.buttons[1]

            def clickedButton(self):
                return self.clicked

        with patch.object(gui_mod, "QMessageBox", FakeCloseBox):
            keep_if_dismissed = window._ask_keep_open_for_standalone(window._standalone_launches)
        close_box = FakeCloseBox.instances[-1]
        check("dismissal is not explicit consent to close", keep_if_dismissed, True)
        check("keep-open is the default button", close_box.default, "Keep WPT open")
        check("Escape is explicitly assigned to keep-open", close_box.escape, "Keep WPT open")
        check("close button makes the leave-running behavior explicit",
              close_box.buttons[1], "Close WPT; don't stop apps")
        check("close warning says WPT does not signal launched standalone",
              "does not signal" in close_box.text.lower(), True)
        check("close warning preserves the prefix-write caution",
              "prefix-write protection ends" in close_box.text.lower(), True)
        check("close warning discloses that a reopened WPT will not track apps",
              "does not rediscover them" in close_box.text.lower(), True)
        FakeCloseBox.choose_close = True
        with patch.object(gui_mod, "QMessageBox", FakeCloseBox):
            explicit_close = window._ask_keep_open_for_standalone(window._standalone_launches)
        check("only the explicit leave-app-running button permits close", explicit_close, False)
        FakeCloseBox.choose_close = False

        close_event = QCloseEvent()
        with patch.object(window, "_ask_keep_open_for_standalone", return_value=True) as keep_prompt:
            window.closeEvent(close_event)
        check("keep-tracking answer prevents close", close_event.isAccepted(), False)
        check("active launch is presented for decision", keep_prompt.call_args.args[0], window._standalone_launches)

        print("10. Explicit close does not reprompt after a read-only job")
        first_event = QCloseEvent()
        second_event = QCloseEvent()
        with patch.object(window, "_ask_keep_open_for_standalone", return_value=False) as close_anyway, \
             patch.object(window, "_jobs_running", side_effect=[True, False]), \
             patch.object(window, "_update_job_running", return_value=False), \
             patch.object(window, "_finishing_dialog") as finishing_dialog:
            window.closeEvent(first_event)
            window.closeEvent(second_event)
        check("explicit leave-apps-running choice waits for active read-only job",
              first_event.isAccepted(), False)
        check("closing state survives the wait", window._closing, True)
        check("job completion accepts close without re-prompt", second_event.isAccepted(), True)
        check("standalone close prompt appeared only once", close_anyway.call_count, 1)
        check("wait dialog was shown", finishing_dialog.called, True)

        print("11. Update restart is refused while a standalone process group is active")
        window._standalone_launches = [
            StandaloneLaunch(process=FakeProcess(returncode=None), log_path=root / "update-active.log", size_matches=False)
        ]
        with patch.object(gui_mod.updates_mod, "launch_install") as update_launch, \
             patch.object(gui_mod.QMessageBox, "warning") as update_warning:
            window._launch_update_install(root / "update.pkg")
        check("active standalone blocks update/restart", update_launch.called, False)
        check("blocked update explains why", update_warning.called, True)

        print("12. Closing WPT leaves concurrently launched detached apps alive")
        fake_wine = env.wine_binary
        fake_wine.write_text("#!/bin/sh\nexec /bin/sleep 30\n")
        fake_wine.chmod(0o755)
        real_launches = []
        close_window = None
        try:
            with patch.dict(os.environ, {"XDG_CACHE_HOME": str(root / "cache")}):
                for _ in range(2):
                    real_launches.append(gui_mod.standalone_mod.launch_standalone(env, exe))
            with patch.object(MainWindow, "refresh_env", lambda self: None):
                close_window = MainWindow()
            close_window.env = env
            close_window._standalone_launches = real_launches.copy()
            check("each detached app receives its own process group",
                  [os.getpgid(item.pid) == item.pid for item in real_launches], [True, True])
            signal_calls = []
            real_killpg = os.killpg

            def record_process_signal(pgid, sig):
                if sig != 0:
                    signal_calls.append((pgid, sig))
                return real_killpg(pgid, sig)

            with patch.object(os, "killpg", side_effect=record_process_signal):
                with patch.object(close_window, "_ask_keep_open_for_standalone", return_value=False):
                    accepted = close_window.close()
            check("WPT accepts close while detached apps run", accepted, True)
            check("WPT close sends no process-group signals", signal_calls, [])
            alive_through_settle = [True] * len(real_launches)
            deadline = time.monotonic() + 0.75
            while time.monotonic() < deadline:
                for index, item in enumerate(real_launches):
                    if item.process.poll() is not None:
                        alive_through_settle[index] = False
                        continue
                    try:
                        os.killpg(item.pid, 0)
                    except ProcessLookupError:
                        alive_through_settle[index] = False
                if not all(alive_through_settle):
                    break
                time.sleep(0.05)
            check("both fake-Wine child process groups survive WPT close",
                  alive_through_settle, [True, True])
        finally:
            for item in real_launches:
                try:
                    os.killpg(item.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            for item in real_launches:
                try:
                    item.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(item.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    item.process.wait(timeout=5)
            survivors = []
            for item in real_launches:
                try:
                    os.killpg(item.pid, 0)
                except ProcessLookupError:
                    continue
                survivors.append(item.pid)
            check("scratch process groups are cleaned up after the assertion", survivors, [])
            if close_window is not None:
                close_window._standalone_launches.clear()

        window._standalone_launches = []
        window.close()
        app.processEvents()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): " + "; ".join(failures))
        return 1
    print("plugin context-menu checks: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())