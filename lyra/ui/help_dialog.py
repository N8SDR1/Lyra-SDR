"""In-app Help / User Guide viewer.

Loads markdown files from `docs/help/` and renders them in a dialog
with a topic list on the left and `QTextBrowser` on the right.

Features:
- **Edit-on-the-fly**: operator can edit a .md file in any editor while
  the app is running and hit Reload (or Ctrl+R) to re-render.
- **Topic discovery is file-driven**: drop a new `<slug>.md` into
  `docs/help/` and it appears in the tree on next reload. The first
  `# Heading` is used as the display name; files whose name starts
  with `_todo-` are labeled "(stub)".
- **Search**: QLineEdit filters the topic list to only topics whose
  file content contains the query (case-insensitive). Selecting a
  match highlights occurrences of the query inside the rendered view
  and jumps to the first hit.
- **Print**: any topic can be printed via `QPrintDialog`.
- **Cross-links**: `[label](./foo.md)` jumps topics; a custom
  `panel:<name>` scheme (e.g. `[see DSP panel](panel:dsp)`) tells the
  host window to flash the corresponding dock panel so the operator
  can find it at a glance.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)


def _help_root() -> Path:
    """Locate the `docs/help` folder.

    Two layouts:
    - Dev tree: walk up from this file looking for docs/help
    - PyInstaller frozen bundle: lyra.resource_root() returns
      sys._MEIPASS, where build/lyra.spec copied docs/ into the
      bundle.

    The dialog handles the "no topics found" case gracefully if the
    folder is missing for any reason (deployment misconfig, etc.).
    """
    from lyra import resource_root
    primary = resource_root() / "docs" / "help"
    if primary.is_dir():
        return primary
    # Walk-up fallback for unusual layouts (someone running Lyra
    # from a wonky relocated install).
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        candidate = parent / "docs" / "help"
        if candidate.is_dir():
            return candidate
    return primary  # missing-folder error path


def _topic_title(md_path: Path) -> str:
    """Pull the first `# Heading` line from a markdown file; fall back
    to the filename if the file has no H1."""
    try:
        with md_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        pass
    return md_path.stem.replace("-", " ").replace("_", " ").title()


class HelpDialog(QDialog):
    """Non-modal help window: topic list (left) + rendered content (right),
    with search / print / panel-highlight cross-links."""

    # Preferred ordering for the built-in topic set. Files not in this
    # list sort alphabetically after the listed ones; `_todo-*` stubs
    # always sort last.
    TOPIC_ORDER = [
        "index",
        "introduction",
        "getting-started",
        "tuning",
        "modes-filters",
        "agc",
        "notches",
        "nr",
        "apf",
        "spectrum",
        "smeter",
        "audio",
        "hardware",
        "tci",
        "shortcuts",
        "troubleshooting",
        # Support + License pinned at the END — they're not
        # operating-reference material; pinning them last keeps the
        # workflow-relevant topics at the top of the list where
        # operators expect them.
        "support",
        "license",
    ]

    # Emitted when user picks a topic (mostly useful for tests).
    topic_changed = Signal(str)
    # Emitted when a `panel:<name>` link is clicked — the host window
    # can use this to flash the matching dock panel for wayfinding.
    panel_highlight_requested = Signal(str)

    def __init__(self, parent=None, root: Path | None = None):
        super().__init__(parent)
        self.setWindowTitle("Lyra — User Guide")
        # Larger default window — most modern screens have room; the
        # resize handles still work for anyone who wants it smaller.
        self.resize(1200, 900)
        self.setMinimumSize(720, 480)
        self.setModal(False)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, False)

        self._root = root or _help_root()
        self._current_slug: str | None = None
        self._current_query: str = ""           # active search query
        # Cache of topic filesystem paths so search can rescan without
        # hitting disk for the title line repeatedly.
        self._topic_paths: list[Path] = []

        outer = QVBoxLayout(self)
        outer.setSpacing(6)

        # ── Top toolbar: search + print + reload ─────────────────────
        top = QHBoxLayout()
        top.setSpacing(6)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "Search the guide — type to filter topics & highlight matches")
        self.search_edit.setClearButtonEnabled(True)
        # Debounce text changes so we don't rescan every keystroke when
        # the user is typing quickly.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(self._apply_search)
        self.search_edit.textChanged.connect(
            lambda _=None: self._search_timer.start())
        top.addWidget(self.search_edit, 1)

        self.print_btn = QPushButton("Print…")
        self.print_btn.setFixedWidth(90)
        self.print_btn.setToolTip(
            "Print the current topic (Ctrl+P). Works with any installed"
            " printer or 'Microsoft Print to PDF' to save a copy.")
        self.print_btn.clicked.connect(self._on_print)
        top.addWidget(self.print_btn, 0)

        self.reload_btn = QPushButton("Reload")
        self.reload_btn.setFixedWidth(90)
        self.reload_btn.setToolTip(
            "Re-read all markdown files from docs/help/ (Ctrl+R).\n"
            "Edit .md files in any editor while Lyra is running and"
            " hit Reload to pick up changes instantly.")
        self.reload_btn.clicked.connect(self.reload)
        top.addWidget(self.reload_btn, 0)

        outer.addLayout(top)

        # ── Body: topic list + rendered view ─────────────────────────
        body = QHBoxLayout()
        body.setSpacing(6)

        # Left column: topic list + path note.
        # Width bounds: min 240, max 360 — was 220..300 which clipped
        # longer topic titles like "Noise Reduction (NR)" + the
        # vertical scrollbar gutter on first paint. 360 leaves comfy
        # room and `setTextElideMode(ElideRight)` is a safety net so
        # any future long title gets an ellipsis instead of being
        # silently chopped at the right edge.
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtGui import QFont as _QFont
        left = QVBoxLayout()
        self.topic_list = QListWidget()
        self.topic_list.setMinimumWidth(240)
        self.topic_list.setMaximumWidth(360)
        self.topic_list.setTextElideMode(_Qt.ElideRight)
        # Same +1.5 pt bump as the content view — keeps the topic
        # list and the content visually consistent when read together.
        list_font = _QFont(self.topic_list.font())
        list_font.setPointSizeF(list_font.pointSizeF() + 1.5)
        self.topic_list.setFont(list_font)
        self.topic_list.currentItemChanged.connect(self._on_topic_selected)
        left.addWidget(self.topic_list, 1)

        self._path_lbl = QLabel()
        self._path_lbl.setWordWrap(True)
        self._path_lbl.setStyleSheet(
            "color: #8a9aac; font-size: 9px; padding: 2px;")
        left.addWidget(self._path_lbl)

        left_container = QWidget()
        left_container.setLayout(left)
        body.addWidget(left_container, 0)

        # Right column: rendered content.
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        self.view.setOpenLinks(False)  # we handle every click manually
        self.view.anchorClicked.connect(self._on_anchor)
        # Bump the rendered-content font slightly for comfortable
        # reading (operator request). Default Qt size felt cramped
        # against the dense ham-radio reference material; +1.5 pt
        # gives noticeable breathing room without breaking layouts.
        # Applied via the view's font (not stylesheet) so headings,
        # tables, lists, and code blocks all scale proportionally.
        from PySide6.QtGui import QFont as _QFont
        body_font = _QFont(self.view.font())
        body_font.setPointSizeF(body_font.pointSizeF() + 1.5)
        self.view.setFont(body_font)
        body.addWidget(self.view, 1)

        outer.addLayout(body, 1)

        # Shortcuts
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self.reload)
        QShortcut(QKeySequence("Ctrl+P"), self, activated=self._on_print)
        QShortcut(QKeySequence("Ctrl+F"), self,
                  activated=lambda: (self.search_edit.setFocus(),
                                     self.search_edit.selectAll()))
        QShortcut(QKeySequence("F3"), self, activated=self._find_next)
        QShortcut(QKeySequence("Esc"), self, activated=self.close)

        self.reload()

    # ── Topic discovery & rendering ─────────────────────────────────
    def reload(self):
        """Re-scan docs/help/ for .md files and re-render the current
        topic. Safe to call any time; preserves current topic selection."""
        remembered = self._current_slug
        self.topic_list.blockSignals(True)
        self.topic_list.clear()

        self._path_lbl.setText(str(self._root))

        if not self._root.is_dir():
            self.view.setMarkdown(
                f"# Help folder not found\n\n"
                f"Expected: `{self._root}`\n\n"
                "Create that folder and drop `.md` files in it, then hit "
                "Reload.")
            self._topic_paths = []
            self.topic_list.blockSignals(False)
            return

        md_files = sorted(self._root.glob("*.md"))
        if not md_files:
            self.view.setMarkdown(
                f"# No help files found\n\n"
                f"Looked in: `{self._root}`\n\n"
                "Add `.md` files and hit Reload.")
            self._topic_paths = []
            self.topic_list.blockSignals(False)
            return

        self._topic_paths = self._sort_topics(md_files)
        self._populate_list(self._topic_paths, match_counts=None)
        self.topic_list.blockSignals(False)

        # Re-apply any active search (re-scans file contents too).
        if self._current_query:
            self._apply_search()

        # Restore previous selection or default to first topic.
        self._select_slug(remembered or "index")

    def _populate_list(self, paths, match_counts):
        """Populate the topic list. If `match_counts` is provided
        (dict: path → hit count), only entries with >0 are included and
        counts are shown in the label."""
        self.topic_list.clear()
        for path in paths:
            slug = path.stem
            title = _topic_title(path)
            if slug.startswith("_todo"):
                title = f"{title}  (stub)"
            if match_counts is not None:
                hits = match_counts.get(path, 0)
                if hits <= 0:
                    continue
                title = f"{title}   ({hits})"
            item = QListWidgetItem(title)
            item.setData(Qt.UserRole, str(path))
            self.topic_list.addItem(item)

    def _sort_topics(self, paths: Iterable[Path]) -> list[Path]:
        """Sort: TOPIC_ORDER first (in listed order), then remaining
        regular topics alphabetically, then `_todo-*` stubs at the end."""
        order_index = {name: i for i, name in enumerate(self.TOPIC_ORDER)}
        regular, stubs = [], []
        for p in paths:
            (stubs if p.stem.startswith("_todo") else regular).append(p)
        regular.sort(key=lambda p: (order_index.get(p.stem, 9999), p.stem))
        stubs.sort(key=lambda p: p.stem)
        return regular + stubs

    def _on_topic_selected(self, current, previous):
        if current is None:
            return
        path = Path(current.data(Qt.UserRole))
        self._render(path)

    # Template placeholders the help-markdown can use to embed live
    # app metadata. Keeps user-guide pages in sync with __version__
    # automatically — bumping lyra/__init__.py updates every doc page
    # that references {{ version }}, {{ version_full }}, or
    # {{ repo_url }}, no per-doc edit needed.
    _TEMPLATE_VARS_REPO_URL = "https://github.com/N8SDR1/Lyra-SDR"

    def _expand_template(self, md: str) -> str:
        """Substitute {{ var }} placeholders with current app metadata.

        Supported placeholders:
          {{ version }}       e.g. "0.0.3"
          {{ version_full }}  e.g. "0.0.3 — First Tester Build  (2026-04-25)"
          {{ repo_url }}      "https://github.com/N8SDR1/Lyra-SDR"

        Unknown placeholders are left untouched so a typo in a
        markdown file is visible at render time rather than silently
        becoming an empty string.
        """
        from lyra import __version__, version_string
        replacements = {
            "{{ version }}":      __version__,
            "{{ version_full }}": version_string(),
            "{{ repo_url }}":     self._TEMPLATE_VARS_REPO_URL,
        }
        for key, val in replacements.items():
            md = md.replace(key, val)
        return md

    def _render(self, path: Path):
        try:
            md = path.read_text(encoding="utf-8")
        except OSError as e:
            self.view.setMarkdown(
                f"# Could not read `{path.name}`\n\n"
                f"```\n{e}\n```")
            return
        # Live placeholder substitution so version / repo URL stay
        # in sync with the package metadata on every render.
        md = self._expand_template(md)
        # Set a search path so `![](../../assets/logo/xxx.png)` style
        # relative image links resolve cleanly. QTextBrowser resolves
        # image URLs against searchPaths() + the current markdown's
        # location.
        from PySide6.QtCore import QUrl
        project_root = self._root.parent.parent     # …/docs → project root
        self.view.setSearchPaths([str(self._root), str(project_root)])
        self.view.document().setBaseUrl(
            QUrl.fromLocalFile(str(self._root) + "/"))
        self.view.setMarkdown(md)
        self.view.verticalScrollBar().setValue(0)
        self._current_slug = path.stem
        self.topic_changed.emit(path.stem)
        # If a search is active, jump to the first hit in the new topic.
        if self._current_query:
            self._find_next(from_top=True)

    def _on_anchor(self, url):
        """Handle link clicks.

        Supported schemes:
        - `http://` / `https://` / `mailto:` — external browser / client
        - `panel:<name>` — flash the matching dock panel in the main
          window (e.g. `[see DSP panel](panel:dsp)` asks the host to
          highlight the DSP dock). No topic change.
        - `./foo.md` or `foo.md` — jump to the matching help topic.
        - plain `#anchor` or `foo.md#anchor` — future: jump to heading.
        """
        target = url.toString()
        if target.startswith(("http://", "https://", "mailto:")):
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(url)
            return
        if target.startswith("panel:"):
            panel = target[len("panel:"):].strip()
            if panel:
                self.panel_highlight_requested.emit(panel)
            return
        # Local .md link — jump to that topic. Tolerate "./foo.md",
        # "foo.md", and anchors after (we currently strip the anchor).
        stem_part = target.split("#", 1)[0]
        if stem_part.endswith(".md"):
            slug = Path(stem_part).stem
            self._select_slug(slug)

    def _select_slug(self, slug: str):
        """Select the list item whose stem == slug, if any. If the item
        isn't in the (possibly search-filtered) list, clear the search
        first so the user can still reach it."""
        for i in range(self.topic_list.count()):
            item = self.topic_list.item(i)
            path = Path(item.data(Qt.UserRole))
            if path.stem == slug:
                self.topic_list.setCurrentRow(i)
                return
        # Not visible under the current filter — clear search and retry.
        if self._current_query:
            self.search_edit.clear()
            # Clearing the QLineEdit triggers the debounced handler,
            # which rebuilds the list. Do a direct re-select here.
            self._current_query = ""
            self._populate_list(self._topic_paths, match_counts=None)
            for i in range(self.topic_list.count()):
                if Path(self.topic_list.item(i).data(Qt.UserRole)).stem == slug:
                    self.topic_list.setCurrentRow(i)
                    return
        # Still nothing — fall back to the first topic so the dialog
        # isn't blank.
        if self.topic_list.count():
            self.topic_list.setCurrentRow(0)

    # ── Search ──────────────────────────────────────────────────────
    def _apply_search(self):
        """Re-scan every topic file's contents for the current query.
        Filters the topic list to only matches (with hit counts) and,
        if a topic is open, jumps to the first match in the rendered
        view."""
        query = self.search_edit.text().strip()
        self._current_query = query
        remembered = self._current_slug
        self.topic_list.blockSignals(True)

        if not query:
            # Empty query = show full topic list.
            self._populate_list(self._topic_paths, match_counts=None)
            self.topic_list.blockSignals(False)
            self._select_slug(remembered or "index")
            # Clear any active text highlight in the rendered view.
            cursor = self.view.textCursor()
            cursor.clearSelection()
            self.view.setTextCursor(cursor)
            return

        # Count occurrences per file.
        q_lower = query.lower()
        hits: dict[Path, int] = {}
        for path in self._topic_paths:
            try:
                text = path.read_text(encoding="utf-8").lower()
            except OSError:
                continue
            c = text.count(q_lower)
            if c > 0:
                hits[path] = c

        self._populate_list(self._topic_paths, match_counts=hits)
        self.topic_list.blockSignals(False)

        if hits:
            # Prefer the previously-open topic if it matched, otherwise
            # pick the topic with the most hits.
            if remembered and any(p.stem == remembered for p in hits):
                self._select_slug(remembered)
            else:
                best = max(hits.items(), key=lambda kv: kv[1])[0]
                self._select_slug(best.stem)
            self._find_next(from_top=True)
        else:
            # No matches — show a friendly message on the right pane.
            self.view.setMarkdown(
                f"# No matches for \"{query}\"\n\n"
                "Try a shorter or different search term, or clear the"
                " search box to see every topic again.")

    def _find_next(self, from_top: bool = False):
        """Jump the rendered view to the next occurrence of the active
        search query. If `from_top` is True, search from the start of
        the document (used right after a topic change)."""
        if not self._current_query:
            return
        if from_top:
            cursor = self.view.textCursor()
            cursor.movePosition(QTextCursor.Start)
            self.view.setTextCursor(cursor)
        # QTextDocument.FindFlags() = 0 means case-insensitive + forward
        found = self.view.find(self._current_query)
        if not found:
            # Wrap around once.
            cursor = self.view.textCursor()
            cursor.movePosition(QTextCursor.Start)
            self.view.setTextCursor(cursor)
            self.view.find(self._current_query)

    # ── Print ───────────────────────────────────────────────────────
    def _on_print(self):
        """Open the system print dialog for the current topic's
        rendered content. Lazy-imported so the rest of Lyra doesn't
        pay the QtPrintSupport startup cost."""
        try:
            from PySide6.QtPrintSupport import QPrinter, QPrintDialog
        except ImportError:
            self.view.setMarkdown(
                "# Printing unavailable\n\n"
                "QtPrintSupport isn't installed. On Windows this is "
                "normally bundled with PySide6; reinstall PySide6 to "
                "recover this feature.")
            return
        printer = QPrinter(QPrinter.HighResolution)
        dlg = QPrintDialog(printer, self)
        dlg.setWindowTitle(f"Print — {self.windowTitle()}")
        if dlg.exec() == QPrintDialog.Accepted:
            # print_ works on both QTextDocument and QTextBrowser's doc.
            doc: QTextDocument = self.view.document()
            doc.print_(printer)

    # ── Public API ──────────────────────────────────────────────────
    def show_topic(self, slug: str):
        """Open the dialog (if not already visible) and jump to `slug`.
        Used by context-sensitive F1 / per-panel help buttons."""
        self._select_slug(slug)
        self.show()
        self.raise_()
        self.activateWindow()
