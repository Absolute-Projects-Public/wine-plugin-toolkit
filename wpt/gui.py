"""PySide6 front end -- a thin shell over the same functions the CLI uses.

Design rules:
  * no logic lives here; if the GUI and the CLI could disagree, the bug is in
    the core modules, not in this file
  * anything slow (msiextract, a prefix scan) runs on a worker thread, so the
    window never freezes mid-install
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
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
from . import updates as updates_mod
from . import msi as msi_mod
from . import presets as presets_mod
from . import products as products_mod
from . import scan as scan_mod
from . import sources as sources_mod
from . import wrappers as wrappers_mod
from . import installer as installer_mod
from .environment import EnvironmentError_, detect
from .installer import (
    DISABLED_SUFFIX,
    apply_plan,
    build_plan,
    filter_needing_repair,
    set_enabled as set_plugin_enabled,
    uninstall as uninstall_product,
    verify_plan,
)
from .scan import Report
from .scan import scan as scan_prefix

SCRATCH = Path.home() / ".cache" / "wpt" / "extract"


def _fit_columns(table, stretch: dict[int, int] | None = None, contents=(), elide: dict[int, bool] | None = None) -> None:
    """Give the long-text columns the room and the short ones only what they need.

    A single `Stretch` column took the whole window and clipped the column that actually holds
    the long text (the installer path), which is backwards. Now: short columns size to their
    content, the long ones share the leftover space between them, path columns elide in the
    middle so both ends stay readable, and every path cell carries its full value as a tooltip.
    """
    header = table.horizontalHeader()
    header.setStretchLastSection(False)
    for column in contents:
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
    for column, minimum in (stretch or {}).items():
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        table.setColumnWidth(column, minimum)
    if elide:
        table.setTextElideMode(Qt.TextElideMode.ElideMiddle)


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
        self.resize(980, 700)
        self.env = None
        self.plan = None
        self.inv = None
        self.worker: Worker | None = None
        self._workers: list[Worker] = []
        self._closing = False
        # update checking owns its own workers so an update never queues behind, or blocks,
        # the prefix jobs (and vice versa)
        self._update_worker: Worker | None = None
        self._update_busy = False
        self._update_release = None
        self._update_path: Path | None = None

        tabs = QTabWidget()
        tabs.addTab(self._env_tab(), "Environment")
        tabs.addTab(self._plugins_tab(), "Plugins")
        tabs.addTab(self._install_tab(), "Install")
        tabs.addTab(self._downloads_tab(), "Download Plugins")
        tabs.addTab(self._pending_tab(), "Pending")
        tabs.addTab(self._scan_tab(), "Diagnostics")
        self.setCentralWidget(tabs)

        toolbar = QToolBar("Toolkit")
        toolbar.setMovable(False)
        self.btn_check_updates = QPushButton("Check for updates")
        self.btn_check_updates.setToolTip("Ask GitHub whether a newer release exists")
        self.btn_check_updates.clicked.connect(lambda _checked=False: self.check_for_updates(manual=True))
        toolbar.addWidget(self.btn_check_updates)
        self.lbl_update_state = QLabel("")
        toolbar.addWidget(self.lbl_update_state)
        self.addToolBar(toolbar)

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._wait_for_jobs)
        self.refresh_env()
        # a quiet check shortly after startup; the result is cached for a day
        QTimer.singleShot(2500, lambda: self.check_for_updates(manual=False))

    # ------------------------------------------------------------------ updates
    def check_for_updates(self, manual: bool = False) -> None:
        """Ask GitHub for the newest release. Never blocks the window.

        The startup check stays quiet when there is nothing to say; a manual click always says
        something, including "could not check", because a button that appears to do nothing is
        worse than a button that reports a failure.
        """
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

        launched, message = updates_mod.launch_install(path, restart=True)
        self.log(message if launched else f"update failed: {message}")
        if not launched:
            QMessageBox.warning(self, "Could not open a terminal", message)
            return
        self._wait_for_jobs()
        QTimer.singleShot(400, self.close)

    # ------------------------------------------------------------------ env
    def _spawn(self, fn, *args, on_line=None, on_done=None, on_failed=None, log=None, label="job") -> bool:
        """Run one job off the GUI thread — safely.

        Two rules here, both learned from a real crash on his machine (2026-09-26: he
        disabled a plugin, hit refresh, and the window died):

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
            message = f"{label}: still working on the previous one — try again in a moment"
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
            if worker in self._workers:
                self._workers.remove(worker)
            # a close requested while this job was running waits for it, rather than
            # destroying a live QThread (Qt aborts the process for that)
            if self._closing and not self._jobs_running():
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
        """An update check or download is its own QThread, held separately from prefix jobs."""
        worker = self._update_worker
        return bool(worker is not None and worker.isRunning())

    def _jobs_running(self) -> bool:
        """True while any background job is alive. (Not `_busy`, which predates this and
        toggles the buttons.)"""
        return any(w.isRunning() for w in self._workers)

    def _wait_for_jobs(self) -> None:
        """Give any running job a moment to finish before the process goes away.

        A QThread that is still running when the interpreter tears down aborts the process
        (SIGABRT, no traceback) — the same rule that made `closeEvent` wait. This is the safety
        net for every other way out (a quit from the menu, a session logout, a script that
        closes the window mid-refresh).
        """
        import time as _time

        deadline = _time.time() + 30
        while (self._jobs_running() or self._update_job_running()) and _time.time() < deadline:
            QApplication.processEvents()
            _time.sleep(0.05)

    def closeEvent(self, event) -> None:  # noqa: D102 - Qt entry point
        """Never let the window close out from under a running job.

        A QThread that is still running when its last reference goes away makes Qt abort
        the process (SIGABRT, no exception, no traceback) — which is how this started: he
        disabled a plugin, refreshed, and the window vanished. The window now stays alive
        (hidden) until the job finishes, then closes itself.
        """
        if not self._jobs_running():
            super().closeEvent(event)
            return
        self._closing = True
        self.hide()
        try:
            self.plugin_log.appendPlainText("finishing the running job before closing…")
        except RuntimeError:            # widgets can already be gone during shutdown
            pass
        event.ignore()

    def _env_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.env_form = QFormLayout()
        box = QGroupBox("Detected stack")
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

    def refresh_env(self) -> None:
        while self.env_form.rowCount():
            self.env_form.removeRow(0)
        try:
            self.env = detect()
        except EnvironmentError_ as exc:
            self.env = None
            self.env_note.setText(f"Not detected: {exc}")
            return
        for key, value in self.env.describe().items():
            self.env_form.addRow(key.replace("_", " "), QLabel(value))
        missing = msi_mod.missing_tools()
        self.env_note.setText(
            "msitools missing: " + ", ".join(missing) + "  ->  sudo pacman -S msitools"
            if missing
            else "msitools present. Wine tree, prefix and plugin directories all resolved."
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
        self.cb_purge = QCheckBox("purge registry")
        self.cb_purge.setToolTip(
            "After removing the files, also delete the registry entries that point at them\n"
            "(and the product's own Uninstall entry). Leaves nothing pointing at the files,\n"
            "so 'wpt scan' stops calling it a broken install. It cannot free an activation:\n"
            "deactivate first, or use 'Report as Unusable' in iLok License Manager."
        )
        bar.addWidget(self.cb_purge)
        self.cb_all_exes = QCheckBox("show other .exe files")
        self.cb_all_exes.setToolTip(
            "Also list executables in Program Files that no plugin MSI describes — Wine's own\n"
            "tools (iexplore, wordpad, wmplayer) and helpers other vendors install there.\n"
            "They are not plugins, so they are hidden by default."
        )
        self.cb_all_exes.stateChanged.connect(lambda _state: self.plugins_done(self.inv) if self.inv else None)
        bar.addWidget(self.cb_all_exes)
        bar.addStretch(1)
        layout.addLayout(bar)

        self.plugin_hint = QLabel(
            "Tick a row to select it, then use the buttons above — or <b>right-click a row</b> for repair, "
            "uninstall, enable/disable, the file's path and this plugin's preset sites."
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

        self.plugin_log = QPlainTextEdit()
        self.plugin_log.setReadOnly(True)
        self.plugin_log.setMaximumBlockCount(1000)
        layout.addWidget(self.plugin_log, 1)
        return page

    def refresh_plugins(self) -> None:
        if not self.env:
            QMessageBox.warning(self, "No environment", "Wine prefix not detected.")
            return
        self.plugin_summary.setText("Reading the prefix…")
        self._spawn(
            lambda emit: inventory_mod.build(self.env),
            on_done=self.plugins_done,
            on_failed=lambda msg: self.plugin_summary.setText(f"Inventory failed: {msg}"),
            log=self.plugin_log,
            label="inventory",
        )

    def plugins_done(self, inv) -> None:
        self.inv = inv
        self.plugin_table.setRowCount(0)
        show_all = getattr(self, "cb_all_exes", None) is not None and self.cb_all_exes.isChecked()
        rows_source = inv.entries if show_all else inv.plugins
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
        """Right-click a plugin row: the same actions as the buttons, and the same shape as the
        Download Plugins menu, so the two tabs behave alike."""
        row = self.plugin_table.rowAt(position.y())
        if row < 0:
            return
        self.plugin_table.selectRow(row)
        cell = self.plugin_table.item(row, 0)
        entry = cell.data(Qt.ItemDataRole.UserRole) if cell else None
        if entry is None:
            return

        menu = QMenu(self)
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
        copy_name = menu.addAction("Copy plugin name")
        copy_name.triggered.connect(lambda: self._copy_to_clipboard(entry.name))
        copy_path = menu.addAction("Copy file path")
        copy_path.triggered.connect(lambda: self._copy_to_clipboard(str(entry.path)))
        if entry.msi is not None:
            copy_msi = menu.addAction(f"Copy the MSI it came from ({entry.msi.name})")
            copy_msi.triggered.connect(lambda: self._copy_to_clipboard(str(entry.msi)))
        reveal = menu.addAction("Show in file manager")
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
        menu.exec(self.plugin_table.viewport().mapToGlobal(position))

    def _reveal(self, path: Path) -> None:
        """Open the containing folder in his desktop's file manager."""
        folder = path if path.is_dir() else path.parent
        opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        self.plugin_log.appendPlainText(
            f"opened {folder} in the file manager" if opened else f"could not open a file manager for {folder}"
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
        """Remove the whole product: clear the msiexec registration and delete the MSI's files."""
        entry = self._selected_entry()
        if not entry or not entry.msi or not self.env:
            return
        msi_path = entry.msi
        ident = msi_mod.identity(msi_path)
        label = ident.label
        if not ident.product_code:
            QMessageBox.warning(self, "No ProductCode", f"{msi_path.name} has no ProductCode to uninstall.")
            return
        registered = scan_mod.is_registered(self.env, ident.product_code)
        purge = self.cb_purge.isChecked()
        answer = QMessageBox.question(
            self,
            "Uninstall product",
            f"Remove {label} completely?\n\n"
            + (
                f"Registered with Windows Installer: msiexec /x {ident.product_code} will run.\n"
                if registered
                else "Not registered with Windows Installer in this prefix, so msiexec has nothing "
                "to remove and the files go directly.\n"
            )
            + f"Files: every file {msi_path.name} placed will be deleted — VST3, VST2, AAX, "
            "standalone and the factory presets it lists.\n\nYour own presets and any downloaded "
            "packs are copied to ~/.local/share/wpt/presets/ first, so they survive either way."
            + (
                "\n\nREGISTRY PURGE is on and will also remove the product's registry entries, so "
                "nothing is left pointing at the deleted files.\nIt does NOT free an activation: if "
                "this plugin was activated here and you are done with this prefix, deactivate it in "
                "iLok License Manager first — or use 'Report as Unusable' there if the location is "
                "unreachable — otherwise the licence slot stays consumed."
                if purge
                else ""
            ),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.btn_uninstall.setEnabled(False)
        product_code = ident.product_code

        def job(emit, msi_path, product_code, purge):
            # READ FIRST, MSIEXEC SECOND. `msiexec /x` deletes Windows Installer's own cached
            # copy of the package, and that cache is often the only copy these products have, so
            # reading the file list afterwards fails *after* the registration is gone (his Fortin
            # Cali Suite uninstall, 2026-09-26). The MSI is staged out of reach, and an unreadable
            # MSI stops here — before anything is touched.
            try:
                msi_path = msi_mod.stage_msi(msi_path, SCRATCH)
                emit(f"reading {msi_path.name} before msiexec runs")
                msi_mod.extract(msi_path, SCRATCH)
                plan = build_plan(msi_path, self.env, SCRATCH, include_aax=True)
            except (OSError, msi_mod.MsiError) as exc:
                raise RuntimeError(
                    f"cannot read {msi_path.name} for its file list ({exc}) — nothing was removed"
                ) from exc
            emit(f"this MSI describes {len(plan.actions)} destination(s)")

            emit(f"msiexec /x {product_code}")
            code, detail = uninstall_product(self.env, product_code)
            if detail:
                emit(detail)
            emit(f"msiexec exited {code}" + ("" if code == 0 else " (no registration to clear?)"))

            # presets first, always: the MSI's own files come back with a reinstall,
            # the user's own and downloaded packs do not
            saved, rescue_dir = presets_mod.rescue(self.env, plan.identity.product_name or "")
            if saved:
                emit(f"presets: {len(saved)} file(s) rescued to {rescue_dir}")
            else:
                emit("presets: none found for this product")

            rows = installer_mod.remove_files(plan, self.env)
            remaining = installer_mod.leftovers(plan, self.env)
            purge_rows = []
            if purge:
                edits = installer_mod.stale_registry_edits(self.env, plan, product_code)
                emit(f"purge: {len(edits)} registry entr{'y' if len(edits) == 1 else 'ies'} to remove")
                for edit in edits:
                    emit(f"    {edit.hive}\\{edit.key}" + (f"  [{edit.value}]" if edit.value else ""))
                    emit(f"        {edit.reason}")
                purge_rows = installer_mod.purge_registry(self.env, edits) if edits else []
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
        )

    def uninstall_done(self, result) -> None:
        rows, remaining, purge_rows = result
        for status, path, note in rows:
            self.plugin_log.appendPlainText(f"{status}: {path} {note}".strip())
        if not rows:
            self.plugin_log.appendPlainText("nothing to remove: no file this MSI describes is on disk")
        if remaining:
            self.plugin_log.appendPlainText(f"! {len(remaining)} path(s) still on disk:")
            for path in remaining:
                self.plugin_log.appendPlainText(f"    {path}")
        else:
            self.plugin_log.appendPlainText("removed every file this MSI placed")
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
        if not entry or not entry.msi:
            return
        self.btn_repair.setEnabled(False)
        msi_path = entry.msi

        def job(emit, msi_path):
            emit(f"extracting {msi_path.name}")
            msi_mod.extract(msi_path, SCRATCH)
            plan = build_plan(msi_path, self.env, SCRATCH)
            todo = filter_needing_repair(plan)
            if not todo.actions:
                return 0, 0, []
            rows = apply_plan(todo, dry_run=False)
            bad = [c for c in verify_plan(todo) if c[0] != "ok"]
            return len(rows), len(bad), rows

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
        self.install_table.setHorizontalHeaderLabels(["Status", "Path", "Detail"])
        _fit_columns(self.install_table, stretch={1: 300, 2: 220}, contents=(0,), elide={1: True})
        layout.addWidget(self.install_table, 2)

        self.install_log = QPlainTextEdit()
        self.install_log.setReadOnly(True)
        self.install_log.setMaximumBlockCount(2000)
        layout.addWidget(self.install_log, 1)
        return page

    def browse_msi(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select the vendor MSI", str(Path.home()), "MSI (*.msi)")
        if path:
            self.msi_combo.insertItem(0, path)
            self.msi_combo.setCurrentIndex(0)

    def find_msis(self) -> None:
        if not self.env:
            self.log("no environment detected")
            return
        self.msi_combo.clear()
        found = [
            path
            for path in msi_mod.find_extracted_msis(self.env.prefix, include_installer_cache=True)
            if msi_mod.declares_plugin_payload(path)
        ]
        for path in found:
            self.msi_combo.addItem(str(path))
        self.log(f"{len(found)} MSI(s) found inside {self.env.prefix}")

    def log(self, message: str) -> None:
        self.install_log.appendPlainText(message)

    def run_install(self, dry_run: bool) -> None:
        if not self.env:
            QMessageBox.warning(self, "No environment", "Wine prefix not detected. See the Environment tab.")
            return
        if not self.msi_combo.currentText():
            QMessageBox.warning(self, "No MSI", "Pick an installer MSI first.")
            return
        self.install_table.setRowCount(0)
        self._busy(True)
        msi_path = Path(self.msi_combo.currentText())

        def job(emit, msi_path: Path, dry_run: bool):
            emit(f"extracting {msi_path.name} -> {SCRATCH}")
            msi_mod.extract(msi_path, SCRATCH)
            plan = build_plan(
                msi_path,
                self.env,
                SCRATCH,
                include_vst2=self.cb_vst2.isChecked(),
                include_aax=self.cb_aax.isChecked(),
                include_standalone=self.cb_standalone.isChecked(),
                include_presets=self.cb_presets.isChecked(),
            )
            return plan, apply_plan(plan, dry_run=dry_run)

        self._spawn(
            job, msi_path, dry_run,
            on_line=self.log,
            on_done=lambda result: self.install_done(result, dry_run),
            on_failed=lambda msg: (self.install_failed(msg), self.refresh_plugins()),
            log=self.log,
            label="install",
        )

    def _log_plan_warnings(self, plan, log) -> None:
        """Surface what the plan refused to do, so 0 destinations never reads as success."""
        for warning in plan.warnings:
            log(f"! {warning}")
        if not plan.actions:
            log("nothing will be placed: this package is an application or a driver, not a plugin"
                if plan.skipped_app else
                "nothing to place: this MSI declares no payload this toolkit recognises")

    def install_done(self, result, dry_run: bool) -> None:
        plan, rows = result
        self.plan = plan
        for status, path, note in rows:
            row = self.install_table.rowCount()
            self.install_table.insertRow(row)
            self.install_table.setItem(row, 0, QTableWidgetItem(status))
            self.install_table.setItem(row, 1, QTableWidgetItem(path))
            self.install_table.setItem(row, 2, QTableWidgetItem(note))

        self._log_plan_warnings(plan, self.log)

        if dry_run:
            self.log(f"preview: {len(rows)} destinations, {plan.total_bytes() / 1e6:.0f} MB payload. Nothing written.")
        else:
            from .installer import verify_plan

            checks = verify_plan(plan)
            ok = sum(1 for c in checks if c[0] == "ok")
            bad = [c for c in checks if c[0] != "ok"]
            self.log(f"verified {ok} files byte-for-byte against the MSI File table")
            for status, path, note in bad:
                self.log(f"  {status}: {path} ({note})")
            self.log("done - rescan plugins in your DAW")
        self._busy(False)

    def install_failed(self, message: str) -> None:
        self.log(f"FAILED: {message}")
        QMessageBox.critical(self, "Install failed", message)
        self._busy(False)

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
        self.btn_download_install = QPushButton("Install downloaded installer")
        self.btn_download_install.setEnabled(False)
        self.btn_download_install.setToolTip(
            "Runs the selected plugin's installer: the vendor .exe is bridged to its MSI (running it\n"
            "under Wine if that is what it takes), then the payload is placed and verified. One at a time."
        )
        self.btn_download_install.clicked.connect(self.install_downloaded)
        bar.addWidget(self.btn_download_install)
        self.cb_watch = QCheckBox("watch ~/Downloads")
        self.cb_watch.setChecked(True)
        self.cb_watch.setToolTip("Notice a finished download and offer to install it, without leaving this tab.")
        bar.addWidget(self.cb_watch)
        bar.addStretch(1)
        layout.addLayout(bar)

        # ------------------------------------------------------------------ preset sources
        # The toolkit installs plugins; it does not fetch presets — several of these sites need
        # a sign-in or sit behind a bot filter, so the honest thing is to open the right page in
        # *his* browser, pre-filled with the plugin he has selected.
        presets_bar = QHBoxLayout()
        presets_bar.addWidget(QLabel("Preset & IR sources:"))
        self.source_combo = QComboBox()
        for source in sources_mod.SOURCES:
            self.source_combo.addItem(f"{source.name}  ({source.kind})", source.name)
        self.source_combo.setMinimumWidth(320)
        self.source_combo.currentIndexChanged.connect(lambda index: self._source_changed(index))
        presets_bar.addWidget(self.source_combo)
        self.btn_source_note = QLabel("")
        self.btn_source_note.setWordWrap(True)
        self.btn_source_note.setStyleSheet("color: palette(mid);")
        presets_bar.addWidget(self.btn_source_note, 1)
        self.btn_source_open = QPushButton("Open in browser")
        self.btn_source_open.setToolTip(
            "Opens the selected source in your own browser — nothing is downloaded by the toolkit.\n"
            "If a plugin is selected in the table, sources that support searching open already\n"
            "searching for it."
        )
        self.btn_source_open.clicked.connect(self.open_source)
        presets_bar.addWidget(self.btn_source_open)
        layout.addLayout(presets_bar)

        self.download_summary = QLabel(
            "Neural DSP installers. Pick a plugin, open its page, download it, then install it from here —\n"
            "the .exe → .msi step is handled for you."
        )
        self.download_summary.setWordWrap(True)
        layout.addWidget(self.download_summary)

        self.download_hint = QLabel(
            "Click a plugin, then <b>Download Selected Plugin</b> to fetch it (their plugin links need you "
            "signed in) — or <b>Browse Plugins In Browser</b> for their full downloads list. "
            "<b>Right-click a row</b> for the same actions plus the preset &amp; IR sites. Any installer "
            "that lands in ~/Downloads is offered here to install."
        )
        self.download_hint.setWordWrap(True)
        self.download_hint.setTextFormat(Qt.TextFormat.RichText)
        # no stylesheet: `palette(mid)` is near-invisible on his dark theme (he reported the
        # instructions as "hidden"), so this uses the normal window text colour
        layout.addWidget(self.download_hint)

        self.download_table = QTableWidget(0, 5)
        self.download_table.setHorizontalHeaderLabels(["Product", "Version", "Released", "In the prefix", "Installer"])
        # the installer column is the longest text in this table, so it gets as much room as the
        # product column rather than whatever Product left over
        _fit_columns(self.download_table, stretch={0: 220, 4: 320}, contents=(1, 2, 3), elide={4: True})
        self.download_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.download_table.itemSelectionChanged.connect(self._download_selection_changed)
        self.download_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.download_table.customContextMenuRequested.connect(self._download_context_menu)
        layout.addWidget(self.download_table, 3)

        self.download_log = QPlainTextEdit()
        self.download_log.setReadOnly(True)
        self.download_log.setMaximumBlockCount(1000)
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
        QTimer.singleShot(200, lambda: self.load_catalogue(refresh=False))
        return page

    def load_catalogue(self, refresh: bool = False) -> None:
        catalogue = catalogue_mod.load_snapshot()
        if refresh:
            self.download_summary.setText("Reading neuraldsp.com/downloads …")
            catalogue = catalogue_mod.refresh(log=lambda message: self.download_log.appendPlainText(message))
        self._catalogue = catalogue
        self.refresh_downloads()

    def _registered_names(self) -> set[str]:
        """Product names this prefix knows: from MSIs, and from what is actually on disk.

        MSI names alone are not enough — a toolkit-placed install need not leave an MSI behind at
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
        """'', 'installed', or 'files only' — a toolkit-placed install has no MSI to prove it."""
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

    def refresh_downloads(self) -> None:
        if not self.env:
            return
        keep = self._selected_product_key()
        # what is already in this prefix: MSI product names, plus what is on disk
        self._msi_names = set()
        for msi_path in msi_mod.find_extracted_msis(self.env.prefix, include_installer_cache=True):
            try:
                name = msi_mod.identity(msi_path).product_name
            except Exception:  # noqa: BLE001 - one unreadable MSI must not break the tab
                continue
            if name:
                self._msi_names.add(_key(name))
        self._installed_products = self._registered_names()

        # what has been downloaded, and whether it matches a catalogue entry
        downloads = Path.home() / "Downloads"
        self._downloads = {}
        pending = installers_mod.discover(self.env, extra_dirs=[downloads]) if downloads.is_dir() else []
        for item in pending:
            self._downloads[_key(item.product)] = item.path

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

        self._restore_selection(keep)
        stamp = self._catalogue.fetched or "bundled snapshot"
        installed_here = sum(1 for r in rows if _key(r.product) in self._installed_products)
        self.download_summary.setText(
            f"{len(rows)} releases from Neural DSP ({stamp})   ·   {ready} with an installer already in "
            f"~/Downloads   ·   {installed_here} of them installed in this prefix"
        )

    def _downloads_for(self, release) -> Path | None:
        """Match a catalogue entry to a downloaded file — strictly.

        `match_download` lives at module level so it can be tested without a real ~/Downloads:
        the first version matched on *any* shared token, which offered him the Nano Cortex
        installer for Quad Cortex and for Cortex Control (seen in a rendered screenshot,
        2026-09-26). A wrong match here is not cosmetic — "Install downloaded installer" would
        install the wrong product.
        """
        downloads = Path.home() / "Downloads"
        if not downloads.is_dir():
            return None
        return match_download(release.product, [*downloads.glob("*.exe"), *downloads.glob("*.msi")])

    def _download_candidates(self) -> set[Path]:
        """The installer files sitting in ~/Downloads — cheap enough to check on a timer."""
        downloads = Path.home() / "Downloads"
        if not downloads.is_dir():
            return set()
        return {p for p in downloads.glob("*.exe")} | {p for p in downloads.glob("*.msi")}

    def _watch_tick(self) -> None:
        """Notice a freshly downloaded installer — and otherwise leave the table alone.

        This used to call `refresh_downloads()` every four seconds, which cleared and rebuilt
        every row: the selected row lost its highlight mid-click, and "Open download page" then
        found nothing selected and did nothing (his report, 2026-09-26: *"the highlight disappears
        at times... it either does nothing or opens it, result changes randomly"*). Now the tick
        only acts when the set of files in ~/Downloads actually changes.
        """
        if not self.cb_watch.isChecked() or not self.env:
            return
        candidates = self._download_candidates()
        if candidates == self._seen_downloads:
            return
        self._seen_downloads = candidates
        before = dict(self._downloads)
        self.refresh_downloads()
        for key, path in self._downloads.items():
            if key not in before:
                self.download_log.appendPlainText(f"found a new installer: {path}")
                self.download_log.appendPlainText("  select it and press Install downloaded installer")

    def _download_context_menu(self, position) -> None:
        """Right-click a row: the same actions as the buttons, plus the preset sources.

        Right-clicking also *selects* the row under the cursor — the buttons act on the selection,
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
        menu.exec(self.download_table.viewport().mapToGlobal(position))

    def _build_download_menu(self, release, installer: Path | None) -> QMenu:
        """The row menu, built separately so it can be inspected without a modal exec()."""
        menu = QMenu(self)
        open_action = menu.addAction("Download Selected Plugin (in browser)")
        open_action.triggered.connect(lambda: self.download_selected_plugin(release))

        if installer:
            install_action = menu.addAction(f"Install {installer.name}")
            install_action.triggered.connect(self.install_downloaded)
        else:
            hint = menu.addAction("No installer downloaded yet")
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
            f"opened {name} for {product}: {url}" if opened else f"could not open a browser — {url}"
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

    def _download_selection_changed(self) -> None:
        release, installer = self._selected_release()
        self.btn_download_install.setEnabled(bool(installer) and self.env is not None)

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
        """The plugin selected in the catalogue table, if any — that is what gets searched."""
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
            f"opened {url}{where}" if opened else f"could not open a browser — the link is: {url}"
        )

    def browse_plugins_in_browser(self) -> None:
        """The vendor's own downloads index — every plugin link in one place."""
        url = catalogue_mod.DOWNLOADS_URL
        opened = QDesktopServices.openUrl(QUrl(url))
        self.download_log.appendPlainText(
            f"opened the full downloads list: {url}" if opened
            else f"could not open a browser — the link is: {url}"
        )

    def download_selected_plugin(self, release=None) -> None:
        """Open the vendor's page for a plugin — in the user's own browser.

        `release` is passed by the context menu; the button connection goes through a lambda
        because Qt hands a `clicked` slot a `checked` bool, which this used to accept as the
        release and then crash on (`'bool' object has no attribute 'windows'` — his traceback,
        2026-09-26). The isinstance guard is the belt to that lambda's braces.
        

        Saying so when there is no selection matters: this used to return silently, which is
        indistinguishable from a broken button (his report: *"it either does nothing or opens
        it"* — the four-second table rebuild was clearing the selection underneath the click).
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
            else f"could not open a browser — the link is: {url}"
        )
        if release.needs_sign_in:
            self.download_log.appendPlainText(
                "  sign in if it asks - the download lands in ~/Downloads and this tab picks it up"
            )

    def install_downloaded(self) -> None:
        release, installer = self._selected_release()
        if installer is None or not self.env:
            return
        answer = QMessageBox.question(
            self,
            "Install plugin",
            f"Install {release.product} from\n{installer.name}?\n\n"
            "If that is the vendor's .exe, its MSI is extracted first — if it has to be run under Wine "
            "to produce one, a vendor installer window will open and may take a few minutes. "
            "Installers are handled one at a time.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.btn_download_install.setEnabled(False)
        self._busy(True)

        def job(emit, installer):
            emit(f"{installer.name}: working out how to get an MSI out of it ...")
            prepared = wrappers_mod.prepare_msi(
                self.env,
                installer,
                SCRATCH,
                product_hint=release.product,
                allow_wine=True,
                log=emit,
            )
            if not prepared.ok:
                raise RuntimeError(prepared.detail)
            emit(f"MSI: {prepared.msi}")
            msi_mod.extract(prepared.msi, SCRATCH)
            plan = build_plan(prepared.msi, self.env, SCRATCH)
            rows = apply_plan(plan, dry_run=False)
            bad = [c for c in verify_plan(plan) if c[0] != "ok"]
            return plan, rows, bad

        self._spawn(
            job, installer,
            on_line=self.download_log.appendPlainText,
            on_done=self.install_downloaded_done,
            on_failed=lambda msg: (self.install_downloaded_failed(msg), self.refresh_plugins()),
            log=self.download_log,
            label="install",
        )

    def install_downloaded_done(self, result) -> None:
        plan, rows, bad = result
        self._log_plan_warnings(plan, self.download_log.appendPlainText)
        for status, path, note in rows:
            self.download_log.appendPlainText(f"{status}: {path} {note}".strip())
        self.download_log.appendPlainText(
            f"{len(rows)} destination(s) written, {len(bad)} failing verification"
            + ("" if not bad else ": " + ", ".join(p for _, p, _ in bad))
        )
        self.download_log.appendPlainText("rescan plugins in your DAW for the new plugin to appear")
        self._busy(False)
        self.btn_download_install.setEnabled(True)
        self.refresh_downloads()
        self.refresh_plugins()

    def install_downloaded_failed(self, message: str) -> None:
        self.download_log.appendPlainText(f"FAILED: {message}")
        QMessageBox.critical(self, "Install failed", message)
        self._busy(False)
        self.btn_download_install.setEnabled(True)

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
        self.pending_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.pending_table.itemSelectionChanged.connect(self._pending_selection_changed)
        layout.addWidget(self.pending_table, 3)

        self.pending_log = QPlainTextEdit()
        self.pending_log.setReadOnly(True)
        self.pending_log.setMaximumBlockCount(1000)
        layout.addWidget(self.pending_log, 1)
        return page

    def refresh_pending(self) -> None:
        if not self.env:
            QMessageBox.warning(self, "No environment", "Wine prefix not detected.")
            return
        self.pending_summary.setText("Looking for installers…")
        self._spawn(
            lambda emit: installers_mod.discover(self.env),
            on_done=self.pending_done,
            on_failed=lambda msg: self.pending_summary.setText(f"Discovery failed: {msg}"),
            log=self.pending_log,
            label="discovery",
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
        if not installer or not self.env:
            return
        # a downloaded .exe is the vendor's wrapper around an MSI: bridge the two here,
        # running the wrapper under Wine only when that is the only way to get its MSI
        answer = QMessageBox.question(
            self,
            "Install plugin",
            f"Install {installer.product} from {installer.path.name}?\n\n"
            "If that is the vendor's .exe, its MSI is extracted first — and if the wrapper has to be "
            "run under Wine to produce one, a vendor installer window will open and may take a few "
            "minutes. One plugin at a time.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.pending_log.appendPlainText(f"installing {installer.product} from {installer.path.name}")
        self.btn_install_pending.setEnabled(False)

        def job(emit, installer):
            prepared = wrappers_mod.prepare_msi(
                self.env,
                installer.path,
                SCRATCH,
                product_hint=installer.product,
                allow_wine=True,
                log=emit,
            )
            if not prepared.ok:
                raise RuntimeError(prepared.detail)
            emit(f"MSI: {prepared.msi}")
            msi_mod.extract(prepared.msi, SCRATCH)
            plan = build_plan(prepared.msi, self.env, SCRATCH)
            rows = apply_plan(plan, dry_run=False)
            bad = [c for c in verify_plan(plan) if c[0] != "ok"]
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
        for warning in plan.warnings:
            self.pending_log.appendPlainText(f"! {warning}")
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
        self.scan_table.setHorizontalHeaderLabels(["Status", "Registry path"])
        _fit_columns(self.scan_table, stretch={1: 320}, contents=(0,), elide={1: True})
        layout.addWidget(self.scan_table, 1)

        self.scan_products = QPlainTextEdit()
        self.scan_products.setReadOnly(True)
        self.scan_products.setMaximumBlockCount(500)
        layout.addWidget(self.scan_products, 1)
        return page

    def run_scan(self) -> None:
        if not self.env:
            QMessageBox.warning(self, "No environment", "Wine prefix not detected.")
            return
        self.scan_summary.setText("Scanning…")
        self._spawn(
            lambda emit: scan_prefix(self.env),
            on_done=self.scan_done,
            on_failed=lambda msg: self.scan_summary.setText(f"Scan failed: {msg}"),
            log=self.scan_products,
            label="scan",
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
        # runtimes and services carry a DisplayName too; listing eighty of them buries
        # the four products anyone is actually looking for
        products = [p for p in report.products if not scan_mod.is_system_product(p.get("DisplayName", ""))]
        hidden = len(report.products) - len(products)
        lines = [f"{p.get('DisplayName', '?')}  {p.get('DisplayVersion', '?')}" for p in products]
        if hidden:
            lines.append(f"({hidden} runtime/system entry/entries hidden - 'wpt scan --all-products' shows them)")
        self.scan_products.setPlainText("\n".join(lines) or "no registered products found")

    def run_triage(self) -> None:
        self.scan_summary.setText("Triage: reading every Wine prefix on this machine…")
        self._spawn(
            lambda emit: products_mod.render(products_mod.triage(self.env)),
            on_done=self.triage_done,
            on_failed=lambda msg: self.scan_summary.setText(f"Triage failed: {msg}"),
            log=self.scan_products,
            label="triage",
        )

    def triage_done(self, text: str) -> None:
        self.scan_products.setPlainText(text)
        self.scan_summary.setText(
            "Triage complete - the pane below lists every prefix found and each product's verdict."
        )


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Wine Plugin Toolkit")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
