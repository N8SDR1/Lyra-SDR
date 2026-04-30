"""Captured-noise-profile management dialog (Phase 3.D #1, Day 3).

Operator-facing window for the saved noise profiles on disk.  Lists
profiles in a table with name / band-mode / captured-at / duration
columns, with action buttons for Use / Re-capture / Rename / Delete
/ Export / Import / Close.

Lives off the main thread (modal dialog).  Talks to Radio via the
captured-profile API added in Day 2.5; no direct file I/O — the
underlying noise_profile_store module is the single source of
truth for profile JSON.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QSettings
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


# Bands lookup is best-effort — if Lyra's band table isn't importable
# (e.g. early development), we just show the freq in MHz.
def _band_for_freq_hz(freq_hz: int) -> str:
    try:
        from lyra.bands import band_for_freq_hz as _b
        return _b(int(freq_hz)) or ""
    except Exception:
        return ""


def _format_band_mode(meta) -> str:
    """Human-friendly band/mode column: '80m LSB', '40m USB', etc.,
    falling back to '<freq MHz> <mode>' if we can't resolve a band."""
    band = _band_for_freq_hz(meta.freq_hz)
    if band:
        return f"{band} {meta.mode}".strip()
    if meta.freq_hz > 0:
        return f"{meta.freq_hz / 1e6:.3f} MHz {meta.mode}".strip()
    return meta.mode or "—"


def _format_captured_at(meta, *,
                       amber_hours: int,
                       red_days: int) -> tuple[str, str]:
    """Returns (display_string, age_color_hex).

    Color rule:
    - <amber_hours: grey  (#cdd9e5)
    - amber_hours .. red_days*24h: amber (#ffb84a)
    - >red_days: red (#ff6060)
    """
    dt = meta.captured_at_datetime()
    if dt is None:
        return ("—", "#7a8a9c")
    # Always show in local time for the operator's reading sake;
    # captured_at_iso is stored in UTC so we convert.
    local = dt.astimezone()
    display = local.strftime("%Y-%m-%d %H:%M")
    # Age color.
    now = datetime.now(timezone.utc)
    delta = now - dt
    hours = delta.total_seconds() / 3600.0
    if hours > red_days * 24:
        return (display, "#ff6060")
    if hours > amber_hours:
        return (display, "#ffb84a")
    return (display, "#cdd9e5")


