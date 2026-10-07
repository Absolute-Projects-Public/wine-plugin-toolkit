"""PySide6 front end -- a thin shell over the same functions the CLI uses.

Design rules:
  * no logic lives here; if the GUI and the CLI could disagree, the bug is in
    the core modules, not in this file
  * anything slow (msiextract, a prefix scan) runs on a worker thread, so the
    window never freezes mid-install
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QMenu,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from . import catalogue as catalogue_mod
from . import installers as installers_mod
from .installers import match_download
from . import inventory as inventory_mod
from . import standalone as standalone_mod
from . import updates as updates_mod
from . import launch_profiles as launch_profiles_mod
from .launch_profiles import ProfileConfigError, ProfileStore
from .launch_profile_ui import LaunchProfilesDialog
from . import msi as msi_mod
from . import presets as presets_mod
from . import products as products_mod
from . import scan as scan_mod
from . import sources as sources_mod
from . import wrappers as wrappers_mod
from . import installer as installer_mod
from .environment import Environment, EnvironmentError_, detect
from .installer import (
    DISABLED_SUFFIX,
    apply_plan,
    build_plan,
    build_plan_from_tables,
    filter_needing_repair,
    set_enabled as set_plugin_enabled,
    uninstall as uninstall_product,
    verify_plan,
)
from .scan import Report
from .scan import scan as scan_prefix

SCRATCH = Path.home() / ".cache" / "wpt" / "extract"

# Width the fallback "finishing the job" dialog gives its wrapped text, and the floor it opens at.
# A wrapped QLabel hints badly enough that the window once came up 60x60 on a real desktop.
_DIALOG_TEXT_WIDTH = 460


def _fit_columns(table, stretch: dict[int, int] | None = None, contents=(), elide: dict[int, bool] | None = None,
                min_section: int = 64) -> None:
    """Give the long-text columns the room and the short ones only what they need.

    A single `Stretch` column took the whole window and clipped the column that actually holds
    the long text (the installer path), which is backwards. Now: short columns size to their
    content, the long ones share the leftover space between them, path columns elide in the
    middle so both ends stay readable, and every path cell carries its full value as a tooltip.
    """
    header = table.horizontalHeader()
    header.setStretchLastSection(False)
    # A content-sized column collapses to the width of its header text when the table has no rows:
    # at 900 px the Pending Install table drew Status 40 px, Kind 35 px and Version 50 px, and the
    # Diagnostics "Status" column was 40 px - which reads as a broken table, not as an empty one.
    header.setMinimumSectionSize(min_section)
    for column in contents:
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
    for column, minimum in (stretch or {}).items():
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        table.setColumnWidth(column, minimum)
    if elide:
        table.setTextElideMode(Qt.TextElideMode.ElideMiddle)


def _log_pane(placeholder: str, blocks: int = 1000) -> QPlainTextEdit:
    """A read-only output pane that says what it is *before* a job writes to it.

    Empty, it was a large grey rectangle under the table on Install, Pending and Diagnostics -
    indistinguishable from a widget that failed to load, and it kept the table from using the
    height. The placeholder line costs nothing and disappears the moment output arrives.
    """
    pane = QPlainTextEdit()
    pane.setReadOnly(True)
    pane.setMaximumBlockCount(blocks)
    pane.setPlaceholderText(placeholder)
    return pane


def app_icon() -> QIcon:
    """The window and taskbar icon, from `wpt/data/wpt.png` inside the package.

    Shipped in the Python package rather than only as a theme icon so a source run
    (`python3 -m wpt.gui`) carries it too; the Arch package also installs the hicolor sizes and a
    .desktop entry, which is what a launcher reads.
    """
    folder = Path(__file__).resolve().parent / "data" / "icons"
    icon = QIcon()
    for size in (16, 32, 48, 64, 128, 256, 512):
        path = folder / f"wpt-{size}.png"
        if path.exists():
            icon.addFile(str(path))
    return icon


def _dark_palette() -> QPalette:
    """A dark palette for machines that are not dark already.

    The window takes the system's own palette by default (KDE's Breeze here), which is what makes
    it look native. The toggle exists for the other case - someone running a light desktop who
    prefers this tool dark, or a screenshot that has to be taken in dark mode on any machine - so
    this is a plain functional palette, deliberately not an attempt to imitate Breeze.
    """
    palette = QPalette()
    window = QColor(49, 54, 59)
    base = QColor(35, 38, 41)
    text = QColor(239, 240, 241)
    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, base)
    palette.setColor(QPalette.ColorRole.AlternateBase, window)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, window)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.ToolTipBase, base)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Highlight, QColor(61, 174, 233))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(252, 252, 252))
    for role in (QPalette.ColorRole.PlaceholderText, QPalette.ColorRole.Mid,
                 QPalette.ColorRole.Dark, QPalette.ColorRole.Shadow):
        palette.setColor(role, QColor(150, 155, 160))
    for role in (QPalette.ColorRole.Light, QPalette.ColorRole.Midlight):
        palette.setColor(role, QColor(70, 76, 82))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(140, 145, 150))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(140, 145, 150))
    return palette


def _empty_note(text: str) -> QLabel:
    """The line shown instead of a table that has no rows.

    An empty QTableWidget is a large empty grid that looks like a widget which failed to load; a
    sentence in its place says the same thing the table cannot, and the tab stops looking broken
    before the first scan.
    """
    note = QLabel(text)
    note.setWordWrap(True)
    note.setAlignment(Qt.AlignmentFlag.AlignCenter)
    note.setMargin(12)
    note.hide()
    return note


def _show_rows(table, note: QLabel, rows: int) -> None:
    """Show the table when it has rows, the note when it does not. One or the other, never both."""
    if rows:
        note.hide()
        table.show()
    else:
        table.hide()
        note.show()


def _key(text: str) -> str:
    """Loose product key for matching catalogue entries to files and prefixes."""
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _split(text: str) -> list[str]:
    return [part for part in re.split(r"[^A-Za-z0-9]+", text.lower()) if part]


class Worker(QThread):
    """Runs one callable off the GUI thread and reports lines back as they come."""

    line = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:  # noqa: D102 - Qt entry point
        try:
            result = self._fn(self.line.emit, *self._args, **self._kwargs)
            self.done.emit(result)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user in the UI
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Wine Plugin Toolkit")
        self.setWindowIcon(app_icon())
        self.resize(980, 700)
        self.env = None
        self.profile_store = ProfileStore()
        self.profile_load_error: str | None = None
        self.plan = None
        self.inv = None
        self.worker: Worker | None = None
        self._workers: list[Worker] = []
        self._standalone_launches: list[standalone_mod.StandaloneLaunch] = []
        self._closing = False
        # shown while a job is still running and the user has asked to leave; modeless on purpose,
        # because the waits below pump the event loop and it has to stay clickable
        self._closing_dialog = None
        self._forced = False
        # non-blocking notices, held so Qt does not collect them while they are on screen
        self._notices: list[QDialog] = []
        # update checking owns its own workers so an update never queues behind, or blocks,
        # the prefix jobs (and vice versa)
        self._downloads_scan_running = False
        self._downloads_before: dict[str, Path] | None = None
        self._update_worker: Worker | None = None
        # every update worker is also held here until it stops: `_update_worker` alone is replaced
        # by the next check, and dropping the last reference to a running QThread is the SIGABRT
        self._update_workers: list[Worker] = []
        self._update_busy = False
        self._update_release = None
        self._update_path: Path | None = None
        # the palette the desktop gave us, kept so the toggle is reversible without a restart
        self._system_palette = QApplication.palette()

        # Tab order is the workflow, left to right: what the machine has, what is installed, what
        # can be fetched, what has landed and is waiting to be installed, the MSI installer itself,
        # then the diagnostics. (He asked for exactly this order on 2026-09-26.)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._env_tab(), "Environment")
        self.tabs.addTab(self._plugins_tab(), "Plugins")
        self.tabs.addTab(self._downloads_tab(), "Download")
        self.tabs.addTab(self._pending_tab(), "Pending Install")
        self.tabs.addTab(self._install_tab(), "Install MSI")
        self.tabs.addTab(self._scan_tab(), "Diagnostics")
        self.setCentralWidget(self.tabs)

        toolbar = QToolBar("Toolkit")
        toolbar.setMovable(False)
        self.cb_dark = QCheckBox("Dark mode")
        self.cb_dark.setToolTip(
            "Use a dark palette instead of the desktop's own. Off by default: the window normally\n"
            "follows your system theme, which is what makes it look native."
        )
        self.cb_dark.toggled.connect(self.apply_theme)
        toolbar.addWidget(self.cb_dark)
        toolbar.addSeparator()
        self.btn_about = QPushButton("Settings & about")
        self.btn_about.setToolTip("Versions, the paths this toolkit resolved, and the config file")
        self.btn_about.clicked.connect(lambda _checked=False: self.open_settings())
        toolbar.addWidget(self.btn_about)
        toolbar.addSeparator()
        self.btn_check_updates = QPushButton("Check for updates")
        self.btn_check_updates.setToolTip("Ask GitHub whether a newer release exists")
        self.btn_check_updates.clicked.connect(lambda _checked=False: self.check_for_updates(manual=True))
        toolbar.addWidget(self.btn_check_updates)
        # last: the toolbar above owns the switch this reads the saved value into
        self.apply_theme()
        self.lbl_update_state = QLabel("")
        toolbar.addWidget(self.lbl_update_state)
        self.addToolBar(toolbar)

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._wait_for_jobs)
        # Screenshot mode: a documented way to take a publishable image on a real desktop (in dark
        # mode, on his machine). It skips the startup refresh and the update check, and empties the
        # tables, so the capture shows the interface instead of this machine's plugins.
        self.screenshot_mode = bool(os.environ.get("WPT_SCREENSHOT_MODE"))
        self.refresh_env()
        if self.screenshot_mode:
            if os.environ.get("WPT_SCREENSHOT_DARK"):
                # the palette is set without touching the saved setting: a screenshot is not a
                # preference, and taking one must not switch the user's window to dark
                self.apply_theme(True, remember=False)
            # Blanking once is not enough: the startup itself schedules the catalogue load 200 ms
            # later, which refills the download table from the real ~/Downloads. Settle afterwards.
            QTimer.singleShot(1500, self._screenshot_settle)
        else:
            # a quiet check shortly after startup; the result is cached for a day
            QTimer.singleShot(2500, lambda: self.check_for_updates(manual=False))

    # ------------------------------------------------------------------ updates
    def check_for_updates(self, manual: bool = False) -> None:
        """Ask GitHub for the newest release. Never blocks the window.

        The startup check stays quiet when there is nothing to say; a manual click always says
        something, including "could not check", because a button that appears to do nothing is
        worse than a button that reports a failure.
        """
        if self._closing:
            return
        if self._update_busy:
            self.lbl_update_state.setText("checking...")
            return
        if not manual and not updates_mod.update_check_enabled():
            return

        cached = None if manual else updates_mod.read_cache()
        if cached is not None and not cached.get("error") and cached.get("tag"):
            self._update_checked(updates_mod.Release(tag=cached["tag"],
                                                     version=tuple(cached.get("version") or ()),
                                                     html_url=cached.get("html_url") or ""),
                                 None, manual, from_cache=True)
            return

        self._update_busy = True
        self.lbl_update_state.setText("checking GitHub..." if manual else "checking for updates...")

        def work(emit):
            try:
                release = updates_mod.latest_release()
                updates_mod.write_cache(release, None)
                return release, None
            except updates_mod.UpdateError as exc:
                updates_mod.write_cache(None, str(exc))
                return None, str(exc)

        worker = Worker(work)
        self._update_worker = worker
        self._update_workers.append(worker)
        worker.finished.connect(lambda: self._forget_update_worker(worker))

        def done(result):
            self._update_busy = False
            release, error = result if isinstance(result, tuple) else (None, None)
            self._update_checked(release, error, manual)

        def failed(message):
            # Worker reports failures as a string on its own signal, not as a returned value
            self._update_busy = False
            self._update_checked(None, message, manual)

        worker.done.connect(done)
        worker.failed.connect(failed)
        worker.start()

    def _update_checked(self, release, error, manual: bool, from_cache: bool = False) -> None:
        if error:
            self.lbl_update_state.setText(f"update check failed: {error}")
            if manual:
                QMessageBox.warning(self, "Could not check for updates",
                                    f"{error}\n\nThe toolkit still works - this only means it "
                                    "could not reach GitHub.")
            return
        if release is None:
            self.lbl_update_state.setText("")
            return
        if not updates_mod.is_newer(release.version, updates_mod.parse_version(updates_mod.__version__)):
            self.lbl_update_state.setText(f"up to date ({updates_mod.__version__})")
            if manual:
                QMessageBox.information(self, "No updates",
                                        f"wpt {updates_mod.__version__} is the newest release "
                                        f"({release.tag}).")
            return

        self.lbl_update_state.setText(f"{release.version_text} available")
        if not manual and updates_mod.skipped_version() == release.version_text:
            self.log(f"wpt {release.version_text} is available (you asked not to be told again)")
            return
        self._offer_update(release)

    def _offer_update(self, release) -> None:
        """Ask before doing anything: download, verify, then a terminal install the user watches."""
        self._update_release = release
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Update available")
        box.setText(f"wpt {release.version_text} is available (you have {updates_mod.__version__}).")
        detail = []
        if not updates_mod.is_arch_family():
            detail.append("This is not an Arch-family system, so there is no package to install "
                          "automatically - it will open the release page instead.")
        elif release.asset(package=True) is None:
            detail.append("This release publishes no Arch package, so it will open the release page "
                          "instead.")
        if release.published_at:
            detail.append(f"Published {release.published_at}.")
        detail.append(f"Notes: {release.html_url}")
        box.setInformativeText(" ".join(detail))

        now = box.addButton("Download and install", QMessageBox.ButtonRole.AcceptRole)
        later = box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        skip = box.addButton("Skip this version", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(now)
        box.exec()

        clicked = box.clickedButton()
        if clicked is skip:
            updates_mod.save_config({"skip_version": release.version_text})
            self.log(f"not mentioning {release.version_text} again "
                     f"(a manual 'Check for updates' still shows it)")
        elif clicked is later:
            self.log(f"update to {release.version_text} deferred")
        elif clicked is now:
            self.start_update_download(release)

    def start_update_download(self, release) -> None:
        """Fetch the package, verify it, then hand it to pacman in a visible terminal."""
        asset = release.asset(package=True)
        if not updates_mod.is_arch_family() or asset is None:
            QDesktopServices.openUrl(QUrl(release.html_url))
            self.log(f"opened {release.html_url} in your browser")
            return
        dest = Path(tempfile.mkdtemp(prefix="wpt-update-"))
        self.log(f"downloading {asset.name} ({asset.size / 1048576:.1f} MB)")
        self.lbl_update_state.setText("downloading...")

        def work(emit):
            emit(f"downloading {asset.name} ({asset.size / 1048576:.1f} MB)")
            path = updates_mod.download(
                asset, dest,
                progress=lambda done, total: emit(
                    f"  {done / 1048576:.1f} MB of {total / 1048576:.1f} MB" if total
                    else f"  {done / 1048576:.1f} MB"))
            updates_mod.validate_package(path)
            want = updates_mod.expected_sha256(release, asset, dest)
            got = updates_mod.sha256(path)
            if want and got != want:
                path.unlink(missing_ok=True)
                raise updates_mod.UpdateError(
                    f"checksum mismatch - the release lists {want[:16]}..., this download is "
                    f"{got[:16]}... Nothing was installed.")
            emit(f"verified sha256 {got[:16]}...")
            return path, got, bool(want)

        self._update_busy = True
        worker = Worker(work)
        self._update_worker = worker
        self._update_workers.append(worker)
        worker.finished.connect(lambda: self._forget_update_worker(worker))

        def done(result):
            self._update_busy = False
            path, digest, checked = result
            self._update_path = path
            self.lbl_update_state.setText("ready to install")
            self.log(f"verified {path.name} (sha256 {digest[:16]}..."
                     + (" matches the release)" if checked else ", the release publishes no checksum)"))
            self._confirm_install()

        def failed(message):
            self._update_busy = False
            self.lbl_update_state.setText("download failed")
            self.log(f"update download failed: {message}")
            QMessageBox.warning(self, "Update failed", message)

        worker.line.connect(self.log)
        worker.done.connect(done)
        worker.failed.connect(failed)
        worker.start()

    def _confirm_install(self) -> None:
        path = self._update_path
        version = getattr(self._update_release, "version_text", "the new version")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Install the update now?")
        box.setText(f"Install wpt {version}?")
        box.setInformativeText(
            "This window closes, a terminal opens and runs:\n\n"
            f"    sudo pacman -U {path}\n\n"
            "pacman asks for your password in that terminal, you watch it run, and the toolkit "
            "reopens when it finishes.")
        go = box.addButton("Close and install", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(go)
        box.exec()
        if box.clickedButton() is not go:
            self.log(f"downloaded to {path} - install it later with: sudo pacman -U {path}")
            return

        self._launch_update_install(path)

    def _launch_update_install(self, path: Path | None) -> None:
        """Do not restart the GUI while its standalone prefix guard is active."""
        if path is None:
            self.log("update install refused: no downloaded package path")
            return
        if self._refuse_prefix_write_for_standalone("update/restart", self.install_log):
            QMessageBox.warning(
                self,
                "Standalone still running",
                "Close the tracked standalone process group and any detached helper/licensing "
                "services before applying an update and restarting WPT.",
            )
            return
        launched, message = updates_mod.launch_install(path, restart=True)
        self.log(message if launched else f"update failed: {message}")
        if not launched:
            QMessageBox.warning(self, "Could not open a terminal", message)
            return
        self._wait_for_jobs()
        QTimer.singleShot(400, self.close)

    # ------------------------------------------------------------------ env
    def _restore_buttons(self, *buttons) -> None:
        """Re-enable buttons a caller disabled before handing work to _spawn()."""
        for button in buttons:
            if button is not None:
                button.setEnabled(True)

    def _spawn(self, fn, *args, on_line=None, on_done=None, on_failed=None, log=None, label="job",
               restore=None, restore_text=None) -> bool:
        """Run one job off the GUI thread, safely.

        Two rules here, both learned from a real crash in the field, a user disabled a
        plugin, hit refresh, and the window died:

        - **a running QThread must keep a Python reference.** The old code did
          `self.worker = Worker(...)`, so starting a second job dropped the last reference
          to the first one while it was still running, and Qt aborts the process
          (*QThread: Destroyed while thread is still running*, SIGABRT) rather than
          raising. Every worker is held in `self._workers` until it finishes;
        - **one job at a time** against a prefix. Two overlapping inventories or an
          uninstall racing a refresh is not worth supporting, so a second request is
          declined with a line in the log instead of queued.
        """
        busy = [w for w in self._workers if w.isRunning()]
        if busy:
            # The caller usually disabled its button before calling us. Declining without undoing
            # that leaves a dead button and a status line describing work that never started.
            if restore:
                self._restore_buttons(*restore)
            if restore_text:
                restore_text()
            message = f"{label}: still working on the previous one, try again in a moment"
            if log is not None:
                if callable(log):
                    log(message)
                else:                      # a QPlainTextEdit, as most tabs pass
                    log.appendPlainText(message)
            else:
                self.plugin_log.appendPlainText(message)
            return False
        worker = Worker(fn, *args)
        self._workers.append(worker)

        def _retire() -> None:
            # QThread.finished is emitted just before all native-thread cleanup is complete.
            # Join before dropping the last tracking reference; otherwise interpreter teardown
            # can destroy a thread that Qt still considers alive and abort with SIGABRT.
            worker.wait()
            if worker in self._workers:
                self._workers.remove(worker)
            # a close requested while this job was running waits for it, rather than
            # destroying a live QThread (Qt aborts the process for that)
            if (self._closing and not self._jobs_running()
                    and not self._update_job_running()):
                self.close()

        worker.finished.connect(_retire)
        if on_line is not None:
            worker.line.connect(on_line)
        if on_done is not None:
            worker.done.connect(on_done)
        if on_failed is not None:
            worker.failed.connect(on_failed)
        self.worker = worker
        worker.start()
        return True

    def _update_job_running(self) -> bool:
        """An update check or download is its own QThread, held separately from prefix jobs.

        Checked against the list rather than `_update_worker`: that attribute is replaced by the
        next check, and the reference it held may still belong to a running thread.
        """
        return any(worker.isRunning() for worker in self._update_workers)

    def _forget_update_worker(self, worker) -> None:
        """Drop an update worker only after Qt's native thread cleanup has joined."""
        worker.wait()
        try:
            self._update_workers.remove(worker)
        except ValueError:
            pass
        if self._closing and not self._jobs_running() and not self._update_job_running():
            self.close()

    def _jobs_running(self) -> bool:
        """True while any background job is alive. (Not `_busy`, which predates this and
        toggles the buttons.)"""
        return any(w.isRunning() for w in self._workers)

    def _active_standalone_launches(self) -> list[standalone_mod.StandaloneLaunch]:
        """Return Wine process groups still active, pruning fully-exited launch groups."""
        active = []
        for launch in self._standalone_launches:
            try:
                running = launch.process.poll() is None
            except OSError:
                # If process state cannot be determined, fail closed before a prefix write.
                running = True
            if not running and hasattr(os, "killpg"):
                try:
                    # Standalone Popen uses start_new_session=True, so its PID is the private
                    # process-group ID. Keep the prefix busy if a child remains after wine exits.
                    os.killpg(launch.pid, 0)
                    running = True
                except ProcessLookupError:
                    running = False
                except OSError:
                    # Permission/unknown errors are ambiguous; fail closed.
                    running = True
            if running:
                active.append(launch)
        self._standalone_launches = active
        return active

    def _refuse_prefix_write_for_standalone(self, operation: str, log) -> bool:
        """Block prefix mutations while a standalone launched here may still use its files."""
        active = self._active_standalone_launches()
        if not active:
            return False
        pids = ", ".join(str(launch.pid) for launch in active)
        message = (
            f"{operation}: close standalone Wine process group leader PID(s) {pids} before modifying "
            "this prefix, and independently close any daemonized/helper/licensing processes that "
            "may have outlived the group (WPT cannot track those)."
        )
        if callable(log):
            log(message)
        else:
            log.appendPlainText(message)
        return True

    def _wait_for_jobs(self) -> None:
        """Give any running job a moment to finish before the process goes away.

        A QThread that is still running when the interpreter tears down aborts the process
        (SIGABRT, no traceback), the same rule that made `closeEvent` wait. This is the safety
        net for every other way out (a quit from the menu, a session logout, a script that
        closes the window mid-refresh).
        """
        import time as _time

        # A vendor installer under Wine is allowed 2400 s, so a short cap here would tear the
        # process down with a live QThread - which Qt answers with SIGABRT and no traceback.
        # Wait as long as the longest job can legitimately take.
        deadline = _time.time() + 2500
        if self._jobs_running() or self._update_job_running():
            # Waiting silently was a trap: the window is hidden by then, so the only thing on
            # screen was nothing, for as long as forty minutes, with no way to stop it. The dialog
            # explains the wait and offers the explicit way out.
            self._finishing_dialog()
        while ((self._jobs_running() or self._update_job_running())
               and not self._forced and _time.time() < deadline):
            # pumping the loop is what keeps that dialog clickable while this waits
            QApplication.processEvents()
            _time.sleep(0.05)

    def _finishing_dialog(self) -> None:
        """One visible, honest way out while a job holds the prefix.

        Reached from `closeEvent` (the window is hidden and waiting) and from `_wait_for_jobs` (a
        quit from the menu, a session logout). Both used to wait in silence.
        """
        if self._closing_dialog is not None:
            return
        box = QDialog(self)
        box.setWindowTitle("Finishing the running job")
        box.setModal(False)
        layout = QVBoxLayout(box)
        label = QLabel(
            "A job is still running against the prefix, so the window closes itself when it "
            "finishes.\n\nA vendor installer under Wine can legitimately take many minutes, and "
            "stopping one part-way through a write is what damages an install."
        )
        label.setWordWrap(True)
        # A word-wrapped label has no useful width of its own to hint with, and the window came up
        # 60x60 on a real desktop as a result (the size hint was tiny and the window manager took
        # its own minimum). Give the text a floor to wrap against and the dialog a floor to open at,
        # then let it size itself from that.
        label.setMinimumWidth(_DIALOG_TEXT_WIDTH)
        layout.addWidget(label)
        row = QHBoxLayout()
        row.addStretch(1)
        force = QPushButton("Force quit anyway…")
        force.setToolTip(
            "Stop the job where it is and exit. The job may be part-way through writing to the\n"
            "prefix, so the product can be left half-installed or half-removed.\n"
            "Waiting is almost always the better choice."
        )
        force.clicked.connect(self._force_quit)
        row.addWidget(force)
        layout.addLayout(row)
        box.setMinimumWidth(_DIALOG_TEXT_WIDTH + 60)
        box.show()
        box.adjustSize()            # lay out now, not at whatever size the WM chose first
        self._closing_dialog = box

        # and it takes itself away the moment the prefix is free again, so a job that finishes
        # while the window merely waited does not leave this on screen
        timer = QTimer(box)
        timer.setInterval(250)

        def tick() -> None:
            if not self._jobs_running() and not self._update_job_running():
                timer.stop()
                self._closing_dialog = None
                box.accept()

        timer.timeout.connect(tick)
        timer.start()

    def _force_quit(self) -> None:
        """Stop the workers and leave, on the user's explicit say-so and nothing else.

        `os._exit` rather than `app.quit`: the whole reason the wait exists is that Qt aborts on
        teardown with a live QThread, which is exactly the abort this is choosing to take.
        """
        answer = QMessageBox.warning(
            self,
            "Force quit?",
            "The running job is stopped where it is.\n\n"
            "If it is part-way through writing to the prefix, the product can be left "
            "half-installed or half-removed - wait if you can.\n\nQuit anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._forced = True
        for worker in (*self._workers, self._update_worker):
            if worker is not None and worker.isRunning():
                worker.terminate()
                worker.wait(3000)
        os._exit(130)

    def _ask_keep_open_for_standalone(self, active_launches) -> bool:
        """Return true unless the user explicitly selects the leave-apps-running close action."""
        pids = ", ".join(str(launch.pid) for launch in active_launches)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Standalone still running")
        box.setText(
            f"WPT still tracks Wine process group leader PID(s) {pids}. Closing WPT does not signal "
            "these standalone processes. Prefix-write protection ends when this window closes, and a "
            "reopened or separate WPT window does not rediscover them. Do not use WPT to modify the "
            "prefix until these apps and any detached helpers or licensing services have closed."
        )
        keep_open = box.addButton("Keep WPT open", QMessageBox.ButtonRole.AcceptRole)
        close_anyway = box.addButton(
            "Close WPT; don't stop apps", QMessageBox.ButtonRole.DestructiveRole
        )
        box.setDefaultButton(keep_open)
        box.setEscapeButton(keep_open)
        box.exec()
        # Escape/titlebar dismissal is not explicit consent to drop the write guard.
        return box.clickedButton() is not close_anyway

    def closeEvent(self, event) -> None:  # noqa: D102 - Qt entry point
        """Never let the window close out from under a running job.

        A QThread that is still running when its last reference goes away makes Qt abort
        the process (SIGABRT, no exception, no traceback), which is how this started: a
        plugin was disabled, the window was refreshed, and it vanished. The window now stays alive
        (hidden) until the job finishes, then closes itself.
        """
        active_launches = self._active_standalone_launches() if not self._closing else []
        if active_launches:
            if self._ask_keep_open_for_standalone(active_launches):
                event.ignore()
                return
            self.plugin_log.appendPlainText(
                "closing WPT while detached standalone app(s) remain active; prefix-write protection ends"
            )
        self._closing = True
        for timer_name in ("_startup_catalogue_timer", "_watch_timer"):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                timer.stop()
        if not self._jobs_running() and not self._update_job_running():
            if self._closing_dialog is not None:
                # the window is really going now, and the dialog is its child: hand it back
                # explicitly so nothing is left pointing at a deleted widget
                self._closing_dialog.accept()
                self._closing_dialog = None
            super().closeEvent(event)
            return
        self.hide()
        try:
            self.plugin_log.appendPlainText("finishing the running job before closing…")
        except RuntimeError:            # widgets can already be gone during shutdown
            pass
        self._finishing_dialog()
        event.ignore()

    # ------------------------------------------------------------------ theme & settings
    def _screenshot_settle(self) -> None:
        """Last step of `WPT_SCREENSHOT_MODE`: empty the tables again and frame the image.

        A published image is of one tab and the window opens on Environment, so the tab is named
        rather than clicked: `WPT_SCREENSHOT_TAB=Download` (a label) or an index.
        """
        self.blank_machine_state()
        wanted = os.environ.get("WPT_SCREENSHOT_TAB", "")
        if wanted:
            labels = [self.tabs.tabText(i) for i in range(self.tabs.count())]
            self.tabs.setCurrentIndex(labels.index(wanted) if wanted in labels else int(wanted))
        self.resize(1200, 760)
        self.raise_()
        self.activateWindow()

    def blank_machine_state(self) -> None:
        """Empty the tables that describe *this* machine, for a screenshot that can be published.

        A render of a real prefix shows which plugins are installed here and which installers have
        been downloaded, and a README image must not. Used by `WPT_SCREENSHOT_MODE=1` (which also
        turns off everything that would refill them) and by `tests/render_tabs.py`.
        """
        self._watch_timer.stop()
        self._msi_names = set()
        self._installed_products = set()
        self._downloads = {}
        self._registered_names = lambda: set()
        self._is_installed = lambda release: ""
        self._downloads_for = lambda release: None
        self.download_table.setRowCount(0)
        self._fill_download_table()
        self.plugin_table.setRowCount(0)
        # emptying the table is a state change like any other: without this the plugin table stayed
        # visible with no rows while its note stayed hidden, which is the empty grid the note exists
        # to replace (visible in the rendered README images)
        _show_rows(self.plugin_table, self.plugin_empty, 0)
        self.plugin_summary.setText("No inventory yet: press 'Refresh inventory'.")

    def apply_theme(self, dark: bool | None = None, *, remember: bool = True) -> None:
        """Set the palette, and remember the choice in the same config.json the update check uses.

        `dark=None` reads the saved setting, which is what a cold start does: the toggle is
        restored before the window is shown rather than after, so there is no flash of the wrong
        theme.
        """
        if dark is None:
            dark = updates_mod.load_config().get("dark_theme") is True
        # the switch is a view of the setting, so it follows whichever way the value arrived;
        # blockSignals stops that from re-entering this function through toggled()
        if self.cb_dark.isChecked() != dark:
            self.cb_dark.blockSignals(True)
            self.cb_dark.setChecked(dark)
            self.cb_dark.blockSignals(False)
        app = QApplication.instance()
        if app is not None:
            app.setPalette(_dark_palette() if dark else self._system_palette)
        if dark is not None and remember:
            updates_mod.save_config({"dark_theme": bool(dark)})

    def open_settings(self) -> None:
        """Versions, resolved paths and the config file - what a bug report needs, in one place."""
        from . import __version__

        dialog = QDialog(self)
        dialog.setWindowTitle("Settings & about")
        layout = QVBoxLayout(dialog)

        head = QLabel(f"<b>Wine Plugin Toolkit {__version__}</b>")
        head.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(head)

        form = QFormLayout()
        resolved = self.env.describe() if self.env else {"prefix": "not detected - see Environment"}
        for key in ("python", "home", "wine_tree", "prefix", "vst3_dir", "vst2_dir", "aax_dir"):
            if key in resolved:
                value = QLabel(str(resolved[key]))
                value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                form.addRow(key.replace("_", " "), value)
        config = QLabel(str(updates_mod.config_path()))
        config.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("config file", config)
        layout.addLayout(form)

        check_updates = QCheckBox("Check for updates when the window opens")
        check_updates.setChecked(updates_mod.update_check_enabled())
        check_updates.setToolTip(
            "Saved in the config file above. `WPT_NO_UPDATE_CHECK=1` in the environment overrides it."
        )

        def remember(checked: bool) -> None:
            updates_mod.save_config({"update_check": bool(checked)})

        check_updates.toggled.connect(remember)
        layout.addWidget(check_updates)

        dark = QCheckBox("Dark mode (the header switch does the same thing)")
        dark.setChecked(self.cb_dark.isChecked())
        dark.toggled.connect(self.cb_dark.setChecked)
        layout.addWidget(dark)

        hint = QLabel(
            "Nothing here is required for the toolkit to work. The update check is the only thing "
            "that ever reaches the network, and it reads the public releases page."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        repo = QPushButton("Open the project page")
        repo.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(f"https://github.com/{updates_mod.REPO}")))
        buttons.addWidget(repo)
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        dialog.exec()

    def _env_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Launch profile:"))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(230)
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        profile_row.addWidget(self.profile_combo)
        self.btn_manage_profiles = QPushButton("Manage profiles…")
        self.btn_manage_profiles.clicked.connect(lambda *_args: self.manage_launch_profiles())
        profile_row.addWidget(self.btn_manage_profiles)
        profile_row.addStretch(1)
        layout.addLayout(profile_row)

        self.env_form = QFormLayout()
        box = QGroupBox("Selected stack")
        box.setLayout(self.env_form)
        layout.addWidget(box)

        self.env_note = QLabel("")
        self.env_note.setWordWrap(True)
        layout.addWidget(self.env_note)

        refresh = QPushButton("Re-detect")
        refresh.clicked.connect(self.refresh_env)
        layout.addWidget(refresh, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return page

    def refresh_env(self, _checked: bool = False) -> bool:
        reason = self._profile_switch_block_reason()
        if reason:
            self.env_note.setText(f"Re-detect is paused: {reason}")
            return False
        previous_env = self.env
        while self.env_form.rowCount():
            self.env_form.removeRow(0)
        try:
            self.profile_store = launch_profiles_mod.load_store()
            self.profile_load_error = None
            self._populate_profile_combo()
        except ProfileConfigError as exc:
            self.profile_store = ProfileStore()
            self.profile_load_error = str(exc)
            self._populate_profile_combo()
            self.env = None
            self._invalidate_profile_views("Launch profile configuration is invalid; fix it before using WPT.")
            self.env_note.setText(f"Launch profile config error: {exc}")
            return False
        try:
            profile = self._selected_profile()
            self.env = profile.resolve() if profile else detect()
        except (EnvironmentError_, ProfileConfigError) as exc:
            self.env = None
            self._invalidate_profile_views("Wine environment unavailable; choose a valid launch profile.")
            self.env_note.setText(f"Not detected: {exc}")
            return False
        for key, value in self.env.describe().items():
            self.env_form.addRow(key.replace("_", " "), QLabel(value))
        missing = msi_mod.missing_tools()
        status = (
            "msitools missing: " + ", ".join(missing) + "  ->  sudo pacman -S msitools"
            if missing
            else "msitools present. Wine tree, prefix and plugin directories all resolved."
        )
        changed = previous_env is not None and (
            previous_env.prefix != self.env.prefix or previous_env.wine_tree != self.env.wine_tree
        )
        if changed:
            message = "Wine prefix or tree changed; refresh the prefix-derived lists before acting."
            self._invalidate_profile_views(message)
            status += " " + message
        self.env_note.setText(status)
        return True

    def _populate_profile_combo(self) -> None:
        was_blocked = self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("Auto-detected", None)
        for profile in self.profile_store.profiles:
            self.profile_combo.addItem(profile.name, profile.name)
        selected = self.profile_combo.findData(self.profile_store.active)
        self.profile_combo.setCurrentIndex(selected if selected >= 0 else 0)
        self.profile_combo.blockSignals(was_blocked)

    def _selected_profile(self):
        name = self.profile_combo.currentData()
        return next((profile for profile in self.profile_store.profiles if profile.name == name), None)

    def _profile_switch_block_reason(self) -> str | None:
        if self._workers or self._update_workers:
            return "Wait for WPT background work to finish before switching launch profiles."
        if self._active_standalone_launches():
            return "Close standalone Wine process groups before switching the active prefix."
        return None

    def _restore_profile_selection(self) -> None:
        was_blocked = self.profile_combo.blockSignals(True)
        selected = self.profile_combo.findData(self.profile_store.active)
        self.profile_combo.setCurrentIndex(selected if selected >= 0 else 0)
        self.profile_combo.blockSignals(was_blocked)

    def _invalidate_inventory(self, message: str) -> None:
        self.inv = None
        if not hasattr(self, "plugin_table"):
            return
        self.plugin_table.clearSelection()
        self.plugin_table.setRowCount(0)
        _show_rows(self.plugin_table, self.plugin_empty, 0)
        self.plugin_summary.setText(message)
        for name in ("btn_repair", "btn_disable", "btn_enable", "btn_uninstall"):
            button = getattr(self, name, None)
            if button is not None:
                button.setEnabled(False)

    def _invalidate_profile_views(self, message: str) -> None:
        """Discard cached data derived from a prefix or Wine runtime that is no longer active."""
        self._invalidate_inventory(message)
        self.plan = None
        if not hasattr(self, "msi_combo"):
            return
        self.msi_combo.clear()
        if hasattr(self, "install_table"):
            self.install_table.setRowCount(0)
            _show_rows(self.install_table, self.install_empty, 0)
            self.install_log.setPlainText(message)
        if hasattr(self, "pending_table"):
            self.pending_table.clearSelection()
            self.pending_table.setRowCount(0)
            _show_rows(self.pending_table, self.pending_empty, 0)
            self.pending_summary.setText(message + " Press 'Find downloaded installers' to rescan.")
            self.pending_log.setPlainText(message)
            self.btn_install_pending.setEnabled(False)
        if hasattr(self, "scan_table"):
            self.scan_table.setRowCount(0)
            _show_rows(self.scan_table, self.scan_empty, 0)
            self.scan_summary.setText(message + " Rerun scan or triage to refresh.")
            self.scan_products.setPlainText(message)
        if hasattr(self, "download_table"):
            self._msi_names = set()
            self._installed_products = set()
            self._downloads = {}
            self._downloads_scan_running = False
            self._seen_downloads = self._download_candidates()
            self.download_table.clearSelection()
            self.download_table.setRowCount(0)
            _show_rows(self.download_table, self.download_empty, 0)
            self.download_summary.setText(message + " Press 'Refresh catalogue' to recompute installed and local-installer status.")
            self.download_log.setPlainText(message)

    def _profile_changed(self, _index: int = -1) -> None:
        if self.profile_load_error:
            self._restore_profile_selection()
            self.env_note.setText(f"Launch profile config error: {self.profile_load_error}")
            return
        selected = self.profile_combo.currentData()
        if selected == self.profile_store.active:
            return
        reason = self._profile_switch_block_reason()
        if reason:
            self._restore_profile_selection()
            self.env_note.setText(reason)
            return
        try:
            store = ProfileStore(
                self.profile_store.profiles,
                active=selected,
                source_digest=self.profile_store.source_digest,
            )
            store = launch_profiles_mod.save_store(store)
        except ProfileConfigError as exc:
            self._restore_profile_selection()
            self.env_note.setText(f"Could not save launch profile selection: {exc}")
            return
        self.profile_store = store
        self.refresh_env()

    def manage_launch_profiles(self) -> None:
        if self.profile_load_error:
            self.env_note.setText(
                f"Cannot edit profiles until the config is repaired: {self.profile_load_error}"
            )
            return
        reason = self._profile_switch_block_reason()
        if reason:
            self.env_note.setText(reason)
            return
        previous_active = next(
            (profile for profile in self.profile_store.profiles if profile.name == self.profile_store.active),
            None,
        )
        previous_active_id = previous_active.profile_id if previous_active else None
        dialog = LaunchProfilesDialog(self.profile_store.profiles, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        reason = self._profile_switch_block_reason()
        if reason:
            self.env_note.setText(
                f"Profile changes were not saved because WPT activity started while the manager was open: {reason}"
            )
            return
        active_profile = next(
            (profile for profile in dialog.profiles if profile.profile_id == previous_active_id),
            None,
        )
        active = active_profile.name if active_profile else None
        try:
            store = ProfileStore(
                dialog.profiles,
                active=active,
                source_digest=self.profile_store.source_digest,
            )
            store = launch_profiles_mod.save_store(store)
        except ProfileConfigError as exc:
            self.env_note.setText(f"Could not save launch profiles: {exc}")
            return
        self.profile_store = store
        self._populate_profile_combo()
        current_active = next(
            (profile for profile in store.profiles if profile.name == store.active), None
        )
        active_profile_removed = previous_active_id is not None and active_profile is None
        if previous_active != current_active:
            self.refresh_env()
            if active_profile_removed:
                self.env_note.setText(
                    self.env_note.text().rstrip()
                    + " The active profile was removed; WPT reverted to Auto-detected. "
                    "Confirm the displayed prefix before continuing."
                )

    # -------------------------------------------------------------- plugins
    def _plugins_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        bar = QHBoxLayout()
        refresh = QPushButton("Refresh inventory")
        refresh.clicked.connect(self.refresh_plugins)
        bar.addWidget(refresh)
        self.btn_repair = QPushButton("Repair selected from cached MSI")
        self.btn_repair.setEnabled(False)
        self.btn_repair.clicked.connect(self.repair_selected)
        bar.addWidget(self.btn_repair)
        self.btn_disable = QPushButton("Disable (hide from DAW)")
        self.btn_disable.setEnabled(False)
        self.btn_disable.clicked.connect(lambda: self.toggle_selected(enabled=False))
        bar.addWidget(self.btn_disable)
        self.btn_enable = QPushButton("Enable")
        self.btn_enable.setEnabled(False)
        self.btn_enable.clicked.connect(lambda: self.toggle_selected(enabled=True))
        bar.addWidget(self.btn_enable)
        self.btn_uninstall = QPushButton("Uninstall…")
        self.btn_uninstall.setEnabled(False)
        self.btn_uninstall.clicked.connect(self.uninstall_selected)
        bar.addWidget(self.btn_uninstall)
        bar.addStretch(1)
        layout.addLayout(bar)

        # The two checkboxes go on their own row: seven controls on one line overflowed a 900 px
        # window and Qt answered by eliding the last button's text to "Uninstall …", which reads
        # like a different (and destructive-looking) control. Verified in the 900x600 render.
        bar2 = QHBoxLayout()
        self.cb_purge = QCheckBox("purge registry")
        self.cb_purge.setToolTip(
            "After removing the files, also delete the registry entries that point at them\n"
            "(and the product's own Uninstall entry). Leaves nothing pointing at the files,\n"
            "so 'wpt scan' stops calling it a broken install. It cannot free an activation:\n"
            "deactivate first, or use 'Report as Unusable' in iLok License Manager."
        )
        bar2.addWidget(self.cb_purge)
        self.cb_all_exes = QCheckBox("show other .exe files")
        self.cb_all_exes.setToolTip(
            "Also list executables in Program Files that no plugin MSI describes. Wine's own\n"
            "tools (iexplore, wordpad, wmplayer) and helpers other vendors install there.\n"
            "They are not plugins, so they are hidden by default."
        )
        self.cb_all_exes.stateChanged.connect(lambda _state: self.plugins_done(self.inv) if self.inv else None)
        bar2.addWidget(self.cb_all_exes)
        bar2.addStretch(1)
        layout.addLayout(bar2)

        self.plugin_hint = QLabel(
            "Tick a row to select it, then use the buttons above, or <b>right-click a row</b> for repair, "
            "uninstall, enable/disable, a matching standalone app, local files and preset sites."
        )
        self.plugin_hint.setWordWrap(True)
        self.plugin_hint.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.plugin_hint)

        self.plugin_summary = QLabel("Not scanned yet - press 'Refresh inventory'.")
        self.plugin_summary.setWordWrap(True)
        layout.addWidget(self.plugin_summary)

        self.plugin_table = QTableWidget(0, 6)
        self.plugin_table.setHorizontalHeaderLabels(
            ["Integrity", "State", "Kind", "Plugin file", "On disk", "MSI says"]
        )
        _fit_columns(self.plugin_table, stretch={3: 260}, contents=(0, 1, 2, 4, 5), elide={3: True})
        self.plugin_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.plugin_table.itemSelectionChanged.connect(self._plugin_selection_changed)
        self.plugin_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.plugin_table.customContextMenuRequested.connect(self._plugin_context_menu)
        layout.addWidget(self.plugin_table, 2)
        self.plugin_empty = _empty_note(
            "No inventory yet: press 'Refresh inventory' to list every plugin file in the prefix.\n"
            "Then check each one's size against its cache MSI."
        )
        layout.addWidget(self.plugin_empty, 2)
        _show_rows(self.plugin_table, self.plugin_empty, 0)

        self.plugin_log = _log_pane("Job output appears here: inventory, repairs, uninstalls and rescans from this tab.", 1000)
        layout.addWidget(self.plugin_log, 1)
        return page

    def refresh_plugins(self) -> None:
        env = self.env
        if env is None:
            QMessageBox.warning(self, "No environment", "Wine prefix not detected.")
            return
        previous_summary = self.plugin_summary.text()
        self.plugin_summary.setText("Reading the prefix…")
        self._spawn(
            lambda emit: inventory_mod.build(env),
            on_done=self.plugins_done,
            on_failed=lambda msg: self.plugin_summary.setText(f"Inventory failed: {msg}"),
            log=self.plugin_log,
            label="inventory",
            restore_text=lambda: self.plugin_summary.setText(previous_summary),
        )

    def plugins_done(self, inv) -> None:
        self.inv = inv
        self.plugin_table.setRowCount(0)
        show_all = getattr(self, "cb_all_exes", None) is not None and self.cb_all_exes.isChecked()
        rows_source = inv.entries if show_all else inv.plugins
        _show_rows(self.plugin_table, self.plugin_empty, len(rows_source))
        for entry in rows_source:
            row = self.plugin_table.rowCount()
            self.plugin_table.insertRow(row)
            integrity = {"ok": "ok", "unverified": "unverified", "size-mismatch": "BROKEN"}[entry.integrity]
            cells = [
                integrity,
                "disabled" if entry.disabled else "enabled",
                entry.kind,
                entry.name,
                f"{entry.size / 1e6:.1f} MB",
                f"{entry.expected_size / 1e6:.1f} MB" if entry.expected_size else "-",
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, entry)
                self.plugin_table.setItem(row, column, item)

        counts = {kind: len(inv.by_kind(kind)) for kind in ("vst3", "vst2", "aax", "standalone")}
        off = len(inv.disabled)
        hidden = len(inv.unowned_standalone)
        self.plugin_summary.setText(
            f"{len(inv.entries)} plugin file(s): "
            + ", ".join(f"{kind} {count}" for kind, count in counts.items())
            + f"   ·   {len(inv.msis)} cached MSI(s)   ·   {len(inv.broken)} broken"
            + (f"   ·   {off} disabled" if off else "")
            + (f"   ·   {hidden} other .exe hidden" if hidden and not show_all else "")
        )
        for warning in inv.warnings:
            self.plugin_log.appendPlainText(f"! {warning}")

    def _plugin_context_menu(self, position) -> None:
        """Right-click a plugin row to build and show its actions."""
        row = self.plugin_table.rowAt(position.y())
        if row < 0:
            return
        self.plugin_table.selectRow(row)
        cell = self.plugin_table.item(row, 0)
        entry = cell.data(Qt.ItemDataRole.UserRole) if cell else None
        if entry is None:
            return

        menu = self._build_plugin_menu(entry)
        try:
            menu.exec(self.plugin_table.viewport().mapToGlobal(position))
        finally:
            menu.deleteLater()

    def _build_plugin_menu(self, entry) -> QMenu:
        """Build one plugin's menu separately so its actions can be tested without a modal exec."""
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        if entry.msi is not None:
            repair = menu.addAction("Repair this plugin from its cached MSI")
            repair.triggered.connect(self.repair_selected)
            uninstall = menu.addAction("Uninstall the whole product…")
            uninstall.triggered.connect(self.uninstall_selected)
        else:
            why = menu.addAction("No MSI found for this file, so repair/uninstall are unavailable")
            why.setEnabled(False)

        menu.addSeparator()
        if entry.disabled:
            enable = menu.addAction("Enable (make it visible to the DAW again)")
            enable.triggered.connect(lambda *_args: self.toggle_selected(enabled=True))
        else:
            disable = menu.addAction("Disable (hide it from the DAW scanner)")
            disable.triggered.connect(lambda *_args: self.toggle_selected(enabled=False))

        menu.addSeparator()
        if self.env is not None and self.inv is not None:
            resolution = standalone_mod.resolve_standalone(entry, self.inv, self.env)
        else:
            resolution = standalone_mod.StandaloneResolution(None, "Wine environment or inventory is unavailable.")
        run_label = "Run in Standalone"
        if resolution.executable is not None and not resolution.verified:
            run_label += " (unverified)"
        run = menu.addAction(run_label)
        active_launches = self._active_standalone_launches()
        if resolution.executable is None:
            run.setEnabled(False)
            run.setToolTip(resolution.reason)
        else:
            wine_binary = self.env.wine_binary if self.env is not None else "the selected Wine runtime"
            if resolution.verified:
                verification = (
                    "MSI ownership and file size were verified at the last inventory scan; "
                    "a size mismatch at launch refuses the run until inventory is refreshed."
                )
            else:
                verification = "MSI ownership and file integrity are unverified."
            active_note = ""
            if active_launches:
                pids = ", ".join(str(launch.pid) for launch in active_launches)
                active_note = (
                    f" {len(active_launches)} WPT-launched standalone group(s) are already active "
                    f"(leader PID(s) {pids}); this launches another using this window's active profile."
                )
            run.setToolTip(
                f"Launch {resolution.executable.name} with {wine_binary}. {verification}{active_note} "
                "The app starts in a separate process session; closing this WPT window does not signal it. "
                "This window tracks launched groups and blocks prefix writes and profile changes while they "
                "are active. A reopened or separate WPT window will not rediscover them. Close the app and "
                "any helpers or licensing services before modifying the prefix."
            )
            run.triggered.connect(
                lambda _checked=False, selected=entry: self._launch_standalone(selected)
            )

        menu.addSeparator()
        copy_name = menu.addAction("Copy plugin name")
        copy_name.triggered.connect(lambda: self._copy_to_clipboard(entry.name))
        copy_path = menu.addAction("Copy file path")
        copy_path.triggered.connect(lambda: self._copy_to_clipboard(str(entry.path)))
        if entry.msi is not None:
            copy_msi = menu.addAction(f"Copy the MSI it came from ({entry.msi.name})")
            copy_msi.triggered.connect(lambda: self._copy_to_clipboard(str(entry.msi)))
        reveal = menu.addAction("Browse local files")
        reveal.triggered.connect(lambda: self._reveal(entry.path))

        menu.addSeparator()
        presets_menu = menu.addMenu("Preset & IR sources for this plugin")
        product = entry.name
        for suffix in (".disabled", ".aaxplugin", ".vst3", ".dll", ".exe"):
            if product.lower().endswith(suffix):
                product = product[: -len(suffix)]
        for source in sources_mod.SOURCES:
            label = source.name + ("  (search this plugin)" if source.searchable else "")
            action = presets_menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, name=source.name, prod=product: self._open_source_for(name, prod)
            )
        return menu

    def _reveal(self, path: Path) -> None:
        """Open the plugin's containing directory in the desktop file manager."""
        folder = path if path.is_dir() else path.parent
        opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        self.plugin_log.appendPlainText(
            f"opened {folder} in the file manager" if opened else f"could not open a file manager for {folder}"
        )

    def _launch_standalone(self, plugin) -> None:
        """Re-resolve and launch the matching app through the selected custom Wine environment."""
        if not self.env or not self.inv:
            self.plugin_log.appendPlainText("could not start standalone: Wine environment or inventory is unavailable")
            return
        if self._jobs_running():
            self.plugin_log.appendPlainText("could not start standalone: a prefix job is still running")
            return
        resolution = standalone_mod.resolve_standalone(plugin, self.inv, self.env)
        if resolution.executable is None:
            self.plugin_log.appendPlainText(f"could not start standalone: {resolution.reason}")
            return
        try:
            launch = standalone_mod.launch_standalone(
                self.env, resolution.executable, expected_size=resolution.expected_size
            )
        except (OSError, ValueError) as exc:
            self.plugin_log.appendPlainText(f"could not start {resolution.executable.name}: {exc}")
            return
        self._standalone_launches.append(launch)
        active_count = len(self._active_standalone_launches())
        if resolution.verified:
            ownership = "MSI ownership verified at last scan; file size matches at launch"
        else:
            ownership = "MSI ownership/integrity unverified"
        self.plugin_log.appendPlainText(
            f"started {resolution.executable.name} with {self.env.wine_binary} in this prefix "
            f"(detached PID {launch.pid}; {active_count} standalone group(s) tracked; {ownership}); "
            f"output: {launch.log_path}"
        )

    def _selected_entry(self):
        rows = self.plugin_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.plugin_table.item(rows[0].row(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _plugin_selection_changed(self) -> None:
        entry = self._selected_entry()
        self.btn_repair.setEnabled(bool(entry and entry.msi))
        self.btn_disable.setEnabled(bool(entry and not entry.disabled))
        self.btn_enable.setEnabled(bool(entry and entry.disabled))
        self.btn_uninstall.setEnabled(bool(entry and entry.msi))

    def toggle_selected(self, enabled: bool) -> None:
        """Rename the selected plugin so the DAW scanner does or does not see it."""
        entry = self._selected_entry()
        if not entry or not self.env:
            return
        if self._refuse_prefix_write_for_standalone("enable/disable", self.plugin_log):
            return
        if self._jobs_running():
            # The rename is a write inside the prefix, and an install or uninstall job is writing
            # there too - the uninstall path deletes both `<name>` and `<name>.disabled`, so this
            # races it over the same files. Same rule as _spawn: decline rather than race.
            self.plugin_log.appendPlainText(
                f"{'enable' if enabled else 'disable'}: still working on a job against the prefix, "
                "try again in a moment")
            return
        # the core matches on a substring of the file name; the base name (no
        # '.disabled') matches the file whichever state it is in
        name = entry.name
        if name.lower().endswith(DISABLED_SUFFIX):
            name = name[: -len(DISABLED_SUFFIX)]
        rows = set_plugin_enabled(self.env, name, enabled=enabled)
        for status, path, note in rows:
            self.plugin_log.appendPlainText(f"{status}: {path} {note}")
        if not rows:
            self.plugin_log.appendPlainText(f"nothing matched '{name}'")
        self.refresh_plugins()

    def uninstall_selected(self) -> None:
        """Remove the whole product: clear the msiexec registration and delete the MSI's files.

        The two reads that build the confirmation text (`msi.identity` and `scan.is_registered`) used
        to run *here*, on the GUI thread, before the dialog opened - the only MSI reads left in the
        window that could freeze it, and one of the two accepted limitations from the review round.
        They now run on a worker like every other read, and the dialog opens from the result: the
        same wording, no blocked event loop.
        """
        entry = self._selected_entry()
        env = self.env
        if not entry or not entry.msi or env is None:
            return
        if self._refuse_prefix_write_for_standalone("uninstall", self.plugin_log):
            return
        msi_path = entry.msi

        def check(emit, path):
            emit(f"reading {Path(path).name} to describe what uninstalling it does …")
            ident = msi_mod.identity(path)
            registered = bool(ident.product_code) and scan_mod.is_registered(env, ident.product_code)
            return ident, registered

        self.btn_uninstall.setEnabled(False)
        self._spawn(
            check, msi_path,
            on_line=self.plugin_log.appendPlainText,
            on_done=lambda pair: self._confirm_uninstall(msi_path, pair[0], pair[1], env),
            # Without this the worker's exception went nowhere: `failed` had no receiver, so an
            # unreadable MSI left the button disabled with nothing in the log to say why. Every
            # other action passes an on_failed; this one was the exception.
            on_failed=lambda msg: (
                self.plugin_log.appendPlainText(f"cannot read that MSI for uninstall: {msg}"),
                self.btn_uninstall.setEnabled(True),
            ),
            log=self.plugin_log,
            label="uninstall check",
            restore=(self.btn_uninstall,),
        )

    def _confirm_uninstall(self, msi_path: Path, ident, registered: bool, env=None) -> None:
        """The dialog, then the job. Runs on the GUI thread but reads nothing from disk."""
        # the pre-check worker disabled this button; every path that does not go on to run the job
        # has to hand it back, or a warning or a cancelled dialog leaves a dead button behind
        self._restore_buttons(self.btn_uninstall)
        env = env if env is not None else self.env
        if env is None:
            self.plugin_log.appendPlainText("uninstall stopped: no Wine environment is configured")
            return
        label = ident.label
        if not ident.product_code:
            QMessageBox.warning(self, "No ProductCode", f"{msi_path.name} has no ProductCode to uninstall.")
            return
        purge = self.cb_purge.isChecked()
        answer = QMessageBox.question(
            self,
            "Uninstall product",
            f"Remove {label} completely?\n\n"
            + (
                f"Registered with Windows Installer: msiexec /x {ident.product_code} may run unless a safety gate refuses it.\n"
                if registered
                else "Not registered with Windows Installer in this prefix, so msiexec has nothing "
                "to remove and the files go directly.\n"
            )
            + f"File-table paths from {msi_path.name} are checked before removal; files another "
            "cached MSI claims, or table-only files whose contents cannot be verified, are "
            "preserved. A registered uninstall may be refused when either condition applies.\n\n"
            "The toolkit first attempts to rescue recognised presets to "
            "~/.local/share/wpt/presets/. A failed copy stops the job. Wine's msiexec /x may "
            "affect host paths mapped into the prefix and remove unrecognised personal files. "
            "Back up your own data before continuing; this rescue is not a guarantee.\n\n"
            "Deleting plugin files does not return an iLok activation; deactivate it separately."
            + (
                "\n\nREGISTRY PURGE is on and will remove matching product entries pointing at "
                "planned files. Other registry entries may remain."
                if purge
                else ""
            ),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._refuse_prefix_write_for_standalone("uninstall", self.plugin_log):
            return
        self.btn_uninstall.setEnabled(False)
        product_code = ident.product_code

        def job(emit, msi_path, product_code, purge):
            # READ FIRST, MSIEXEC SECOND. `msiexec /x` deletes Windows Installer's own cached
            # copy of the package, and that cache is often the only copy these products have, so
            # reading the file list afterwards fails *after* the registration is gone (observed on a
            # Fortin Cali Suite uninstall). The MSI is staged out of reach, and an unreadable
            # MSI stops here, before anything is touched.
            try:
                msi_path = msi_mod.stage_msi(msi_path, SCRATCH, require_cabinets=False)
                emit(f"reading {msi_path.name} before msiexec runs")
                try:
                    msi_mod.extract(msi_path, SCRATCH)
                except msi_mod.MsiError as exc:
                    # Extracting needs the cabinet. Wine's cache keeps only the MSI, and a vendor
                    # bootstrapper deletes the payload it unpacked, so an installed product can
                    # end up with its media gone - and then this refused outright, leaving a
                    # working plugin nobody could remove. The MSI's tables still name every file
                    # it placed, and a removal only acts on those destinations.
                    plan = build_plan_from_tables(msi_path, env, include_aax=True)
                    emit(f"! {msi_path.name}: its payload media is gone")
                    emit(f"!   {exc}")
                    emit("!   File-table paths are known, but installed bytes cannot be verified;")
                    emit("!   matching files will be preserved for manual review.")
                else:
                    plan = build_plan(msi_path, env, SCRATCH, include_aax=True)
            except (OSError, msi_mod.MsiError) as exc:
                raise RuntimeError(
                    f"cannot read {msi_path.name} for its file list ({exc}), nothing was removed"
                ) from exc
            emit(f"this MSI describes {len(plan.actions)} destination(s)")

            uninstall_env = env
            try:
                installer_mod.validate_removal_paths(plan, uninstall_env)
            except (OSError, msi_mod.MsiError) as exc:
                raise RuntimeError(
                    "uninstall refused: unsafe or ambiguous planned path "
                    f"({exc}); no preset rescue, msiexec, direct deletion, or registry purge was run"
                ) from exc
            registered_now = bool(product_code) and scan_mod.is_registered(uninstall_env, product_code)
            if registered_now != registered:
                raise RuntimeError(
                    "registration state changed since confirmation; uninstall stopped without changes"
                )
            if registered_now and plan.destinations_only:
                raise RuntimeError(
                    "uninstall refused: payload bytes are unavailable, so file contents cannot be "
                    "verified. No preset rescue, msiexec, direct deletion, or registry purge was run; "
                    "restore the payload media first."
                )
            unmapped_roots = installer_mod.unmapped_destination_warnings(plan)
            if registered_now and unmapped_roots:
                details = "; ".join(unmapped_roots)
                raise RuntimeError(
                    "uninstall refused: this MSI has file roots with no safe destination; Wine's "
                    "vendor uninstaller may remove those unmapped files. No preset rescue, msiexec, "
                    f"direct deletion, or registry purge was run. {details}"
                )
            warnings_before = len(plan.warnings)
            shared_paths = inventory_mod.mark_cross_product_claims(plan, uninstall_env)
            for warning in plan.warnings[warnings_before:]:
                emit(f"! ownership warning: {warning}")
            if registered_now and shared_paths:
                raise RuntimeError(
                    "uninstall refused: one or more planned files are also claimed by another "
                    "cached MSI. No preset rescue, msiexec, direct deletion, or registry purge was run."
                )

            # Rescue before msiexec: the vendor's removal can delete user files itself.
            # Derive product names from the plan and vendor tree, not only ProductName.
            saved, saved_for, looked_at = presets_mod.rescue_for_plan(uninstall_env, plan)
            failed_rescue = [row for row in saved if row[0] == "failed"]
            if failed_rescue:
                details = "; ".join(f"{source}: {note}" for _status, source, note in failed_rescue)
                raise RuntimeError(f"preset rescue failed before msiexec; uninstall refused: {details}")
            if saved:
                for product, destination in saved_for:
                    emit(f"presets: {product} -> {destination}")
                emit(f"presets: {len(saved)} file(s) copied before msiexec")
            else:
                emit("! presets: nothing found to rescue; planned directories may hold user files")
                emit("!   looked at: "
                     + (", ".join(looked_at) if looked_at else "no product folder in this prefix"))
                emit("!   back up any user files in the planned destinations before removing them")

            if registered_now:
                emit(f"msiexec /x {product_code}")
                code, detail = uninstall_product(uninstall_env, product_code)
                if detail:
                    emit(detail)
                status = installer_mod.uninstall_status_label(code, detail)
                emit(status)
                if code != 0:
                    raise RuntimeError(
                        f"{status}; direct file removal and registry purge were skipped"
                    )
                try:
                    still_registered = scan_mod.is_registered(uninstall_env, product_code)
                except OSError as exc:
                    raise RuntimeError(
                        "could not verify registration after msiexec; direct file removal and "
                        f"registry purge were skipped ({exc})"
                    ) from exc
                if still_registered:
                    raise RuntimeError(
                        "msiexec exited 0 but the product is still registered; direct file removal "
                        "and registry purge were skipped"
                    )
            else:
                emit("product is not registered; skipping msiexec /x")

            rows = installer_mod.remove_files(plan, uninstall_env)
            remaining = installer_mod.leftovers(plan, uninstall_env)
            purge_rows = []
            removal_errors = [row for row in rows if row[0] in ("failed", "refused")]
            if purge and (removal_errors or remaining):
                emit("! purge skipped: file removal was refused or left MSI-owned files behind")
            if purge and not removal_errors and not remaining:
                edits = installer_mod.stale_registry_edits(uninstall_env, plan, product_code)
                emit(f"purge: {len(edits)} registry entr{'y' if len(edits) == 1 else 'ies'} to remove")
                for edit in edits:
                    emit(f"    {edit.hive}\\{edit.key}" + (f"  [{edit.value}]" if edit.value else ""))
                    emit(f"        {edit.reason}")
                purge_rows = installer_mod.purge_registry(uninstall_env, edits) if edits else []
            return rows, remaining, purge_rows

        self._spawn(
            job, msi_path, product_code, purge,
            on_line=self.plugin_log.appendPlainText,
            on_done=self.uninstall_done,
            on_failed=lambda msg: (
                self.plugin_log.appendPlainText(f"uninstall failed: {msg}"),
                self.btn_uninstall.setEnabled(True),
                self.refresh_plugins(),      # whatever happened, the table must rescan
            ),
            log=self.plugin_log,
            label="uninstall",
            restore=(self.btn_uninstall,),
        )

    def uninstall_done(self, result) -> None:
        rows, remaining, purge_rows = result
        for status, path, note in rows:
            self.plugin_log.appendPlainText(f"{status}: {path} {note}".strip())
        removal_errors = [row for row in rows if row[0] in ("failed", "refused")]
        if removal_errors:
            self.plugin_log.appendPlainText(
                f"! uninstall incomplete: {len(removal_errors)} file removal(s) failed or were refused")
        if not rows:
            self.plugin_log.appendPlainText("nothing to remove: no file this MSI describes is on disk")
        if remaining:
            self.plugin_log.appendPlainText(f"! {len(remaining)} path(s) still on disk:")
            for path in remaining:
                self.plugin_log.appendPlainText(f"    {path}")
        elif not removal_errors:
            self.plugin_log.appendPlainText(
                "no MSI-owned file remains on disk; unowned files were preserved")
        if purge_rows:
            for status, target, note in purge_rows:
                self.plugin_log.appendPlainText(f"{status}: {target} {note}".strip())
            self.plugin_log.appendPlainText(
                f"{len(purge_rows)} registry entr{'y' if len(purge_rows) == 1 else 'ies'} removed"
            )
        else:
            self.plugin_log.appendPlainText(
                "the vendor's registry keys are not touched - the Diagnostics tab shows what it still claims"
            )
        self.btn_uninstall.setEnabled(True)
        self.refresh_plugins()

    def repair_selected(self) -> None:
        entry = self._selected_entry()
        env = self.env
        if not entry or not entry.msi or env is None:
            return
        if self._refuse_prefix_write_for_standalone("repair", self.plugin_log):
            return
        self.btn_repair.setEnabled(False)
        msi_path = entry.msi

        def job(emit, msi_path):
            emit(f"extracting {msi_path.name}")
            msi_mod.extract(msi_path, SCRATCH)
            plan = build_plan(msi_path, env, SCRATCH)
            todo = filter_needing_repair(plan, env)
            if not todo.actions:
                return 0, 0, []
            rows = apply_plan(todo, env, dry_run=False)
            bad = [c for c in verify_plan(todo, env) if c[0] != "ok"]
            restored = sum(status == "copied" for status, _path, _note in rows)
            return restored, len(bad), rows

        self._spawn(
            job, msi_path,
            on_line=self.plugin_log.appendPlainText,
            on_done=self.repair_done,
            on_failed=lambda msg: (
                self.plugin_log.appendPlainText(f"repair failed: {msg}"),
                self.btn_repair.setEnabled(True),
                self.refresh_plugins(),
            ),
            log=self.plugin_log,
            label="repair",
            restore=(self.btn_repair,),
        )

    def repair_done(self, result) -> None:
        copied, bad, rows = result
        if not rows:
            self.plugin_log.appendPlainText("nothing to repair: every tracked file is present at the right size")
        else:
            for status, path, note in rows:
                self.plugin_log.appendPlainText(f"{status}: {path} {note}")
            self.plugin_log.appendPlainText(f"{copied} file(s) restored, {bad} still failing verification")
        self.btn_repair.setEnabled(True)
        self.refresh_plugins()

    # -------------------------------------------------------------- install
    def _install_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        picker = QHBoxLayout()
        self.msi_combo = QComboBox()
        self.msi_combo.setMinimumWidth(560)
        picker.addWidget(QLabel("Installer MSI:"))
        picker.addWidget(self.msi_combo, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse_msi)
        picker.addWidget(browse)
        reload_btn = QPushButton("Find in prefix")
        reload_btn.clicked.connect(self.find_msis)
        picker.addWidget(reload_btn)
        layout.addLayout(picker)

        options = QGroupBox("What to install")
        opts = QHBoxLayout(options)
        self.cb_vst3 = QCheckBox("VST3")
        self.cb_vst3.setChecked(True)
        self.cb_vst3.setEnabled(False)
        self.cb_vst2 = QCheckBox("VST2 dll")
        self.cb_vst2.setChecked(True)
        self.cb_standalone = QCheckBox("Standalone app")
        self.cb_standalone.setChecked(True)
        self.cb_presets = QCheckBox("Factory presets (never overwrites yours)")
        self.cb_presets.setChecked(True)
        self.cb_aax = QCheckBox("AAX (Pro Tools only)")
        for box in (self.cb_vst3, self.cb_vst2, self.cb_standalone, self.cb_presets, self.cb_aax):
            opts.addWidget(box)
        # without this the checkboxes stretch: five widgets, no stretch factor, so Qt hands each of
        # them a fifth of the window (VST3 hard left, AAX hard right) instead of grouping them
        opts.addStretch(1)
        layout.addWidget(options)

        buttons = QHBoxLayout()
        self.btn_preview = QPushButton("Preview plan")
        self.btn_preview.clicked.connect(lambda: self.run_install(dry_run=True))
        self.btn_install = QPushButton("Install")
        self.btn_install.clicked.connect(lambda: self.run_install(dry_run=False))
        buttons.addWidget(self.btn_preview)
        buttons.addWidget(self.btn_install)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.install_table = QTableWidget(0, 3)
        self.install_empty = _empty_note(
            "No plan yet: pick an installer MSI above, then 'Preview plan'.\n"
            "The preview lists each destination before anything is written."
        )
        layout.addWidget(self.install_empty, 2)
        _show_rows(self.install_table, self.install_empty, 0)
        self.install_table.setHorizontalHeaderLabels(["Status", "Path", "Detail"])
        _fit_columns(self.install_table, stretch={1: 300, 2: 220}, contents=(0,), elide={1: True})
        layout.addWidget(self.install_table, 2)

        self.install_log = _log_pane("Job output appears here: the plan, the destination list and every verified file.", 2000)
        layout.addWidget(self.install_log, 1)
        return page

    def browse_msi(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select the vendor MSI", str(Path.home()), "MSI (*.msi)")
        if path:
            self.msi_combo.insertItem(0, path)
            self.msi_combo.setCurrentIndex(0)

    def find_msis(self) -> None:
        """List the plugin MSIs inside the prefix, off the GUI thread.

        `declares_plugin_payload` shells out to msitools once per cached MSI, so on a prefix with a
        dozen of them this is seconds of work - the same reason the downloads scan runs on a worker
        (see `_scan_prefix_and_downloads`). It used to run inline, on the GUI thread.
        """
        env = self.env
        if env is None:
            self.log("no environment detected")
            return
        self._spawn(
            self._scan_prefix_msis, env.prefix,
            on_line=self.log,
            on_done=lambda found: self.msis_found(found, env.prefix),
            on_failed=lambda msg: self.log(f"could not read the prefix's MSIs: {msg}"),
            log=self.log,
            label="find MSIs",
        )

    def _scan_prefix_msis(self, emit, prefix) -> list:
        """The slow half of `find_msis`. Reads only; touches no widget."""
        emit(f"reading the cached MSIs under {prefix} …")
        return [
            path
            for path in msi_mod.find_extracted_msis(prefix, include_installer_cache=True)
            if msi_mod.declares_plugin_payload(path)
        ]

    def msis_found(self, found, prefix: Path | None = None) -> None:
        # the combo is replaced only on success: clearing it before the scan meant a failed scan
        # threw away the MSI the user had already picked
        self.msi_combo.clear()
        for path in found:
            self.msi_combo.addItem(str(path))
        prefix_label = prefix if prefix is not None else (self.env.prefix if self.env else "the selected prefix")
        self.log(f"{len(found)} MSI(s) found inside {prefix_label}")

    def log(self, message: str) -> None:
        self.install_log.appendPlainText(message)

    def run_install(self, dry_run: bool) -> None:
        env = self.env
        if env is None:
            QMessageBox.warning(self, "No environment", "Wine prefix not detected. See the Environment tab.")
            return
        if not dry_run and self._refuse_prefix_write_for_standalone("install", self.install_log):
            return
        if not self.msi_combo.currentText():
            QMessageBox.warning(self, "No MSI", "Pick an installer MSI first.")
            return
        self.install_table.setRowCount(0)
        # clearing the table has to put the note back, or a second Preview click leaves an empty
        # grid on screen where the note should be (the same swap plugins_done does)
        _show_rows(self.install_table, self.install_empty, 0)
        self._busy(True)
        msi_path = Path(self.msi_combo.currentText())
        # Read the choices HERE, on the GUI thread, and hand them to the job as plain values.
        # Touching a QWidget from the worker thread is undefined behaviour in Qt - at best the
        # job sees a stale copy of the options and installs the wrong subset.
        options = {
            "include_vst2": self.cb_vst2.isChecked(),
            "include_aax": self.cb_aax.isChecked(),
            "include_standalone": self.cb_standalone.isChecked(),
            "include_presets": self.cb_presets.isChecked(),
        }

        def job(emit, msi_path: Path, dry_run: bool, options: dict):
            emit(f"extracting {msi_path.name} -> {SCRATCH}")
            msi_mod.extract(msi_path, SCRATCH)
            plan = build_plan(msi_path, env, SCRATCH, **options)
            return plan, apply_plan(plan, env, dry_run=dry_run)

        self._spawn(
            job, msi_path, dry_run, options,
            on_line=self.log,
            on_done=lambda result: self.install_done(result, dry_run, env),
            on_failed=lambda msg: (self.install_failed(msg), self.refresh_plugins()),
            log=self.log,
            label="install",
            restore_text=lambda: self._busy(False),
        )

    def _log_plan_warnings(self, plan, log) -> None:
        """Surface what the plan refused to do, so 0 destinations never reads as success."""
        for warning in plan.warnings:
            log(f"! {warning}")
        if not plan.actions:
            log("nothing will be placed: this package is an application or a driver, not a plugin"
                if plan.skipped_app else
                "nothing to place: this MSI declares no payload this toolkit recognises")

    def install_done(self, result, dry_run: bool, env: Environment | None = None) -> None:
        plan, rows = result
        env = env if env is not None else self.env
        if env is None:
            self.log("verification skipped: the Wine environment is no longer detected")
            self._busy(False)
            return
        self.plan = plan
        for status, path, note in rows:
            row = self.install_table.rowCount()
            self.install_table.insertRow(row)
            self.install_table.setItem(row, 0, QTableWidgetItem(status))
            self.install_table.setItem(row, 1, QTableWidgetItem(path))
            self.install_table.setItem(row, 2, QTableWidgetItem(note))
        _show_rows(self.install_table, self.install_empty, self.install_table.rowCount())

        self._log_plan_warnings(plan, self.log)

        if dry_run:
            self.log(f"preview: {len(rows)} destinations, {plan.total_bytes() / 1e6:.0f} MB payload. Nothing written.")
        else:
            from .installer import verify_plan

            checks = verify_plan(plan, env if env is not None else self.env)
            ok = sum(1 for c in checks if c[0] == "ok")
            bad = [c for c in checks if c[0] != "ok"]
            self.log(f"verified {ok} files byte-for-byte against the MSI File table")
            for status, path, note in bad:
                self.log(f"  {status}: {path} ({note})")
            self.log("done - rescan plugins in your DAW")
        self._busy(False)

    def _notice(self, title: str, message: str) -> None:
        """Report something without stopping the event loop.

        `QMessageBox.critical()` is blocking: it runs a nested event loop that ends only when
        someone dismisses it, so every line after it waits - including the code that frees the
        buttons the job had disabled. On a desktop that is one click; with the window hidden, sent
        to another display, or running headless, nobody ever clicks it and the toolkit sits there
        with those buttons disabled for good. The message still appears; it just is not waited on,
        which is the same reason the quit dialog above is modeless.
        (Found in the stability review: it is what hung tests/gui_smoke.py for its whole 600 s
        timeout, and what leaves a failed install with Install and Preview still disabled.)
        """
        box = QDialog(self)
        box.setWindowTitle(title)
        box.setModal(False)
        layout = QVBoxLayout(box)
        label = QLabel(message)
        label.setWordWrap(True)
        label.setMinimumWidth(_DIALOG_TEXT_WIDTH)
        layout.addWidget(label)
        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(box.close)
        row.addWidget(close)
        layout.addLayout(row)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.setMinimumWidth(_DIALOG_TEXT_WIDTH + 60)

        def forget(*_args) -> None:
            if box in self._notices:
                self._notices.remove(box)

        box.finished.connect(forget)
        self._notices.append(box)
        box.show()
        box.adjustSize()

    def install_failed(self, message: str) -> None:
        # Free the buttons *before* reporting anything: see _notice().
        self.log(f"FAILED: {message}")
        self._busy(False)
        self._notice("Install failed", message)

    def _busy(self, busy: bool) -> None:
        self.btn_install.setEnabled(not busy)
        self.btn_preview.setEnabled(not busy)

    # ------------------------------------------------------------ downloads
    def _downloads_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        bar = QHBoxLayout()
        refresh = QPushButton("Refresh catalogue")
        refresh.setToolTip("Re-read neuraldsp.com/downloads. Without it the bundled snapshot is used.")
        refresh.clicked.connect(lambda: self.load_catalogue(refresh=True))
        bar.addWidget(refresh)
        all_pages = QPushButton("Browse Plugins In Browser")
        all_pages.setToolTip(
            "Opens neuraldsp.com/downloads in your browser: every plugin's download page in one\n"
            "list, for anything not in the table above."
        )
        all_pages.clicked.connect(lambda *_args: self.browse_plugins_in_browser())
        bar.addWidget(all_pages)
        open_page = QPushButton("Download Selected Plugin")
        open_page.setToolTip(
            "Opens the selected plugin's download page in your own browser. Neural DSP needs you\n"
            "signed in before it hands over an installer, so this is the step the toolkit cannot do\n"
            "for you."
        )
        open_page.clicked.connect(lambda *_args: self.download_selected_plugin())
        bar.addWidget(open_page)
        # "Install downloaded installer" and the "watch ~/Downloads" checkbox used to live here.
        # Installs now happen on the Pending Install tab - this tab fetches, that tab stages and
        # installs - so the row is three buttons instead of five controls. The change-detected
        # refresh stays and no longer needs a switch: it is what keeps the "Installer" column
        # current, and Pending Install reads the same ~/Downloads state.
        bar.addStretch(1)
        layout.addLayout(bar)

        # ------------------------------------------------------------------ preset sources
        # The toolkit installs plugins; it does not fetch presets, several of these sites need
        # a sign-in or sit behind a bot filter, so the honest thing is to open the right page in
        # *your* browser, pre-filled with the plugin you have selected.
        presets_bar = QHBoxLayout()
        presets_bar.addWidget(QLabel("Preset & IR sources:"))
        self.source_combo = QComboBox()
        for source in sources_mod.SOURCES:
            self.source_combo.addItem(f"{source.name}  ({source.kind})", source.name)
        self.source_combo.setMinimumWidth(320)
        self.source_combo.currentIndexChanged.connect(lambda index: self._source_changed(index))
        presets_bar.addWidget(self.source_combo)
        # The button belongs *next to* the combo it acts on. It used to sit at the far right of a
        # stretch, with nothing tying it to the row's label - read as an unanchored control.
        self.btn_source_open = QPushButton("Open in browser")
        self.btn_source_open.setToolTip(
            "Opens the selected source in your own browser. Nothing is downloaded by the toolkit.\n"
            "If a plugin is selected in the table, sources that support searching open already\n"
            "searching for it."
        )
        self.btn_source_open.clicked.connect(self.open_source)
        presets_bar.addWidget(self.btn_source_open)
        presets_bar.addSpacing(16)
        self.btn_source_note = QLabel("")
        self.btn_source_note.setWordWrap(True)
        # no stylesheet here: `palette(mid)` is a background role, so it resolves to a colour the
        # desktop may use for a *background* - 1.25:1 on Breeze Dark - and it is resolved once, so
        # flipping the dark switch never re-colours it either. The window text colour reads on
        # both themes (see the note below about the same mistake in download_hint).
        # `currentIndexChanged` is connected after the combo is filled, so the entry it opens on
        # never fired it and this note stayed blank until the user changed the selection. Called
        # here, not next to the connect, because the label it writes to does not exist yet there.
        self._source_changed()
        presets_bar.addWidget(self.btn_source_note, 1)
        layout.addLayout(presets_bar)

        self.download_summary = QLabel(
            "Neural DSP installers. Pick a plugin and open its page in your browser. The download\n"
            "lands in ~/Downloads, and the Pending Install tab installs it from there (the .exe → .msi\n"
            "step is handled for you)."
        )
        self.download_summary.setWordWrap(True)
        layout.addWidget(self.download_summary)

        self.download_hint = QLabel(
            "Click a plugin, then <b>Download Selected Plugin</b> (their links need you signed in), or "
            "<b>Browse Plugins In Browser</b> for their full list. <b>Right-click a row</b> for the preset "
            "&amp; IR sites. Downloaded installers show up on the <b>Pending Install</b> tab."
        )
        self.download_hint.setWordWrap(True)
        self.download_hint.setTextFormat(Qt.TextFormat.RichText)
        # no stylesheet: `palette(mid)` can be near-invisible on a dark theme (reported as hidden
        # instructions as "hidden"), so this uses the normal window text colour
        layout.addWidget(self.download_hint)

        self.download_table = QTableWidget(0, 5)
        self.download_table.setHorizontalHeaderLabels(["Product", "Version", "Released", "In the prefix", "Installer"])
        # the installer column is the longest text in this table, so it gets as much room as the
        # product column rather than whatever Product left over
        _fit_columns(self.download_table, stretch={0: 220, 4: 320}, contents=(1, 2, 3), elide={4: True})
        self.download_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.download_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.download_table.customContextMenuRequested.connect(self._download_context_menu)
        self.download_empty = _empty_note(
            "No catalogue rows loaded yet: press 'Refresh catalogue' to populate this list."
        )
        layout.addWidget(self.download_empty, 3)
        _show_rows(self.download_table, self.download_empty, 0)
        layout.addWidget(self.download_table, 3)

        self.download_log = _log_pane("Job output appears here: catalogue refreshes and installer downloads.", 1000)
        layout.addWidget(self.download_log, 1)

        self._downloads: dict[str, Path] = {}
        self._installed_products: set[str] = set()
        self._msi_names: set[str] = set()
        self._seen_downloads: set[Path] = set()
        self._catalogue = catalogue_mod.load_snapshot()
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(4000)
        self._watch_timer.timeout.connect(self._watch_tick)
        self._watch_timer.start()
        self._startup_catalogue_timer = QTimer(self)
        self._startup_catalogue_timer.setSingleShot(True)
        self._startup_catalogue_timer.timeout.connect(self._startup_load_catalogue)
        self._startup_catalogue_timer.start(200)
        return page

    def _startup_load_catalogue(self) -> None:
        """Load the catalogue only while the window is still open."""
        if not self._closing:
            self.load_catalogue(refresh=False)

    def load_catalogue(self, refresh: bool = False) -> None:
        if self._closing:
            return
        catalogue = catalogue_mod.load_snapshot()
        if refresh:
            self.download_summary.setText("Reading neuraldsp.com/downloads …")
            catalogue = catalogue_mod.refresh(
                log=lambda message: self.download_log.appendPlainText(message))
        self._catalogue = catalogue
        self.refresh_downloads(background=bool(self.env))

    def _registered_names(self) -> set[str]:
        """Product names this prefix knows: from MSIs, and from what is actually on disk.

        MSI names alone are not enough, a toolkit-placed install need not leave an MSI behind at
        all, and a driver/application package is not listed anywhere a plugin would be, so both
        sources are combined. Directory listings only: this runs on a timer and must stay cheap.
        """
        names: set[str] = set()
        for msi_path in msi_mod.find_extracted_msis(self.env.prefix, include_installer_cache=True):
            try:
                name = msi_mod.identity(msi_path).product_name
            except Exception:  # noqa: BLE001 - one unreadable MSI must not break the tab
                continue
            if name:
                names.add(_key(name))

        # plugin files, one product folder deep in the plugin directories
        for root in (self.env.vst3_dir, self.env.vst2_dir, self.env.aax_dir):
            if not root.is_dir():
                continue
            for path in list(root.iterdir()) + [c for d in root.iterdir() if d.is_dir() for c in d.iterdir()]:
                stem = path.name[:-len(".disabled")] if path.name.endswith(".disabled") else path.name
                names.add(_key(Path(stem).stem))

        # vendor folders in Program Files and ProgramData hold the product's own directory,
        # which is where an application or driver lands (Nano Cortex: Neural DSP Drivers/Nano
        # Cortex Driver)
        for base_dir in (self.env.program_files, self.env.program_data):
            if not base_dir.is_dir():
                continue
            for vendor in base_dir.iterdir():
                if not vendor.is_dir():
                    continue
                for product in list(vendor.iterdir()):
                    names.add(_key(product.stem))
        return names

    def _is_installed(self, release) -> str:
        """'', 'installed', or 'files only', a toolkit-placed install has no MSI to prove it."""
        names = self._installed_products
        key = _key(release.product)
        if key in self._msi_names:
            return "installed"
        if key in names:
            return "installed"
        tokens = [token for token in _split(release.product) if len(token) >= 4]
        if tokens and any(all(token in name for token in tokens) for name in names):
            return "installed" if any(all(token in name for token in tokens) for name in self._msi_names) else "files only"
        return ""

    def _selected_product_key(self) -> str:
        rows = self.download_table.selectionModel().selectedRows()
        if not rows:
            return ""
        cell = self.download_table.item(rows[0].row(), 0)
        release = cell.data(Qt.ItemDataRole.UserRole) if cell else None
        return _key(release.product) if release is not None else ""

    def _restore_selection(self, product_key: str) -> None:
        """Put the highlight back where it was, so a refresh is invisible."""
        if not product_key:
            return
        for row in range(self.download_table.rowCount()):
            cell = self.download_table.item(row, 0)
            release = cell.data(Qt.ItemDataRole.UserRole) if cell else None
            if release is not None and _key(release.product) == product_key:
                self.download_table.selectRow(row)
                self.download_table.scrollToItem(cell)
                return

    def _scan_prefix_and_downloads(self, env: Environment | None = None):
        """The slow half of a downloads refresh: cached MSIs, and what is in ~/Downloads.

        Reading product names out of cached MSIs and matching downloaded installers both shell out
        to msitools, which is seconds of work. This runs on a worker thread; the table is filled
        afterwards on the GUI thread.
        """
        env = env if env is not None else self.env
        if env is None:
            return set(), {}
        msi_names: set[str] = set()
        for msi_path in msi_mod.find_extracted_msis(env.prefix, include_installer_cache=True):
            try:
                name = msi_mod.identity(msi_path).product_name
            except Exception:  # noqa: BLE001 - one unreadable MSI must not break the tab
                continue
            if name:
                msi_names.add(_key(name))
        downloads = Path.home() / "Downloads"
        pending = (installers_mod.discover(env, extra_dirs=[downloads])
                   if downloads.is_dir() else [])
        return msi_names, {_key(item.product): item.path for item in pending}

    def refresh_downloads(self, *, background: bool = False) -> None:
        """Fill the table. `background=True` does the slow scan on a worker thread.

        The watcher used to call this every time ~/Downloads changed, which meant a msitools scan
        on the GUI thread - the freeze the worker threads exist to avoid.
        """
        env = self.env
        if self._closing or env is None:
            return
        if background:
            # A scan already in flight will refill the table when it lands, so a second one is not
            # started. Returning here rather than falling through matters: without it the old
            # `and not self._downloads_scan_running` simply failed and control reached the
            # synchronous scan below, running msitools on the GUI thread - the freeze these workers
            # exist to avoid. It fires whenever ~/Downloads changes mid-scan, i.e. right after a
            # download finishes, which is when this is called.
            if self._downloads_scan_running:
                return
            self._downloads_scan_running = True

            def work(emit):
                return self._scan_prefix_and_downloads(env)

            worker = Worker(work)
            self._workers.append(worker)

            def done(result):
                self._downloads_scan_running = False
                msi_names, downloads = result
                self._msi_names = msi_names
                self._downloads = downloads
                self._fill_download_table()
                before = getattr(self, "_downloads_before", None)
                self._downloads_before = None
                if before is not None:
                    for key, path in self._downloads.items():
                        if key not in before:
                            self.download_log.appendPlainText(f"found a new installer: {path}")
                            self.download_log.appendPlainText(
                                "  it is now listed on the Pending Install tab")

            def failed(message):
                self._downloads_scan_running = False
                self.download_log.appendPlainText(f"could not list downloads: {message}")
                self._fill_download_table()

            worker.done.connect(done)
            worker.failed.connect(failed)
            worker.finished.connect(lambda: self._forget_worker(worker))
            worker.start()
            return

        self._msi_names, self._downloads = self._scan_prefix_and_downloads(env)
        self._fill_download_table()

    def _fill_download_table(self) -> None:
        keep = self._selected_product_key()
        self._installed_products = self._registered_names()
        rows = self._catalogue.releases
        self.download_table.setRowCount(0)
        ready = 0
        self._seen_downloads = self._download_candidates()
        for release in rows:
            key = _key(release.product)
            installer = self._downloads.get(key) or self._downloads_for(release)
            if installer:
                ready += 1
            installed = self._is_installed(release)
            row = self.download_table.rowCount()
            self.download_table.insertRow(row)
            cells = [
                release.product,
                release.version or "?",
                release.released or "",
                installed or "-",
                installer.name if installer else ("open page (sign in)" if release.needs_sign_in else "downloadable"),
            ]
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                cell.setData(Qt.ItemDataRole.UserRole, release)
                if column == 4 and installer:
                    cell.setToolTip(str(installer))
                if installer:
                    cell.setData(Qt.ItemDataRole.UserRole + 1, str(installer))
                self.download_table.setItem(row, column, cell)

        _show_rows(self.download_table, self.download_empty, len(rows))
        self._restore_selection(keep)
        stamp = self._catalogue.fetched or "bundled snapshot"
        installed_here = sum(1 for r in rows if _key(r.product) in self._installed_products)
        self.download_summary.setText(
            f"{len(rows)} releases from Neural DSP ({stamp})   ·   {ready} with an installer already in "
            f"~/Downloads   ·   {installed_here} of them installed in this prefix"
        )

    def _forget_worker(self, worker) -> None:
        """Join and drop a finished worker so closing cannot outpace its teardown."""
        worker.wait()
        try:
            self._workers.remove(worker)
        except ValueError:
            pass
        if self._closing and not self._jobs_running() and not self._update_job_running():
            self.close()

    def _downloads_for(self, release) -> Path | None:
        """Match a catalogue entry to a downloaded file, strictly.

        `match_download` lives at module level so it can be tested without a real ~/Downloads:
        the first version matched on *any* shared token, which offered the Nano Cortex
        installer for Quad Cortex and for Cortex Control (seen in a rendered screenshot). A wrong match here is not cosmetic, "Install downloaded installer" would
        install the wrong product.
        """
        downloads = Path.home() / "Downloads"
        if not downloads.is_dir():
            return None
        return match_download(release.product, [*downloads.glob("*.exe"), *downloads.glob("*.msi")])

    def _download_candidates(self) -> set[Path]:
        """The installer files sitting in ~/Downloads, cheap enough to check on a timer."""
        downloads = Path.home() / "Downloads"
        if not downloads.is_dir():
            return set()
        return {p for p in downloads.glob("*.exe")} | {p for p in downloads.glob("*.msi")}

    def _watch_tick(self) -> None:
        """Notice a freshly downloaded installer, and otherwise leave the table alone.

        This used to call `refresh_downloads()` every four seconds, which cleared and rebuilt
        every row: the selected row lost its highlight mid-click, and "Open download page" then
        found nothing selected and did nothing (reported 2026-09-26: *"the highlight disappears
        at times... it either does nothing or opens it, result changes randomly"*). Now the tick
        only acts when the set of files in ~/Downloads actually changes.
        """
        if not self.env:
            return
        candidates = self._download_candidates()
        if candidates == self._seen_downloads:
            return
        self._seen_downloads = candidates
        # hand the scan to a worker: matching installers means msitools, and doing that on the GUI
        # thread is what froze the window when a download finished
        self._downloads_before = dict(self._downloads)
        self.refresh_downloads(background=True)

    def _download_context_menu(self, position) -> None:
        """Right-click a row: the same actions as the buttons, plus the preset sources.

        Right-clicking also *selects* the row under the cursor, the buttons act on the selection,
        so a menu that did not do this would look like a menu that does nothing.
        """
        row = self.download_table.rowAt(position.y())
        if row < 0:
            return
        self.download_table.selectRow(row)
        cell = self.download_table.item(row, 0)
        release = cell.data(Qt.ItemDataRole.UserRole) if cell else None
        if release is None:
            return
        installer = cell.data(Qt.ItemDataRole.UserRole + 1)

        menu = self._build_download_menu(release, Path(installer) if installer else None)
        try:
            menu.exec(self.download_table.viewport().mapToGlobal(position))
        finally:
            menu.deleteLater()

    def _build_download_menu(self, release, installer: Path | None) -> QMenu:
        """The row menu, built separately so it can be inspected without a modal exec()."""
        menu = QMenu(self)
        open_action = menu.addAction("Download Selected Plugin (in browser)")
        open_action.triggered.connect(lambda: self.download_selected_plugin(release))

        if installer:
            stage_action = menu.addAction(f"Staged for install: {installer.name}")
            stage_action.setToolTip("Opens the Pending Install tab, where installers are installed from")
            stage_action.triggered.connect(self.goto_pending)
        else:
            hint = menu.addAction("Not downloaded yet")
            hint.setEnabled(False)

        menu.addSeparator()
        presets_menu = menu.addMenu("Preset & IR sources")
        for source in sources_mod.SOURCES:
            label = source.name + ("  (search this plugin)" if source.searchable else "")
            action = presets_menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, name=source.name: self._open_source_for(name, release.product)
            )

        menu.addSeparator()
        copy_name = menu.addAction("Copy product name")
        copy_name.triggered.connect(lambda: self._copy_to_clipboard(release.product))
        if installer:
            copy_path = menu.addAction("Copy installer path")
            copy_path.triggered.connect(lambda: self._copy_to_clipboard(str(installer)))
        copy_url = menu.addAction("Copy download link")
        copy_url.triggered.connect(lambda: self._copy_to_clipboard(release.windows or release.macos or ""))
        return menu

    def _open_source_for(self, name: str, product: str) -> None:
        opened, url = sources_mod.open_source(name, product)
        if not url:
            self.download_log.appendPlainText(f"no source called '{name}'")
            return
        self.download_log.appendPlainText(
            f"opened {name} for {product}: {url}" if opened else f"could not open a browser. {url}"
        )

    def _copy_to_clipboard(self, text: str) -> None:
        if not text:
            self.download_log.appendPlainText("nothing to copy there")
            return
        QApplication.clipboard().setText(text)
        self.download_log.appendPlainText(f"copied: {text}")

    def _selected_release(self):
        rows = self.download_table.selectionModel().selectedRows()
        if not rows:
            return None, None
        cell = self.download_table.item(rows[0].row(), 0)
        if cell is None:
            return None, None
        release = cell.data(Qt.ItemDataRole.UserRole)
        installer = cell.data(Qt.ItemDataRole.UserRole + 1)
        return release, Path(installer) if installer else None

    def goto_pending(self) -> None:
        """Jump to the staging tab - the one place installers are installed from."""
        self.tabs.setCurrentWidget(self.tabs.widget(3))
        self.refresh_pending()

    # ------------------------------------------------------------------ preset sources
    def _source_changed(self, _index: int | None = None) -> None:
        source = sources_mod.find(self.source_combo.currentData() or "")
        if source is None:
            self.btn_source_note.setText("")
            return
        tail = []
        if source.searchable:
            tail.append("searches for the selected plugin")
        if source.sign_in:
            tail.append("sign-in in your browser")
        self.btn_source_note.setText(source.note + (f"  ({'; '.join(tail)})" if tail else ""))

    def _selected_product(self) -> str:
        """The plugin selected in the catalogue table, if any, that is what gets searched."""
        rows = self.download_table.selectionModel().selectedRows()
        if not rows:
            return ""
        item = self.download_table.item(rows[0].row(), 0)
        return item.text() if item else ""

    def open_source(self) -> None:
        name = self.source_combo.currentData()
        product = self._selected_product()
        opened, url = sources_mod.open_source(name or "", product)
        if not url:
            self.download_log.appendPlainText(f"no source called '{name}'")
            return
        where = f" for {product}" if product and sources_mod.find(name or "") and sources_mod.find(name or "").searchable else ""
        self.download_log.appendPlainText(
            f"opened {url}{where}" if opened else f"could not open a browser. The link is: {url}"
        )

    def browse_plugins_in_browser(self) -> None:
        """The vendor's own downloads index, every plugin link in one place."""
        url = catalogue_mod.DOWNLOADS_URL
        opened = QDesktopServices.openUrl(QUrl(url))
        self.download_log.appendPlainText(
            f"opened the full downloads list: {url}" if opened
            else f"could not open a browser. The link is: {url}"
        )

    def download_selected_plugin(self, release=None) -> None:
        """Open the vendor's page for a plugin, in the user's own browser.

        `release` is passed by the context menu; the button connection goes through a lambda
        because Qt hands a `clicked` slot a `checked` bool, which this used to accept as the
        release and then crash on (`'bool' object has no attribute 'windows'`, an observed traceback,
        2026-09-26). The isinstance guard is the belt to that lambda's braces.
        

        Saying so when there is no selection matters: this used to return silently, which is
        indistinguishable from a broken button (reported as *"it either does nothing or opens
        it"*, the four-second table rebuild was clearing the selection underneath the click).
        """
        if release is None or isinstance(release, bool):
            release, _ = self._selected_release()
        if release is None:
            self.download_log.appendPlainText(
                "nothing selected: click a plugin row first (or right-click it for the menu)"
            )
            return
        url = release.windows or release.macos
        if not url:
            QMessageBox.warning(self, "No link", f"{release.product} has no download link in the catalogue.")
            return
        opened = QDesktopServices.openUrl(QUrl(url))
        self.download_log.appendPlainText(
            f"opened {release.product} in your browser: {url}" if opened
            else f"could not open a browser. The link is: {url}"
        )
        if release.needs_sign_in:
            self.download_log.appendPlainText(
                "  sign in if it asks - the download lands in ~/Downloads and this tab picks it up"
            )

    # -------------------------------------------------------------- pending
    def _pending_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        bar = QHBoxLayout()
        refresh = QPushButton("Find downloaded installers")
        refresh.clicked.connect(self.refresh_pending)
        bar.addWidget(refresh)
        self.btn_install_pending = QPushButton("Install selected")
        self.btn_install_pending.setEnabled(False)
        self.btn_install_pending.clicked.connect(self.install_pending_selected)
        bar.addWidget(self.btn_install_pending)
        bar.addStretch(1)
        layout.addLayout(bar)

        self.pending_summary = QLabel(
            "Not scanned yet - looks in ~/Downloads and the prefix root for .msi / .exe installers."
        )
        self.pending_summary.setWordWrap(True)
        layout.addWidget(self.pending_summary)

        self.pending_table = QTableWidget(0, 5)
        self.pending_table.setHorizontalHeaderLabels(["Status", "Product", "Version", "Kind", "File"])
        _fit_columns(self.pending_table, stretch={4: 300}, contents=(0, 1, 2, 3), elide={4: True})
        self.pending_empty = _empty_note(
            "No installers waiting: press 'Find downloaded installers' to scan ~/Downloads\n"
            "and the prefix root for .msi / .exe files."
        )
        layout.addWidget(self.pending_empty, 2)
        _show_rows(self.pending_table, self.pending_empty, 0)
        self.pending_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.pending_table.itemSelectionChanged.connect(self._pending_selection_changed)
        layout.addWidget(self.pending_table, 3)

        self.pending_log = _log_pane("Nothing scanned yet: press 'Find downloaded installers'. Output appears here.", 1000)
        layout.addWidget(self.pending_log, 1)
        return page

    def refresh_pending(self) -> None:
        env = self.env
        if env is None:
            QMessageBox.warning(self, "No environment", "Wine prefix not detected.")
            return
        previous_summary = self.pending_summary.text()
        self.pending_summary.setText("Looking for installers…")
        self._spawn(
            lambda emit: installers_mod.discover(env),
            on_done=self.pending_done,
            on_failed=lambda msg: self.pending_summary.setText(f"Discovery failed: {msg}"),
            log=self.pending_log,
            label="discovery",
            restore_text=lambda: self.pending_summary.setText(previous_summary),
        )

    def pending_done(self, found) -> None:
        self.pending_table.setRowCount(0)
        for item in found:
            row = self.pending_table.rowCount()
            self.pending_table.insertRow(row)
            cells = [
                item.status,
                item.product,
                item.version or "?",
                item.kind,
                str(item.path),
            ]
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                cell.setData(Qt.ItemDataRole.UserRole, item)
                self.pending_table.setItem(row, column, cell)
        _show_rows(self.pending_table, self.pending_empty, len(found))
        waiting = [i for i in found if i.installed is None]
        self.pending_summary.setText(
            f"{len(found)} installer(s) found   ·   {len(waiting)} not installed yet"
            + ("" if waiting else "   ·   nothing new to install")
        )
        if not found:
            self.pending_log.appendPlainText(
                "nothing found in ~/Downloads or the prefix root (.msi and .exe are scanned)"
            )

    def _selected_installer(self):
        rows = self.pending_table.selectionModel().selectedRows()
        if not rows:
            return None
        cell = self.pending_table.item(rows[0].row(), 0)
        return cell.data(Qt.ItemDataRole.UserRole) if cell else None

    def _pending_selection_changed(self) -> None:
        self.btn_install_pending.setEnabled(self._selected_installer() is not None)

    def install_pending_selected(self) -> None:
        installer = self._selected_installer()
        env = self.env
        if not installer or env is None:
            return
        if self._refuse_prefix_write_for_standalone("install", self.pending_log):
            return
        # a downloaded .exe is the vendor's wrapper around an MSI: bridge the two here,
        # running the wrapper under Wine only when that is the only way to get its MSI
        answer = QMessageBox.question(
            self,
            "Install plugin",
            f"Install {installer.product} from {installer.path.name}?\n\n"
            "If that is the vendor's .exe, its MSI is extracted first, and if the wrapper has to be "
            "run under Wine to produce one, a vendor installer window will open and may take a few "
            "minutes. One plugin at a time.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.pending_log.appendPlainText(f"installing {installer.product} from {installer.path.name}")
        self.btn_install_pending.setEnabled(False)

        def job(emit, installer):
            prepared = wrappers_mod.prepare_msi(
                env,
                installer.path,
                SCRATCH,
                product_hint=installer.product,
                allow_wine=True,
                log=emit,
            )
            if not prepared.ok:
                raise RuntimeError(prepared.detail)
            if prepared.msi is None:
                raise RuntimeError("installer preparation succeeded without an MSI path")
            emit(f"MSI: {prepared.msi}")
            msi_mod.extract(prepared.msi, SCRATCH)
            plan = build_plan(prepared.msi, env, SCRATCH)
            rows = apply_plan(plan, env, dry_run=False)
            bad = [c for c in verify_plan(plan, env) if c[0] != "ok"]
            return plan, rows, bad

        self._spawn(
            job, installer,
            on_line=self.pending_log.appendPlainText,
            on_done=self.install_pending_done,
            on_failed=lambda msg: (
                self.pending_log.appendPlainText(f"install failed: {msg}"),
                self.btn_install_pending.setEnabled(True),
            ),
            log=self.pending_log,
            label="install",
            restore=(self.btn_install_pending,),
        )

    def install_pending_done(self, result) -> None:
        plan, rows, bad = result
        self._log_plan_warnings(plan, self.pending_log.appendPlainText)
        for status, path, note in rows:
            self.pending_log.appendPlainText(f"{status}: {path} {note}")
        self.pending_log.appendPlainText(
            f"{len(rows)} destination(s) written, {len(bad)} failing verification"
            + ("" if not bad else ": " + ", ".join(p for _, p, _ in bad))
        )
        # _log_plan_warnings above already printed every warning: repeating them here made one
        # problem look like two, and the other two install paths do not do it
        self.btn_install_pending.setEnabled(True)
        self.refresh_pending()

    # ----------------------------------------------------------------- scan
    def _scan_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        run = QPushButton("Scan prefix for broken installs")
        run.clicked.connect(self.run_scan)
        layout.addWidget(run, alignment=Qt.AlignmentFlag.AlignLeft)
        triage = QPushButton("Triage products in every Wine prefix")
        triage.setToolTip(
            "Reads every Wine prefix on this machine (this one, ~/.wine, Bottles bottles, game\n"
            "prefixes) and says for each recorded product whether it is in use, files only, a\n"
            "registry fragment, or installed into a different prefix."
        )
        triage.clicked.connect(self.run_triage)
        layout.addWidget(triage, alignment=Qt.AlignmentFlag.AlignLeft)

        self.scan_summary = QLabel("Not scanned yet.")
        self.scan_summary.setWordWrap(True)
        layout.addWidget(self.scan_summary)

        self.scan_table = QTableWidget(0, 2)
        self.scan_empty = _empty_note(
            "Nothing scanned yet: press 'Scan prefix for broken installs'.\n"
            "It lists registry paths and checks whether each file is on disk."
        )
        layout.addWidget(self.scan_empty, 1)
        _show_rows(self.scan_table, self.scan_empty, 0)
        self.scan_table.setHorizontalHeaderLabels(["Status", "Registry path"])
        _fit_columns(self.scan_table, stretch={1: 320}, contents=(0,), elide={1: True})
        layout.addWidget(self.scan_table, 1)

        self.scan_products = _log_pane("Nothing triaged yet: 'Triage products' lists every product in every prefix here.", 500)
        layout.addWidget(self.scan_products, 1)
        return page

    def run_scan(self) -> None:
        env = self.env
        if env is None:
            QMessageBox.warning(self, "No environment", "Wine prefix not detected.")
            return
        previous_summary = self.scan_summary.text()
        self.scan_summary.setText("Scanning…")
        self._spawn(
            lambda emit: scan_prefix(env),
            on_done=self.scan_done,
            on_failed=lambda msg: self.scan_summary.setText(f"Scan failed: {msg}"),
            log=self.scan_products,
            label="scan",
            restore_text=lambda: self.scan_summary.setText(previous_summary),
        )

    def scan_done(self, report: Report) -> None:
        missing = report.missing
        self.scan_summary.setText(
            f"{len(report.entries)} plugin paths in the registry, {len(report.present)} present, "
            f"{len(missing)} MISSING on disk."
            + ("" if not missing else "  These are installs that registered but never copied their files.")
        )
        self.scan_table.setRowCount(0)
        for entry in report.entries:
            row = self.scan_table.rowCount()
            self.scan_table.insertRow(row)
            self.scan_table.setItem(row, 0, QTableWidgetItem("present" if entry.exists else "MISSING"))
            self.scan_table.setItem(row, 1, QTableWidgetItem(entry.raw))
        _show_rows(self.scan_table, self.scan_empty, len(report.entries))
        # runtimes and services carry a DisplayName too; listing eighty of them buries
        # the four products anyone is actually looking for
        products = [p for p in report.products if not scan_mod.is_system_product(p.get("DisplayName", ""))]
        hidden = len(report.products) - len(products)
        lines = [f"{p.get('DisplayName', '?')}  {p.get('DisplayVersion', '?')}" for p in products]
        if hidden:
            lines.append(f"({hidden} runtime/system entry/entries hidden - 'wpt scan --all-products' shows them)")
        self.scan_products.setPlainText("\n".join(lines) or "no registered products found")

    def run_triage(self) -> None:
        env = self.env
        if env is None:
            QMessageBox.warning(self, "No environment", "Wine prefix not detected.")
            return
        previous_summary = self.scan_summary.text()
        self.scan_summary.setText("Triage: reading every Wine prefix on this machine…")
        self._spawn(
            lambda emit: products_mod.render(products_mod.triage(env)),
            on_done=self.triage_done,
            on_failed=lambda msg: self.scan_summary.setText(f"Triage failed: {msg}"),
            log=self.scan_products,
            label="triage",
            restore_text=lambda: self.scan_summary.setText(previous_summary),
        )

    def triage_done(self, text: str) -> None:
        self.scan_products.setPlainText(text)
        self.scan_summary.setText(
            "Triage complete - the pane below lists every prefix found and each product's verdict."
        )


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Wine Plugin Toolkit")
    # Wayland matches a window to its launcher by this name; without it the taskbar shows a
    # generic python icon instead of the toolkit's own
    app.setDesktopFileName("wpt-gui")
    app.setWindowIcon(app_icon())
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
