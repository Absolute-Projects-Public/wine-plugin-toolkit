"""Small dialogs for creating and editing saved Wine launch profiles."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .launch_profiles import (
    LaunchProfile,
    ProfileConfigError,
    format_environment_text,
    parse_environment_text,
    profile_config_path,
)


class ProfileEditorDialog(QDialog):
    """Edit one profile; invalid input remains in the dialog with an inline explanation."""

    def __init__(self, profile: LaunchProfile | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit launch profile" if profile else "New launch profile")
        self.profile: LaunchProfile | None = None
        self._profile_id = profile.profile_id if profile else None


        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(profile.name if profile else "")
        self.name_edit.setPlaceholderText("e.g. Mantra / PipeASIO")
        form.addRow("Profile name", self.name_edit)

        self.prefix_edit = QLineEdit(profile.prefix if profile else "")
        self.prefix_browse = QPushButton("Browse…")
        prefix_row = self._path_row(self.prefix_edit, self.prefix_browse, "Choose Wine prefix")
        form.addRow("Wine prefix", prefix_row)

        self.tree_edit = QLineEdit(profile.wine_tree if profile else "")
        self.tree_browse = QPushButton("Browse…")
        tree_row = self._path_row(self.tree_edit, self.tree_browse, "Choose custom Wine tree")
        form.addRow("Custom Wine tree", tree_row)

        self.environment_edit = QPlainTextEdit()
        self.environment_edit.setPlaceholderText("PIPEWIRE_LATENCY=128/48000")
        self.environment_edit.setMinimumHeight(92)
        if profile:
            self.environment_edit.setPlainText(format_environment_text(dict(profile.environment)))
        form.addRow("Environment overrides", self.environment_edit)
        layout.addLayout(form)

        note = QLabel(
            f"Saved to {profile_config_path()}. Literal overrides affect Run in Standalone only; no shell expansion. "
            "The profile file uses mode 0600. Values are plain text, so do not put passwords or tokens here."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.error_note = QLabel("")
        self.error_note.setWordWrap(True)
        layout.addWidget(self.error_note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _path_row(edit: QLineEdit, button: QPushButton, title: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        layout.addWidget(button)
        button.clicked.connect(lambda *_args: ProfileEditorDialog._browse_directory(edit, title))
        return row

    @staticmethod
    def _browse_directory(edit: QLineEdit, title: str) -> None:
        current = edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(edit.window(), title, current)
        if chosen:
            edit.setText(chosen)

    def _save(self) -> None:
        try:
            profile = LaunchProfile(
                name=self.name_edit.text(),
                prefix=self.prefix_edit.text(),
                wine_tree=self.tree_edit.text(),
                environment=parse_environment_text(self.environment_edit.toPlainText()),
                profile_id=self._profile_id,
            )
        except (ProfileConfigError, TypeError) as exc:
            self.error_note.setText(str(exc))
            return
        self.profile = profile
        self.accept()


class LaunchProfilesDialog(QDialog):
    """Manage the saved profile list without writing until the user accepts the dialog."""

    def __init__(self, profiles: tuple[LaunchProfile, ...], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Manage launch profiles")
        self._profiles = list(profiles)
        self.profiles: tuple[LaunchProfile, ...] = tuple(profiles)

        layout = QVBoxLayout(self)
        hint = QLabel(
            "Each profile pins a prefix and Wine tree for WPT operations, including Run in Standalone. "
            "Environment overrides apply only to standalone launches."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        row = QHBoxLayout()
        self.profile_list = QListWidget()
        self.profile_list.currentRowChanged.connect(self._selection_changed)
        row.addWidget(self.profile_list, 1)

        actions = QVBoxLayout()
        self.btn_new = QPushButton("New…")
        self.btn_new.clicked.connect(lambda *_args: self._edit_profile(None))
        self.btn_edit = QPushButton("Edit…")
        self.btn_edit.clicked.connect(self._edit_selected)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.clicked.connect(self._remove_selected)
        actions.addWidget(self.btn_new)
        actions.addWidget(self.btn_edit)
        actions.addWidget(self.btn_remove)
        actions.addStretch(1)
        row.addLayout(actions)
        layout.addLayout(row, 1)

        self.error_note = QLabel("")
        self.error_note.setWordWrap(True)
        layout.addWidget(self.error_note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_profiles)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._rebuild_list()
        self.setMinimumSize(650, 410)

    def _rebuild_list(self, selected_name: str | None = None) -> None:
        self.profile_list.clear()
        for profile in self._profiles:
            item = QListWidgetItem(profile.name)
            item.setToolTip(f"Prefix: {profile.prefix}\nWine tree: {profile.wine_tree}")
            self.profile_list.addItem(item)
        if selected_name:
            for index in range(self.profile_list.count()):
                if self.profile_list.item(index).text() == selected_name:
                    self.profile_list.setCurrentRow(index)
                    break
        self._selection_changed(self.profile_list.currentRow())

    def _selection_changed(self, row: int) -> None:
        valid = 0 <= row < len(self._profiles)
        self.btn_edit.setEnabled(valid)
        self.btn_remove.setEnabled(valid)

    def _edit_selected(self) -> None:
        row = self.profile_list.currentRow()
        if 0 <= row < len(self._profiles):
            self._edit_profile(row)

    def _edit_profile(self, row: int | None) -> None:
        current = self._profiles[row] if isinstance(row, int) else None
        editor = ProfileEditorDialog(current, self)
        if editor.exec() != QDialog.DialogCode.Accepted or editor.profile is None:
            return
        updated = editor.profile
        if any(
            item.name.casefold() == updated.name.casefold()
            for index, item in enumerate(self._profiles)
            if index != row
        ):
            self.error_note.setText("Profile names must be unique (case-insensitive).")
            return
        if isinstance(row, int):
            self._profiles[row] = updated
        else:
            self._profiles.append(updated)
        self.error_note.clear()
        self._rebuild_list(updated.name)

    def _remove_selected(self) -> None:
        row = self.profile_list.currentRow()
        if 0 <= row < len(self._profiles):
            self._profiles.pop(row)
            self.error_note.clear()
            self._rebuild_list()

    def _accept_profiles(self) -> None:
        names = [profile.name.casefold() for profile in self._profiles]
        if len(names) != len(set(names)):
            self.error_note.setText("Profile names must be unique (case-insensitive).")
            return
        self.profiles = tuple(self._profiles)
        self.accept()