class NoiseProfileManager(QDialog):
    """Manage captured noise profiles — list, use, rename, delete,
    re-capture, export, import."""

    COL_NAME = 0
    COL_BAND_MODE = 1
    COL_CAPTURED = 2
    COL_DURATION = 3

    def __init__(self, radio, parent=None):
        super().__init__(parent)
        self.radio = radio
        self.setWindowTitle("Captured Noise Profiles")
        self.setMinimumSize(720, 460)
        self.resize(820, 520)

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)

        # Folder hint at the top so the operator can see where
        # profiles are coming from.  Click to open in OS explorer.
        self._folder_label = QLabel()
        self._folder_label.setStyleSheet(
            "color: #8a9aac; font-family: Consolas, monospace; "
            "font-size: 10px;")
        self._folder_label.setTextInteractionFlags(
            Qt.TextSelectableByMouse)
        v.addWidget(self._folder_label)

        # Profile list (QTableWidget for column structure).
        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(
            ["Name", "Band / Mode", "Captured", "Duration"])
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(
            QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(self.COL_NAME, QHeaderView.Stretch)
        hdr.setSectionResizeMode(
            self.COL_BAND_MODE, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(
            self.COL_CAPTURED, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(
            self.COL_DURATION, QHeaderView.ResizeToContents)
        # Double-click on a row = "Use Selected".
        self.table.itemDoubleClicked.connect(
            lambda _it: self._on_use_selected())
        self.table.itemSelectionChanged.connect(
            self._refresh_button_states)
        v.addWidget(self.table, 1)

        # Action button row.
        btns = QHBoxLayout()
        btns.setSpacing(6)
        self.btn_use = QPushButton("Use Selected")
        self.btn_use.clicked.connect(self._on_use_selected)
        btns.addWidget(self.btn_use)
        self.btn_recapture = QPushButton("Re-capture")
        self.btn_recapture.setToolTip(
            "Discard the selected profile's stored magnitudes and "
            "start a new capture under the same name.  Useful when "
            "band conditions have shifted but the profile name "
            "(e.g. 'Powerline 80m') still describes the noise.")
        self.btn_recapture.clicked.connect(self._on_recapture)
        btns.addWidget(self.btn_recapture)
        self.btn_rename = QPushButton("Rename")
        self.btn_rename.clicked.connect(self._on_rename)
        btns.addWidget(self.btn_rename)
        self.btn_delete = QPushButton("Delete")
        self.btn_delete.clicked.connect(self._on_delete)
        btns.addWidget(self.btn_delete)
        btns.addStretch(1)
        self.btn_export = QPushButton("Export…")
        self.btn_export.setToolTip(
            "Save a single profile as a JSON file outside the "
            "Lyra storage folder — for sharing or backup.")
        self.btn_export.clicked.connect(self._on_export)
        btns.addWidget(self.btn_export)
        self.btn_import = QPushButton("Import…")
        self.btn_import.setToolTip(
            "Bring a profile JSON from outside the storage folder "
            "into Lyra.")
        self.btn_import.clicked.connect(self._on_import)
        btns.addWidget(self.btn_import)
        v.addLayout(btns)

        # Standard close button.
        bottom = QDialogButtonBox(QDialogButtonBox.Close)
        bottom.rejected.connect(self.reject)
        v.addWidget(bottom)

        # Auto-refresh on model changes.
        self.radio.noise_profiles_changed.connect(self._reload)
        self.radio.noise_active_profile_changed.connect(
            lambda _name: self._reload())

        self._reload()

    # ── List management ──────────────────────────────────────────

    def _reload(self) -> None:
        """Re-scan the profile folder and rebuild the table."""
        self._folder_label.setText(
            f"Folder: {self.radio.noise_profile_folder}")
        s = QSettings("N8SDR", "Lyra")
        amber_hours = int(s.value("noise/age_amber_hours", 24,
                                  type=int))
        red_days = int(s.value("noise/age_red_days", 7, type=int))

        metas = self.radio.list_saved_noise_profiles()
        active = self.radio.active_captured_profile_name
        current_fft = self.radio._rx_channel.nr_fft_size

        self.table.setRowCount(len(metas))
        for row, meta in enumerate(metas):
            # Name column — prefix a small dot when this is the
            # currently-loaded profile.
            display_name = meta.name
            if meta.name == active:
                display_name = f"●  {meta.name}"
            name_item = QTableWidgetItem(display_name)
            if meta.name == active:
                font = name_item.font()
                font.setBold(True)
                name_item.setFont(font)
                name_item.setForeground(QColor("#39ff14"))
            # Mark incompatible profiles with strikethrough font +
            # tooltip.  load_saved_noise_profile() will reject these
            # at load time; we also visually flag them.
            if not meta.is_compatible(current_fft):
                font = name_item.font()
                font.setStrikeOut(True)
                name_item.setFont(font)
                name_item.setForeground(QColor("#7a8a9c"))
                name_item.setToolTip(
                    f"Profile FFT size {meta.fft_size} doesn't match "
                    f"current NR config ({current_fft}).  Cannot be "
                    f"loaded.")
            self.table.setItem(row, self.COL_NAME, name_item)

            self.table.setItem(
                row, self.COL_BAND_MODE,
                QTableWidgetItem(_format_band_mode(meta)))

            cap_text, cap_color = _format_captured_at(
                meta, amber_hours=amber_hours, red_days=red_days)
            cap_item = QTableWidgetItem(cap_text)
            cap_item.setForeground(QColor(cap_color))
            self.table.setItem(row, self.COL_CAPTURED, cap_item)

            dur_text = (f"{meta.duration_sec:.1f} s"
                        if meta.duration_sec > 0 else "—")
            self.table.setItem(
                row, self.COL_DURATION, QTableWidgetItem(dur_text))

        self._refresh_button_states()

    def _selected_meta(self):
        """Return the ProfileMeta for the currently-selected row,
        or None if nothing is selected."""
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        idx = rows[0].row()
        metas = self.radio.list_saved_noise_profiles()
        if idx >= len(metas):
            return None
        return metas[idx]

    def _refresh_button_states(self) -> None:
        meta = self._selected_meta()
        has_sel = meta is not None
        compat = (has_sel
                  and meta.is_compatible(self.radio._rx_channel.nr_fft_size))
        self.btn_use.setEnabled(has_sel and compat)
        self.btn_recapture.setEnabled(has_sel)
        self.btn_rename.setEnabled(has_sel)
        self.btn_delete.setEnabled(has_sel)
        self.btn_export.setEnabled(has_sel)

    # ── Action handlers ──────────────────────────────────────────

    def _on_use_selected(self) -> None:
        meta = self._selected_meta()
        if meta is None:
            return
        try:
            self.radio.load_saved_noise_profile(meta.name)
        except Exception as exc:
            QMessageBox.warning(self, "Load failed", str(exc))
            return
        # Flip the NR source toggle to "captured" so the loaded
        # profile is actually USED — operators expect "Use Selected"
        # to mean "make this the active noise source too".  NR
        # profile (Light/Medium/Aggressive aggression) is left
        # alone — operator's preferred aggression continues to apply
        # to the new noise source.
        self.radio.set_nr_use_captured_profile(True)
        self.radio.status_message.emit(
            f"Active noise profile: {meta.name}", 4000)

    def _on_recapture(self) -> None:
        meta = self._selected_meta()
        if meta is None:
            return
        ans = QMessageBox.question(
            self, "Re-capture profile?",
            f"This will overwrite {meta.name!r} with a fresh capture "
            f"using the current band noise.\n\n"
            f"Tune to a noise-only frequency or wait for a "
            f"transmission gap before continuing.\n\n"
            f"Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        # Pull duration from settings.
        s = QSettings("N8SDR", "Lyra")
        duration = float(s.value("noise/capture_duration_sec", 2.0,
                                 type=float))
        # We also need to remember that on completion we want to
        # save under the existing name with overwrite=True.  The
        # cleanest way is to hook into noise_capture_done once.
        self._pending_recapture_name = meta.name
        self.radio.noise_capture_done.connect(
            self._on_recapture_done)
        self.radio.begin_noise_capture(duration)
        self.radio.status_message.emit(
            f"Re-capturing {meta.name!r} ({duration:.1f} s)…", 6000)

    def _on_recapture_done(self, verdict: str) -> None:
        # Disconnect first so subsequent unrelated captures don't
        # re-trigger this slot.
        try:
            self.radio.noise_capture_done.disconnect(
                self._on_recapture_done)
        except (TypeError, RuntimeError):
            pass
        name = getattr(self, "_pending_recapture_name", "")
        self._pending_recapture_name = ""
        if not name:
            return
        try:
            self.radio.save_current_capture_as(name, overwrite=True)
        except Exception as exc:
            QMessageBox.warning(self, "Re-capture save failed", str(exc))
            return
        msg = f"Re-captured {name!r}"
        if verdict == "suspect":
            msg += " ⚠ smart-guard flagged signal during capture"
        self.radio.status_message.emit(msg, 6000)

    def _on_rename(self) -> None:
        meta = self._selected_meta()
        if meta is None:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename profile",
            f"New name for {meta.name!r}:",
            text=meta.name)
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == meta.name:
            return
        try:
            self.radio.rename_saved_noise_profile(meta.name, new_name)
        except FileExistsError:
            QMessageBox.warning(
                self, "Rename failed",
                f"A profile named {new_name!r} already exists.  "
                f"Pick a different name or delete the existing one first.")
        except Exception as exc:
            QMessageBox.warning(self, "Rename failed", str(exc))

    def _on_delete(self) -> None:
        meta = self._selected_meta()
        if meta is None:
            return
        ans = QMessageBox.question(
            self, "Delete profile?",
            f"Permanently delete {meta.name!r}?\n\n"
            f"The JSON file will be removed from disk.  This cannot "
            f"be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            self.radio.delete_saved_noise_profile(meta.name)
        except Exception as exc:
            QMessageBox.warning(self, "Delete failed", str(exc))

    def _on_export(self) -> None:
        meta = self._selected_meta()
        if meta is None:
            return
        # Suggest a filename based on the profile name.
        from lyra.dsp.noise_profile_store import sanitize_filename
        suggested = sanitize_filename(meta.name) + ".json"
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export noise profile",
            str(Path.home() / suggested),
            "Noise profile (*.json);;All files (*.*)")
        if not path:
            return
        try:
            self.radio.export_saved_noise_profile(meta.name, Path(path))
            self.radio.status_message.emit(
                f"Exported {meta.name!r} → {path}", 5000)
        except Exception as exc:
            QMessageBox.warning(self, "Export failed", str(exc))

    def _on_import(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Import noise profile",
            str(Path.home()),
            "Noise profile (*.json);;All files (*.*)")
        if not path:
            return
        try:
            name = self.radio.import_saved_noise_profile(Path(path))
        except FileExistsError:
            ans = QMessageBox.question(
                self, "Overwrite existing?",
                "A profile with this name already exists in your "
                "Lyra storage folder.  Overwrite it?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return
            try:
                name = self.radio.import_saved_noise_profile(
                    Path(path), overwrite=True)
            except Exception as exc:
                QMessageBox.warning(
                    self, "Import failed", str(exc))
                return
        except Exception as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        self.radio.status_message.emit(
            f"Imported noise profile: {name}", 5000)
