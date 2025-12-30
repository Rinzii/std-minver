"""Main window and application wiring."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

from PyQt6 import uic
from PyQt6.QtCore import Qt, QEvent, QThread, QTimer
from PyQt6.QtGui import QColor, QTextCursor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from cetest_ce import CeClient, CeLoadWorker, CeProbeWorker, CeAttempt, CeGroupSummary
from cetest_core import (
    CPP_SAMPLE,
    CODE_ABORTED,
    CODE_TIMEOUT,
    CODE_TRANSPORT_ERROR,
    LOG,
    Theme,
    ThemeManager,
    ansi_to_html_spans,
    choose_mono_font,
    default_rich_css,
    decode_text_file_for_editor,
    _escape_html,
    ensure_theme_json,
    setup_logging,
)
from cetest_flags import ExtraFlagsConfig
from cetest_models import CompilerInfo, family_sort_key, guess_family, parse_semver_key, platform_label, series_label
from cetest_prefs import (
    AppSettings,
    PreferencesState,
    _clamp_int,
    _apply_preferences_to_options_panel,
    _load_extra_flags_config,
    _load_library_rules,
    _load_preferences_state,
    _migrate_selected_group_keys,
    build_user_args_for_group,
    _save_extra_flags_by_group,
    _save_library_rules,
    _save_preferences_state,
)
from cetest_preprocess import _split_lines_paths, flatten_user_includes
from cetest_ui_widgets import CompilersPanel, EditorWidget, LibraryRuleDialog, LogPanel, OptionsPanel, ResultsPanel


def _is_dark_color(c: QColor) -> bool:

    return (c.red() * 0.2126 + c.green() * 0.7152 + c.blue() * 0.0722) < 128.0


def _qcolor_shade(c: QColor, factor: float) -> QColor:


    f = float(factor)
    r = max(0, min(255, int(c.red() * f)))
    g = max(0, min(255, int(c.green() * f)))
    b = max(0, min(255, int(c.blue() * f)))
    return QColor(r, g, b)


def build_app_stylesheet(theme: Theme, ui_font_pt: int) -> str:

    bg = theme.bg.name()
    fg = theme.fg.name()
    sel_bg = theme.selection_bg.name()
    sel_fg = theme.selection_fg.name()

    dark = _is_dark_color(theme.bg)
    border = _qcolor_shade(theme.bg, 1.45 if dark else 0.82).name()
    btn_bg = _qcolor_shade(theme.bg, 1.16 if dark else 0.98).name()
    btn_bg_hover = _qcolor_shade(theme.bg, 1.26 if dark else 0.95).name()

    return f"""
/* Global */
QWidget {{
  color: {fg};
  font-size: {int(ui_font_pt)}pt;
}}

/* Inputs / views */
QLineEdit, QComboBox, QListWidget, QTreeWidget, QTextEdit, QTextBrowser {{
  background: {bg};
  color: {fg};
  border: 1px solid {border};
  border-radius: 6px;
}}

QLineEdit, QComboBox {{
  padding: 4px 8px;
}}

QComboBox::drop-down {{
  border-left: 1px solid {border};
  width: 22px;
}}

QTreeWidget::item, QListWidget::item {{
  padding: 4px 6px;
}}

QTreeWidget::item:selected, QListWidget::item:selected, QTextEdit::selection, QTextBrowser::selection {{
  background: {sel_bg};
  color: {sel_fg};
}}

/* Buttons */
QPushButton {{
  background: {btn_bg};
  border: 1px solid {border};
  border-radius: 8px;
  padding: 6px 10px;
}}
QPushButton:hover {{
  background: {btn_bg_hover};
}}
QPushButton:disabled {{
  opacity: 0.55;
}}

/* Dock chrome */
QDockWidget::title {{
  padding: 6px 8px;
  background: {btn_bg};
  border-bottom: 1px solid {border};
}}
QToolBar {{
  spacing: 6px;
}}
QStatusBar {{
  border-top: 1px solid {border};
}}
"""


class PreferencesDialog(QDialog):

    def __init__(self, parent: QWidget | None = None):

        super().__init__(parent)
        uic.loadUi(str(Path(__file__).with_name("ui") / "preferences_dialog.ui"), self)
        self.buttonBox.accepted.connect(self.accept)
        self.buttonBox.rejected.connect(self.reject)
        self.btnBrowseTheme.clicked.connect(self._browse_theme)
        if hasattr(self, "btnBrowseCompileCommands"):
            self.btnBrowseCompileCommands.clicked.connect(self._browse_compile_commands)


        self._ce: CeClient | None = None
        self._lib_rules: list[LibraryRule] = []
        self._lib_families: list[str] = []
        self._lib_compiler_ids: list[str] = []
        self._lib_catalog: list[CeLibraryInfo] = []

        if hasattr(self, "tableLibraryRules"):
            self.tableLibraryRules.setColumnCount(4)
            self.tableLibraryRules.setHorizontalHeaderLabels(["Scope", "Target", "Library", "Version"])
            self.tableLibraryRules.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            self.tableLibraryRules.verticalHeader().setVisible(False)
            self.tableLibraryRules.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.tableLibraryRules.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

            self.tableLibraryRules.itemSelectionChanged.connect(self._update_lib_buttons)
            if hasattr(self, "btnLibAdd"):
                self.btnLibAdd.clicked.connect(self._on_lib_add)
            if hasattr(self, "btnLibEdit"):
                self.btnLibEdit.clicked.connect(self._on_lib_edit)
            if hasattr(self, "btnLibRemove"):
                self.btnLibRemove.clicked.connect(self._on_lib_remove)
            if hasattr(self, "btnLibRefresh"):
                self.btnLibRefresh.clicked.connect(self._on_lib_refresh)
            self._update_lib_buttons()

    def _browse_theme(self) -> None:

        path, _ = QFileDialog.getOpenFileName(self, "Select theme.json", "", "JSON Files (*.json);;All Files (*)")
        if path:
            self.editThemePath.setText(path)

    def _browse_compile_commands(self) -> None:

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select compile_commands.json",
            "",
            "JSON Files (*.json);;All Files (*)",
        )
        if path:
            self.editCompileCommandsPath.setText(path)

    @staticmethod
    def _mode_to_index(mode: str) -> int:

        m = (mode or "").strip().lower()
        if m == "dark":
            return 1
        if m == "light":
            return 2
        return 0

    @staticmethod
    def _index_to_mode(index: int) -> str:

        if index == 1:
            return "dark"
        if index == 2:
            return "light"
        return "auto"

    def set_preferences_state(self, p: PreferencesState) -> None:

        self.comboThemeMode.setCurrentIndex(self._mode_to_index(p.theme_mode))
        self.editThemePath.setText(p.theme_path or "")
        self.spinUIFontPt.setValue(int(p.ui_font_pt))
        self.spinEditorFontPt.setValue(int(p.editor_font_pt))
        if hasattr(self, "editCompileCommandsPath"):
            self.editCompileCommandsPath.setText(p.compile_commands_path or "")
        if hasattr(self, "editExtraIncludeDirs"):
            self.editExtraIncludeDirs.setPlainText(p.extra_include_dirs_text or "")
        if hasattr(self, "checkInlineOnce"):
            self.checkInlineOnce.setChecked(bool(p.inline_once))
        if hasattr(self, "checkAutoCopy"):
            self.checkAutoCopy.setChecked(bool(p.auto_copy))
        if hasattr(self, "checkStripPragmaOnce"):
            self.checkStripPragmaOnce.setChecked(bool(p.strip_pragma_once))
        if hasattr(self, "checkEmitLineDirectives"):
            self.checkEmitLineDirectives.setChecked(bool(p.emit_line_directives))
        if hasattr(self, "checkIncludeDebugComments"):
            self.checkIncludeDebugComments.setChecked(bool(p.include_debug_comments))
        if hasattr(self, "editExtraFlagsGnu"):
            self.editExtraFlagsGnu.setPlainText(str(p.extra_flags_gnu or ""))
        if hasattr(self, "editExtraFlagsMsvc"):
            self.editExtraFlagsMsvc.setPlainText(str(p.extra_flags_msvc or ""))
        self.set_library_rules(list(p.library_rules or []))

    def set_library_context(self, ce: CeClient | None, families: list[str], compiler_ids: list[str]) -> None:

        self._ce = ce
        self._lib_families = list(families or [])
        self._lib_compiler_ids = list(compiler_ids or [])
        self._update_lib_buttons()

    def set_library_rules(self, rules: list[LibraryRule]) -> None:

        self._lib_rules = list(rules or [])
        self._refresh_lib_table()

    def library_rules(self) -> list[LibraryRule]:

        return list(self._lib_rules)

    def _refresh_lib_table(self) -> None:

        if not hasattr(self, "tableLibraryRules"):
            return
        tbl: QTableWidget = self.tableLibraryRules
        tbl.setRowCount(len(self._lib_rules))
        for row, r in enumerate(self._lib_rules):
            scope_txt = {"all": "All", "family": "Family", "compiler": "Compiler"}.get(r.scope, r.scope)
            tgt_txt = r.target if r.scope != "all" else ""

            for col, txt in enumerate((scope_txt, tgt_txt, r.lib_id, r.version)):
                it = QTableWidgetItem(str(txt))
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                tbl.setItem(row, col, it)
        tbl.resizeRowsToContents()
        self._update_lib_buttons()

    def _selected_lib_row(self) -> int:

        if not hasattr(self, "tableLibraryRules"):
            return -1
        rows = self.tableLibraryRules.selectionModel().selectedRows()
        if not rows:
            return -1
        return int(rows[0].row())

    def _update_lib_buttons(self) -> None:

        has_tbl = hasattr(self, "tableLibraryRules")
        row = self._selected_lib_row() if has_tbl else -1
        if hasattr(self, "btnLibEdit"):
            self.btnLibEdit.setEnabled(row >= 0)
        if hasattr(self, "btnLibRemove"):
            self.btnLibRemove.setEnabled(row >= 0)
        if hasattr(self, "btnLibRefresh"):
            self.btnLibRefresh.setEnabled(self._ce is not None)

    def _on_lib_refresh(self) -> None:

        if self._ce is None:
            QMessageBox.information(self, "Libraries", "No Compiler Explorer client available.")
            return
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            self._lib_catalog = self._ce.list_libraries_cpp()
        except Exception as e:
            QMessageBox.warning(self, "Libraries", f"Failed to load libraries list from CE:\n\n{e}")
        finally:
            QApplication.restoreOverrideCursor()
        self._update_lib_buttons()

    def _on_lib_add(self) -> None:

        dlg = LibraryRuleDialog(
            title="Add library rule",
            initial=None,
            families=self._lib_families,
            compiler_ids=self._lib_compiler_ids,
            libraries=self._lib_catalog,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._lib_rules.append(dlg.rule())
        self._refresh_lib_table()

    def _on_lib_edit(self) -> None:

        row = self._selected_lib_row()
        if row < 0 or row >= len(self._lib_rules):
            return
        cur = self._lib_rules[row]
        dlg = LibraryRuleDialog(
            title="Edit library rule",
            initial=cur,
            families=self._lib_families,
            compiler_ids=self._lib_compiler_ids,
            libraries=self._lib_catalog,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._lib_rules[row] = dlg.rule()
        self._refresh_lib_table()

    def _on_lib_remove(self) -> None:

        row = self._selected_lib_row()
        if row < 0 or row >= len(self._lib_rules):
            return
        self._lib_rules.pop(row)
        self._refresh_lib_table()

    def preferences_state(self) -> PreferencesState:

        mode = self._index_to_mode(int(self.comboThemeMode.currentIndex()))
        path = (self.editThemePath.text() or "").strip()
        ui_pt = int(self.spinUIFontPt.value())
        ed_pt = int(self.spinEditorFontPt.value())
        cc = (self.editCompileCommandsPath.text() or "").strip() if hasattr(self, "editCompileCommandsPath") else ""
        extra = self.editExtraIncludeDirs.toPlainText() if hasattr(self, "editExtraIncludeDirs") else ""
        inline_once = bool(self.checkInlineOnce.isChecked()) if hasattr(self, "checkInlineOnce") else True
        auto_copy = bool(self.checkAutoCopy.isChecked()) if hasattr(self, "checkAutoCopy") else True
        strip_po = bool(self.checkStripPragmaOnce.isChecked()) if hasattr(self, "checkStripPragmaOnce") else True
        emit_line = bool(self.checkEmitLineDirectives.isChecked()) if hasattr(self, "checkEmitLineDirectives") else True
        dbg = bool(self.checkIncludeDebugComments.isChecked()) if hasattr(self, "checkIncludeDebugComments") else False
        extra_gnu = self.editExtraFlagsGnu.toPlainText() if hasattr(self, "editExtraFlagsGnu") else ""
        extra_msvc = self.editExtraFlagsMsvc.toPlainText() if hasattr(self, "editExtraFlagsMsvc") else ""
        return PreferencesState(
            theme_mode=mode,
            theme_path=path,
            ui_font_pt=ui_pt,
            editor_font_pt=ed_pt,
            compile_commands_path=cc,
            extra_include_dirs_text=extra,
            inline_once=inline_once,
            auto_copy=auto_copy,
            strip_pragma_once=strip_po,
            emit_line_directives=emit_line,
            include_debug_comments=dbg,
            extra_flags_gnu=extra_gnu,
            extra_flags_msvc=extra_msvc,
            library_rules=self.library_rules(),
        )


def render_report_html(standard: str, summaries: list[CeGroupSummary], lib_rules: list[LibraryRule] | None = None) -> str:

    by_fam: dict[str, list[CeGroupSummary]] = {}
    for s in summaries:
        by_fam.setdefault(s.compiler_type, []).append(s)

    total_groups = len(summaries)
    total_attempts = sum(len(s.attempts) for s in summaries)
    total_ok = sum(1 for s in summaries for a in s.attempts if a.ok())
    total_fail = total_attempts - total_ok
    total_inconclusive = sum(1 for s in summaries if s.inconclusive_reason is not None)

    all_lowest: list[CeAttempt] = [
        s.lowest_supported
        for s in summaries
        if s.lowest_supported is not None and s.lowest_supported.semver and s.inconclusive_reason is None
    ]
    overall_lowest = min(all_lowest, key=lambda a: parse_semver_key(a.semver)) if all_lowest else None

    overall_line = "None"
    if overall_lowest is not None:
        overall_line = (
            f"{overall_lowest.compiler_name} ({overall_lowest.semver})"
            f" [{overall_lowest.compiler_type} | {overall_lowest.platform} | {overall_lowest.series}]"
        )

    css = """
    <style>
    body { font-family: -apple-system, system-ui, Segoe UI, Roboto, Helvetica, Arial, sans-serif; }
    h2,h3 { margin: 12px 0 8px 0; }
    .meta { margin: 8px 0 14px 0; }
    table { border-collapse: collapse; width: 100%; }
    th, td { border: 1px solid rgba(127,127,127,0.55); padding: 6px 8px; vertical-align: top; }
    th { background: rgba(127,127,127,0.12); text-align: left; }
    .ok { font-weight: 600; }
    .fail { font-weight: 600; }
    .inc { font-weight: 600; }
    </style>
    """

    html: list[str] = [css]
    html.append("<h2>Compiler Explorer Report</h2>")
    html.append("<div class='meta'>")
    html.append(f"<div><b>C++ standard:</b> {_escape_html(standard)}</div>")
    rules = list(lib_rules or [])
    if rules:
        shown = rules[:8]
        def _fmt(r: LibraryRule) -> str:

            if r.scope == "all":
                return f"All: {r.lib_id}@{r.version}"
            if r.scope == "family":
                return f"Family({r.target}): {r.lib_id}@{r.version}"
            return f"Compiler({r.target}): {r.lib_id}@{r.version}"
        txt = "; ".join(_fmt(r) for r in shown)
        if len(rules) > len(shown):
            txt += f"; … (+{len(rules) - len(shown)} more)"
        html.append(f"<div><b>Library rules:</b> {_escape_html(txt)}</div>")
    else:
        html.append("<div><b>Library rules:</b> (none)</div>")
    html.append(f"<div><b>Groups tested:</b> {total_groups} (inconclusive: {total_inconclusive})</div>")
    html.append(f"<div><b>Total compilers tested:</b> {total_attempts} (OK: {total_ok}, FAIL: {total_fail})</div>")
    html.append(f"<div><b>Overall lowest passing (group-wise):</b> {_escape_html(overall_line)}</div>")
    html.append("</div>")

    html.append("<h3>Summary</h3>")
    html.append("<table>")
    html.append(
        "<tr><th>Family</th><th>Platform</th><th>Series</th><th>Highest passing</th><th>Lowest passing</th>"
        "<th>First failure</th><th>Tested</th><th>Status</th></tr>"
    )

    for fam in sorted(by_fam.keys(), key=family_sort_key):
        groups = sorted(by_fam[fam], key=lambda g: (g.platform.casefold(), g.series.casefold()))
        for g in groups:
            hs = "None"
            ls = "None"
            ff = "None"
            if g.highest_supported is not None:
                hs = f"{g.highest_supported.compiler_name} ({g.highest_supported.semver or 'unknown'})"
            if g.lowest_supported is not None:
                ls = f"{g.lowest_supported.compiler_name} ({g.lowest_supported.semver or 'unknown'})"
            if g.first_failure is not None:
                ff = f"{g.first_failure.compiler_name} ({g.first_failure.semver or 'unknown'})"

            status = "OK"
            cls = "ok"
            if g.highest_supported is None and g.inconclusive_reason is None:
                status = "NO SUCCESS"
                cls = "fail"
            if g.inconclusive_reason is not None:
                status = f"INCONCLUSIVE: {g.inconclusive_reason}"
                cls = "inc"

            html.append(
                "<tr>"
                f"<td>{_escape_html(fam)}</td>"
                f"<td>{_escape_html(g.platform)}</td>"
                f"<td>{_escape_html(g.series)}</td>"
                f"<td class='ok'>{_escape_html(hs)}</td>"
                f"<td class='ok'>{_escape_html(ls)}</td>"
                f"<td class='fail'>{_escape_html(ff)}</td>"
                f"<td>{len(g.attempts)}</td>"
                f"<td class='{cls}'>{_escape_html(status)}</td>"
                "</tr>"
            )

    html.append("</table>")
    return "\n".join(html)


def summaries_to_report_dict(standard: str, summaries: list[CeGroupSummary]) -> dict[str, Any]:

    def att_to_dict(a: CeAttempt) -> dict[str, Any]:

        return {
            "platform": a.platform,
            "series": a.series,

            "arch": a.platform,
            "family": a.compiler_type,
            "id": a.compiler_id,
            "name": a.compiler_name,
            "semver": a.semver,
            "code": a.code,
            "ok": a.ok(),
            "stderr": a.stderr_text,
        }

    def opt_att(a: CeAttempt | None) -> dict[str, Any] | None:

        return None if a is None else att_to_dict(a)

    return {
        "standard": standard,
        "groups": [
            {
                "family": s.compiler_type,
                "platform": s.platform,
                "series": s.series,

                "arch": s.platform,
                "highest_supported": opt_att(s.highest_supported),
                "lowest_supported": opt_att(s.lowest_supported),
                "first_failure": opt_att(s.first_failure),
                "attempts": [att_to_dict(a) for a in s.attempts],
                "inconclusive_reason": s.inconclusive_reason,
            }
            for s in summaries
        ],
    }


def report_dict_to_summaries(d: dict[str, Any]) -> tuple[str, list[CeGroupSummary]]:

    std = str(d.get("standard") or "c++17")
    groups = d.get("groups")
    if not isinstance(groups, list):
        return std, []

    def dict_to_att(x: dict[str, Any]) -> CeAttempt:

        return CeAttempt(
            platform=str(x.get("platform") or x.get("arch") or "unknown"),
            series=str(x.get("series") or "(unknown series)"),
            compiler_type=str(x.get("family") or "unknown"),
            compiler_id=str(x.get("id") or ""),
            compiler_name=str(x.get("name") or ""),
            semver=(str(x.get("semver")) if x.get("semver") is not None else None),
            code=int(x.get("code") if x.get("code") is not None else -1),
            stderr_text=str(x.get("stderr") or ""),
        )

    summaries: list[CeGroupSummary] = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        fam = str(g.get("family") or "unknown")
        platform = str(g.get("platform") or g.get("arch") or "unknown")
        series = str(g.get("series") or "(unknown series)")
        hs = g.get("highest_supported")
        ls = g.get("lowest_supported")
        ff = g.get("first_failure")
        atts = g.get("attempts")
        inc = g.get("inconclusive_reason")
        hs_a = dict_to_att(hs) if isinstance(hs, dict) else None
        ls_a = dict_to_att(ls) if isinstance(ls, dict) else None
        ff_a = dict_to_att(ff) if isinstance(ff, dict) else None
        attempts = [dict_to_att(a) for a in atts] if isinstance(atts, list) and all(isinstance(a, dict) for a in atts) else []
        summaries.append(
            CeGroupSummary(
                platform=platform,
                series=series,
                compiler_type=fam,
                highest_supported=hs_a,
                lowest_supported=ls_a,
                first_failure=ff_a,
                attempts=attempts,
                inconclusive_reason=(str(inc) if inc is not None else None),
            )
        )
    return std, summaries


class MainWindow(QMainWindow):



    def __init__(self, theme_mgr: ThemeManager, ce: CeClient, mono: QFont, settings: AppSettings):

        super().__init__()
        uic.loadUi(str(Path(__file__).with_name("ui") / "main_window.ui"), self)
        self._theme_mgr = theme_mgr
        self._ce = ce
        self._settings = settings
        self._current_source_path: Path | None = None

        self._by_family_platform_series: dict[str, dict[str, dict[str, list[CompilerInfo]]]] = {}
        self._summaries: list[CeGroupSummary] = []
        self._compilers_loaded = False
        self._current_std_for_report = "c++17"

        self._load_abort = threading.Event()
        self._probe_abort: threading.Event | None = None

        self._probe_thread: QThread | None = None
        self._probe_worker: CeProbeWorker | None = None
        self._load_thread: QThread | None = None
        self._load_worker: CeLoadWorker | None = None

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_session_values)

        self.setDockOptions(
            QMainWindow.DockOption.AllowNestedDocks
            | QMainWindow.DockOption.AllowTabbedDocks
            | QMainWindow.DockOption.AnimatedDocks
            | QMainWindow.DockOption.GroupedDragging
        )

        self._busy_label = QLabel("Probing...")
        self._busy_bar = QProgressBar()
        self._busy_bar.setRange(0, 0)
        self._busy_bar.setMaximumWidth(140)
        self._busy_label.hide()
        self._busy_bar.hide()
        self.statusbar.addPermanentWidget(self._busy_label)
        self.statusbar.addPermanentWidget(self._busy_bar)

        self.editor = EditorWidget(mono)
        self.editor.setText(CPP_SAMPLE)

        self.options_panel = OptionsPanel()
        self.compilers_panel = CompilersPanel(self._settings)
        self.results_panel = ResultsPanel(mono)
        self.log_panel = LogPanel(mono)


        self._ui_font_pt = 13
        self._editor_font_pt = 12


        self._apply_appearance_from_settings()


        self.dock_editor.setWidget(self.editor)
        self.dock_compilers.setWidget(self.compilers_panel)
        options_scroll = QScrollArea()
        options_scroll.setWidgetResizable(True)
        options_scroll.setWidget(self.options_panel)
        self.dock_options.setWidget(options_scroll)
        self.dock_results.setWidget(self.results_panel)
        self.dock_log.setWidget(self.log_panel)


        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_editor)

        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_compilers)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_options)
        self.splitDockWidget(self.dock_compilers, self.dock_options, Qt.Orientation.Vertical)

        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_report)
        self.splitDockWidget(self.dock_compilers, self.dock_report, Qt.Orientation.Horizontal)
        self.splitDockWidget(self.dock_options, self.dock_report, Qt.Orientation.Horizontal)

        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_log)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_results)
        self.splitDockWidget(self.dock_log, self.dock_results, Qt.Orientation.Horizontal)

        self.resizeDocks([self.dock_editor, self.dock_compilers, self.dock_report], [800, 520, 860], Qt.Orientation.Horizontal)
        self.resizeDocks([self.dock_compilers, self.dock_options], [740, 200], Qt.Orientation.Vertical)
        self.resizeDocks([self.dock_log, self.dock_results], [620, 900], Qt.Orientation.Horizontal)

        self._default_geometry = self.saveGeometry()
        self._default_state = self.saveState()

        self._wire_actions()

        self.options_panel.run_clicked.connect(self.probe_lowest_supported)
        self.options_panel.abort_clicked.connect(self.abort_probe)
        self.options_panel.sample_clicked.connect(self.insert_sample)
        if hasattr(self.options_panel, "preferences_clicked"):
            self.options_panel.preferences_clicked.connect(self.open_preferences)
        self.options_panel.session_changed.connect(self._schedule_save)
        if hasattr(self.options_panel, "compile_commands_changed"):
            self.options_panel.compile_commands_changed.connect(self._on_compile_commands_changed)
        if hasattr(self.options_panel, "pp_settings_changed"):
            self.options_panel.pp_settings_changed.connect(self._on_pp_settings_changed)

        self.compilers_panel.selection_changed.connect(self._schedule_save)
        self.compilers_panel.filter_changed.connect(lambda *_: self._schedule_save())
        if hasattr(self.compilers_panel, "extra_flags_changed"):
            self.compilers_panel.extra_flags_changed.connect(lambda: self.on_result_selected(self.results_panel.list.currentRow()))

        self.results_panel.result_row_changed.connect(self.on_result_selected)
        self.results_panel.split.splitterMoved.connect(lambda *_: self._schedule_save())

        self.editor.textChanged.connect(self._schedule_save)

        self._theme_mgr.theme_changed.connect(self._on_theme_changed)
        self._on_theme_changed(self._theme_mgr.current_theme())

        self._apply_saved_session_early()
        self._restore_layout(use_defaults=False)

        self._log("Loading compilers from Compiler Explorer...", clear=True)
        self._start_load_compilers()

    def _wire_actions(self) -> None:

        if hasattr(self, "actionOpenSource"):
            self.actionOpenSource.triggered.connect(self.open_source_file)
        if hasattr(self, "actionPreprocessorAddFile"):
            self.actionPreprocessorAddFile.triggered.connect(self.preprocessor_add_file)
        self.actionExportReportHTML.triggered.connect(self.export_report_html)
        self.actionExportReportJSON.triggered.connect(self.export_report_json)
        self.actionResetLayoutSession.triggered.connect(self.reset_layout_and_session)
        self.actionProbe.triggered.connect(self.probe_lowest_supported)
        self.actionAbort.triggered.connect(self.abort_probe)
        self.actionInsertEmptyMain.triggered.connect(self.insert_sample)
        if hasattr(self, "actionPreferences"):
            self.actionPreferences.triggered.connect(self.open_preferences)

    def _apply_stylesheet(self, theme: Theme) -> None:

        app = QApplication.instance()
        if app is None:
            return
        app.setStyleSheet(build_app_stylesheet(theme, self._ui_font_pt))

    def _apply_appearance_from_settings(self) -> None:


        p = _load_preferences_state(self._settings, default_ui_pt=self._ui_font_pt, default_editor_pt=self._editor_font_pt)


        self._theme_mgr.set_mode(str(p.theme_mode or "auto"))
        if isinstance(p.theme_path, str) and p.theme_path.strip():
            try:
                self._theme_mgr.set_theme_path(Path(p.theme_path).expanduser())
            except Exception as e:
                LOG.exception("Failed to load theme file: %s", p.theme_path)
                self.statusbar.showMessage(f"Theme load failed: {e}", 7000)


        self._ui_font_pt = int(p.ui_font_pt)
        self._editor_font_pt = int(p.editor_font_pt)

        app = QApplication.instance()
        if app is not None:
            f = app.font()
            f.setPointSize(self._ui_font_pt)
            app.setFont(f)

        mono = choose_mono_font(self._editor_font_pt)
        self.editor.set_mono_font(mono)
        self.log_panel.text.document().setDefaultStyleSheet(default_rich_css(mono))
        self.results_panel.details.document().setDefaultStyleSheet(default_rich_css(mono))


        theme = self._theme_mgr.current_theme()
        self.editor.apply_theme(theme)
        self._apply_stylesheet(theme)

    def open_preferences(self) -> None:

        p0 = _load_preferences_state(self._settings, default_ui_pt=self._ui_font_pt, default_editor_pt=self._editor_font_pt)

        families: list[str] = []
        compiler_ids: list[str] = []
        if self._compilers_loaded and self._by_family_platform_series:
            families = sorted(self._by_family_platform_series.keys(), key=family_sort_key)
            ids: set[str] = set()
            for by_platform in self._by_family_platform_series.values():
                for by_series in by_platform.values():
                    for lst in by_series.values():
                        for c in lst:
                            if c.id:
                                ids.add(c.id)
            compiler_ids = sorted(ids, key=lambda s: s.casefold())

        dlg = PreferencesDialog(self)
        if hasattr(dlg, "set_library_context"):
            dlg.set_library_context(self._ce, families, compiler_ids)
        dlg.set_preferences_state(p0)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        p1 = dlg.preferences_state()
        _save_preferences_state(self._settings, p1)
        self._apply_appearance_from_settings()
        _apply_preferences_to_options_panel(self.options_panel, p1)
        self._schedule_save()

    def _persist_preferences_from_options_panel(self, *, compile_commands_override: str | None = None) -> None:


        try:
            p = _load_preferences_state(self._settings, default_ui_pt=self._ui_font_pt, default_editor_pt=self._editor_font_pt)
            cc = str(compile_commands_override).strip() if compile_commands_override is not None else p.compile_commands_path
            p2 = PreferencesState(
                theme_mode=p.theme_mode,
                theme_path=p.theme_path,
                ui_font_pt=p.ui_font_pt,
                editor_font_pt=p.editor_font_pt,
                compile_commands_path=cc,
                extra_include_dirs_text=(self.options_panel.extra_include_dirs_text() if hasattr(self.options_panel, "extra_include_dirs_text") else p.extra_include_dirs_text),
                inline_once=(bool(self.options_panel.inline_once()) if hasattr(self.options_panel, "inline_once") else p.inline_once),
                auto_copy=(bool(self.options_panel.auto_copy()) if hasattr(self.options_panel, "auto_copy") else p.auto_copy),
                strip_pragma_once=(bool(self.options_panel.strip_pragma_once()) if hasattr(self.options_panel, "strip_pragma_once") else p.strip_pragma_once),
                emit_line_directives=(
                    bool(self.options_panel.emit_line_directives()) if hasattr(self.options_panel, "emit_line_directives") else p.emit_line_directives
                ),
                include_debug_comments=(
                    bool(self.options_panel.include_debug_comments()) if hasattr(self.options_panel, "include_debug_comments") else p.include_debug_comments
                ),
                extra_flags_gnu=p.extra_flags_gnu,
                extra_flags_msvc=p.extra_flags_msvc,
                library_rules=p.library_rules,
            )
            _save_preferences_state(self._settings, p2)
        except Exception:
            LOG.exception("Failed to persist preferences from Options panel")

    def _on_compile_commands_changed(self, s: str) -> None:

        self._persist_preferences_from_options_panel(compile_commands_override=str(s or "").strip())

    def _on_pp_settings_changed(self) -> None:

        self._persist_preferences_from_options_panel()

    def preprocessor_add_file(self) -> None:

        start = self._suggest_open_source_path()
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Select source file to flatten (inline user includes)",
            start,
            "C/C++ Files (*.c *.cc *.cpp *.cxx *.h *.hh *.hpp *.hxx *.ixx *.cppm);;Text Files (*.txt);;All Files (*)",
        )
        if not path_str:
            return

        root = Path(path_str).expanduser()
        self._settings.set_value(AppSettings.K_LAST_OPEN_PATH, str(root))

        prefs = _load_preferences_state(self._settings, default_ui_pt=self._ui_font_pt, default_editor_pt=self._editor_font_pt)
        extra_dirs = _split_lines_paths(str(prefs.extra_include_dirs_text or ""))
        cc_path = Path(str(prefs.compile_commands_path)).expanduser() if prefs.compile_commands_path.strip() else None
        inline_once = bool(prefs.inline_once)
        auto_copy = bool(prefs.auto_copy)
        strip_po = bool(prefs.strip_pragma_once)
        emit_line = bool(prefs.emit_line_directives)
        dbg = bool(prefs.include_debug_comments)

        self._log(f"Flattening (user includes only): {root}", clear=False)
        self.statusbar.showMessage("Flattening user includes...", 0)

        try:
            flattened, st = flatten_user_includes(
                root_file=root,
                extra_include_dirs=extra_dirs,
                compile_commands_path=cc_path,
                inline_once=inline_once,
                strip_pragma_once=strip_po,
                emit_line_directives=emit_line,
                include_debug_comments=dbg,
            )
        except Exception as e:
            LOG.exception("Preprocessor Add File failed for: %s", root)
            QMessageBox.critical(self, "Preprocessor Add File failed", f"Could not flatten:\n{root}\n\n{e}")
            self.statusbar.showMessage("Preprocessor Add File failed", 7000)
            return

        self._current_source_path = root
        self.editor.setText(flattened)
        self.setWindowTitle(f"CE Multi-Compiler Tester — {root.name} (flattened)")

        if auto_copy:
            try:
                cb = QApplication.clipboard()
                if cb is not None:
                    cb.setText(flattened)
            except Exception:
                LOG.exception("Failed to copy flattened output to clipboard")

        cc_match = st.get("compile_commands_match") if isinstance(st, dict) else None
        if isinstance(cc_match, dict) and st.get("compile_commands_used"):
            if not cc_match.get("matched"):
                self._log("compile_commands.json: no match found for selected file (common for headers) — using only extra dirs + relative includes.", clear=False)
            else:
                exact = "exact" if cc_match.get("exact") else "fallback"
                self._log(
                    f"compile_commands.json match ({exact}): {cc_match.get('matched_entry_file')} (score={cc_match.get('score')})",
                    clear=False,
                )

        msg = (
            f"Flattened {root.name}: inlined_files={st.get('files_inlined')} "
            f"inlined_includes={st.get('include_lines_inlined')} "
            f"unresolved_includes={st.get('include_lines_unresolved')}"
        )
        self._log(msg, clear=False)
        self.statusbar.showMessage(msg, 8000)

    def _mk_dock(self, title: str, widget: QWidget) -> QDockWidget:

        d = QDockWidget(title, self)
        d.setObjectName(f"dock_{title.lower()}")
        d.setWidget(widget)
        d.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        d.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        return d

    def _log(self, msg: str, clear: bool = False) -> None:

        if clear:
            self.log_panel.set_ansi_text(msg)
        else:
            self.log_panel.append_ansi(msg)
        LOG.debug("UI: %s", msg)

    def changeEvent(self, event):

        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self._theme_mgr.refresh()
        super().changeEvent(event)

    @staticmethod
    def _thread_is_running(th: QThread | None) -> bool:

        if th is None:
            return False
        try:
            return th.isRunning()
        except RuntimeError:
            return False

    def closeEvent(self, event):

        self.abort_probe()
        self._load_abort.set()

        if self._thread_is_running(self._probe_thread):
            try:
                self._probe_thread.requestInterruption()
                self._probe_thread.quit()
                self._probe_thread.wait(2000)
            except RuntimeError:
                pass

        if self._thread_is_running(self._load_thread):
            try:
                self._load_thread.requestInterruption()
                self._load_thread.quit()
                self._load_thread.wait(2000)
            except RuntimeError:
                pass

        self._save_layout_now()
        self._save_session_values()
        super().closeEvent(event)

    def _save_layout_now(self) -> None:

        self._settings.set_value(AppSettings.K_GEOMETRY, self.saveGeometry())
        self._settings.set_value(AppSettings.K_STATE, self.saveState())
        LOG.debug("Saved layout state")

    def _restore_layout(self, use_defaults: bool) -> None:

        if use_defaults:
            self.restoreGeometry(self._default_geometry)
            self.restoreState(self._default_state)
            return

        geom = self._settings.get_value(AppSettings.K_GEOMETRY)
        st = self._settings.get_value(AppSettings.K_STATE)

        if geom is not None:
            self.restoreGeometry(geom)
        if st is not None:
            self.restoreState(st)

    def _schedule_save(self) -> None:

        self._save_timer.start(250)

    def _apply_saved_session_early(self) -> None:

        txt = self._settings.get_value(AppSettings.K_EDITOR)
        if isinstance(txt, str) and txt:
            self.editor.setText(txt)

        std = self._settings.get_value(AppSettings.K_CPPSTD)
        if isinstance(std, str) and std:
            self.options_panel.cpp_std.setCurrentText(std)

        prefs = _load_preferences_state(self._settings, default_ui_pt=self._ui_font_pt, default_editor_pt=self._editor_font_pt)
        _apply_preferences_to_options_panel(self.options_panel, prefs)

        flt = self._settings.get_value(AppSettings.K_COMPILERS_FILTER)
        if isinstance(flt, str) and flt:
            self.compilers_panel.filter_edit.setText(flt)

        rs = self._settings.get_value(AppSettings.K_RESULTS_SPLIT)
        if isinstance(rs, list) and all(isinstance(x, int) for x in rs):
            self.results_panel.split.setSizes(rs)

        saved_report_json = self._settings.get_value(AppSettings.K_LAST_REPORT_JSON)
        saved_report_html = self._settings.get_value(AppSettings.K_LAST_REPORT_HTML)
        if isinstance(saved_report_json, str) and saved_report_json.strip():
            try:
                d = json.loads(saved_report_json)
                std2, sums = report_dict_to_summaries(d if isinstance(d, dict) else {})
                self._current_std_for_report = std2
                self._summaries = sums
                self._rebuild_results_list()
            except Exception:
                LOG.exception("Failed to restore last report JSON")

        if isinstance(saved_report_html, str) and saved_report_html.strip():
            self.report_browser.setHtml(saved_report_html)

        row = self._settings.get_value(AppSettings.K_LAST_RESULT_ROW)
        if isinstance(row, int) and row >= 0:
            self.results_panel.list.setCurrentRow(row)

    def _save_session_values(self) -> None:

        self._settings.set_value(AppSettings.K_EDITOR, self.editor.text())
        self._settings.set_value(AppSettings.K_CPPSTD, self.options_panel.selected_cpp_std())

        if hasattr(self.options_panel, "compile_commands_path"):
            self._persist_preferences_from_options_panel(compile_commands_override=self.options_panel.compile_commands_path())
        else:
            self._persist_preferences_from_options_panel()
        self._settings.set_value(AppSettings.K_COMPILERS_FILTER, self.compilers_panel.filter_edit.text())
        self._settings.set_value(AppSettings.K_RESULTS_SPLIT, self.results_panel.split.sizes())

        selected = [f"{fam}|{platform}|{series}" for fam, platform, series in self.compilers_panel.selected_groups()]
        self._settings.set_value(AppSettings.K_SELECTED, selected)
        self._settings.set_value(AppSettings.K_LAST_RESULT_ROW, self.results_panel.list.currentRow())

        if self._summaries:
            report_d = summaries_to_report_dict(self._current_std_for_report, self._summaries)
            report_json = json.dumps(report_d, ensure_ascii=False)
            report_html = self.report_browser.toHtml()
            self._settings.set_value(AppSettings.K_LAST_REPORT_JSON, report_json)
            self._settings.set_value(AppSettings.K_LAST_REPORT_HTML, report_html)

        LOG.debug(
            "Saved session editor_len=%d std=%s selected=%d report_groups=%d",
            len(self.editor.text()),
            self.options_panel.selected_cpp_std(),
            len(selected),
            len(self._summaries),
        )

    def _on_theme_changed(self, theme: Theme) -> None:

        self.editor.apply_theme(theme)
        self._apply_stylesheet(theme)

    def _start_load_compilers(self) -> None:

        self._load_thread = QThread(self)
        self._load_worker = CeLoadWorker(self._ce, self._load_abort)
        self._load_worker.moveToThread(self._load_thread)

        self._load_thread.started.connect(self._load_worker.run)
        self._load_worker.loaded.connect(self._on_compilers_loaded)
        self._load_worker.failed.connect(self._on_compilers_failed)
        self._load_worker.aborted.connect(self._on_compilers_aborted)

        self._load_worker.loaded.connect(self._load_thread.quit)
        self._load_worker.failed.connect(self._load_thread.quit)
        self._load_worker.aborted.connect(self._load_thread.quit)
        self._load_thread.finished.connect(self._load_worker.deleteLater)

        self._load_thread.start()

    def _on_compilers_aborted(self) -> None:

        self._log("Compiler loading aborted.", clear=False)
        self.statusbar.showMessage("Load aborted", 5000)

    def _on_compilers_loaded(self, compilers: list[CompilerInfo]) -> None:

        self._compilers_loaded = True
        by_family_platform_series: dict[str, dict[str, dict[str, list[CompilerInfo]]]] = {}
        fam_counts: dict[str, int] = {}

        for c in compilers:
            fam = guess_family(c)
            platform = platform_label(c)
            series = series_label(c)
            by_family_platform_series.setdefault(fam, {}).setdefault(platform, {}).setdefault(series, []).append(c)
            fam_counts[fam] = fam_counts.get(fam, 0) + 1

        for by_platform in by_family_platform_series.values():
            for by_series in by_platform.values():
                for lst in by_series.values():
                    lst.sort(key=lambda x: parse_semver_key(x.semver), reverse=True)

        self._by_family_platform_series = by_family_platform_series

        families_to_platform_to_series_counts: dict[str, dict[str, dict[str, int]]] = {}
        for fam, by_platform in by_family_platform_series.items():
            families_to_platform_to_series_counts[fam] = {
                platform: {series: len(lst) for series, lst in by_series.items()} for platform, by_series in by_platform.items()
            }

        self.compilers_panel.set_data(families_to_platform_to_series_counts)

        saved_sel = self._settings.get_value(AppSettings.K_SELECTED, [])
        if isinstance(saved_sel, list):
            sel_set = _migrate_selected_group_keys({str(x) for x in saved_sel}, families_to_platform_to_series_counts)
            self.compilers_panel.set_selected_groups(sel_set)

        fam_count = len(by_family_platform_series)
        platform_count = len({p for by_platform in by_family_platform_series.values() for p in by_platform.keys()})
        top = sorted(fam_counts.items(), key=lambda kv: (family_sort_key(kv[0]), -kv[1]))
        top_txt = ", ".join(f"{k}={v}" for k, v in top[:16])

        self._log(
            f"Loaded {len(compilers)} C++ compilers across {fam_count} families and {platform_count} platforms. {top_txt}",
            clear=True,
        )
        self.statusbar.showMessage("Compilers loaded", 5000)

    def _on_compilers_failed(self, msg: str) -> None:

        self._log(f"Failed to load compilers from Compiler Explorer: {msg}", clear=True)
        self.options_panel.btn_probe.setEnabled(False)
        self.statusbar.showMessage("Failed to load compilers", 5000)

    def insert_sample(self) -> None:

        self.editor.setText(CPP_SAMPLE)

    def _suggest_open_source_path(self) -> str:

        v = self._settings.get_value(AppSettings.K_LAST_OPEN_PATH, "")
        if not isinstance(v, str) or not v.strip():
            return ""
        try:
            p = Path(v).expanduser()
            if p.exists() and p.is_file():
                return str(p)
            if p.exists() and p.is_dir():
                return str(p)
            if p.parent.exists() and p.parent.is_dir():
                return str(p.parent)
        except Exception:
            return ""
        return ""

    @staticmethod
    def _decode_text_file_for_editor(data: bytes) -> tuple[str, str]:


        if b"\x00" in data:
            raise ValueError("This looks like a binary file (contains NUL bytes).")


        for enc in ("utf-8-sig", "utf-8"):
            try:
                return data.decode(enc), enc
            except UnicodeDecodeError:
                pass


        return data.decode("utf-8", errors="replace"), "utf-8 (replacement)"

    def open_source_file(self) -> None:

        start = self._suggest_open_source_path()
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Open source file",
            start,
            "C/C++ Files (*.c *.cc *.cpp *.cxx *.h *.hh *.hpp *.hxx *.ixx *.cppm);;Text Files (*.txt);;All Files (*)",
        )
        if not path_str:
            return

        path = Path(path_str).expanduser()
        try:
            data = path.read_bytes()
            if len(data) > 5_000_000:
                raise ValueError("File is too large to load into the editor (> 5 MB).")
            text, enc = self._decode_text_file_for_editor(data)
        except Exception as e:
            LOG.exception("Failed to open source file: %s", path)
            QMessageBox.critical(self, "Open failed", f"Could not open:\n{path}\n\n{e}")
            self.statusbar.showMessage("Open failed", 7000)
            return

        self._current_source_path = path
        self._settings.set_value(AppSettings.K_LAST_OPEN_PATH, str(path))
        self.editor.setText(text)
        self.setWindowTitle(f"CE Multi-Compiler Tester — {path.name}")
        self.statusbar.showMessage(f"Loaded {path.name} ({enc}, {len(text)} chars)", 6000)

    def _build_jobs(self) -> list[tuple[str, str, str, list[CompilerInfo]]]:

        jobs: list[tuple[str, str, str, list[CompilerInfo]]] = []
        groups = self.compilers_panel.selected_groups()
        for fam, platform, series in groups:
            lst = self._by_family_platform_series.get(fam, {}).get(platform, {}).get(series, [])
            if lst:
                jobs.append((platform, series, fam, lst))
        jobs.sort(key=lambda t: (family_sort_key(t[2]), t[0].casefold(), t[1].casefold()))
        return jobs

    def _set_busy(self, on: bool) -> None:

        if on:
            self._busy_label.show()
            self._busy_bar.show()
        else:
            self._busy_label.hide()
            self._busy_bar.hide()

    def probe_lowest_supported(self) -> None:

        if not self._compilers_loaded:
            self._log("Compilers are still loading...", clear=False)
            return

        if self._thread_is_running(self._probe_thread):
            self._log("Probe already running.", clear=False)
            return

        jobs = self._build_jobs()
        if not jobs:
            self._log("Select at least one family/platform/series in the Compilers pane.", clear=False)
            self.statusbar.showMessage("Nothing selected", 4000)
            return

        self._probe_abort = threading.Event()

        source = self.editor.text()
        std = self.options_panel.selected_cpp_std()
        extra_flags = _load_extra_flags_config(self._settings)
        lib_rules = _load_library_rules(self._settings)

        self._current_std_for_report = std
        self._summaries.clear()
        self.results_panel.list.clear()
        self.results_panel.details.clear()
        self.report_browser.clear()

        self.options_panel.btn_probe.setEnabled(False)
        self.options_panel.btn_abort.setEnabled(True)
        self._set_busy(True)
        self.statusbar.showMessage("Probing...", 0)
        self._log(
            f"Probing {len(jobs)} group(s) using binary search (std={std}, library_rules={len(lib_rules)})...",
            clear=False,
        )

        self._probe_thread = QThread(self)
        self._probe_worker = CeProbeWorker(self._ce, jobs, source, std, extra_flags, lib_rules, self._probe_abort)
        self._probe_worker.moveToThread(self._probe_thread)

        self._probe_thread.started.connect(self._probe_worker.run)
        self._probe_worker.group_done.connect(self._on_group_done)
        self._probe_worker.finished.connect(self._on_probe_finished)
        self._probe_worker.failed.connect(self._on_probe_failed)
        self._probe_worker.aborted.connect(self._on_probe_aborted)

        self._probe_worker.finished.connect(self._probe_thread.quit)
        self._probe_worker.failed.connect(self._probe_thread.quit)
        self._probe_worker.aborted.connect(self._probe_thread.quit)
        self._probe_thread.finished.connect(self._probe_worker.deleteLater)

        self._probe_thread.start()

    def abort_probe(self) -> None:

        if self._probe_abort is not None and not self._probe_abort.is_set():
            self._probe_abort.set()
            self._log("Abort requested.", clear=False)
            self.statusbar.showMessage("Aborting...", 0)

        if self._thread_is_running(self._probe_thread):
            try:
                self._probe_thread.requestInterruption()
            except RuntimeError:
                pass

    def _rebuild_results_list(self) -> None:

        self.results_panel.list.clear()
        for s in sorted(self._summaries, key=lambda x: (family_sort_key(x.compiler_type), x.platform.casefold(), x.series.casefold())):
            label = f"{s.compiler_type} | {s.platform} | {s.series}"
            if s.inconclusive_reason is not None:
                self.results_panel.list.addItem(QListWidgetItem(f"INCONCLUSIVE  {label}  {s.inconclusive_reason}"))
                continue
            hs = s.highest_supported
            ls = s.lowest_supported
            if hs is None:
                self.results_panel.list.addItem(QListWidgetItem(f"NO SUCCESS  {label}"))
                continue
            hs_txt = f"{hs.semver or 'unknown'}"
            ls_txt = f"{(ls.semver if ls is not None else None) or 'unknown'}"
            self.results_panel.list.addItem(QListWidgetItem(f"OK  {label}  lowest={ls_txt}  highest={hs_txt}  tested={len(s.attempts)}"))

        if self.results_panel.list.count() > 0 and self.results_panel.list.currentRow() == -1:
            self.results_panel.list.setCurrentRow(0)

    def _refresh_report_view(self) -> None:

        if not self._summaries:
            self.report_browser.clear()
            return
        lib_rules = _load_library_rules(self._settings)
        html = render_report_html(self._current_std_for_report, self._summaries, lib_rules)
        self.report_browser.setHtml(html)

    def _on_group_done(self, summary: CeGroupSummary) -> None:

        self._summaries.append(summary)
        self._summaries.sort(key=lambda x: (family_sort_key(x.compiler_type), x.platform.casefold(), x.series.casefold()))
        self._rebuild_results_list()
        self._refresh_report_view()

        label = f"{summary.compiler_type} | {summary.platform} | {summary.series}"
        if summary.inconclusive_reason is not None:
            self._log(f"Done: {label} -> INCONCLUSIVE ({summary.inconclusive_reason})", clear=False)
        else:
            hs = summary.highest_supported
            ls = summary.lowest_supported
            if hs is None:
                self._log(f"Done: {label} -> no successful compiler. tested={len(summary.attempts)}", clear=False)
            else:
                hs_v = hs.semver or "unknown"
                ls_v = (ls.semver if ls is not None else None) or "unknown"
                self._log(f"Done: {label} -> lowest={ls_v} highest={hs_v} tested={len(summary.attempts)}", clear=False)

        self._schedule_save()

    def _finish_probe_ui(self) -> None:

        self.options_panel.btn_probe.setEnabled(True)
        self.options_panel.btn_abort.setEnabled(False)
        self._set_busy(False)
        self._refresh_report_view()
        self._schedule_save()

    def _on_probe_finished(self) -> None:

        self._finish_probe_ui()
        self._log(f"Done. Probed {len(self._summaries)} group(s).", clear=False)
        self.statusbar.showMessage("Done", 5000)

    def _on_probe_aborted(self) -> None:

        self._finish_probe_ui()
        self._log("Aborted.", clear=False)
        self.statusbar.showMessage("Aborted", 5000)

    def _on_probe_failed(self, msg: str) -> None:

        self._finish_probe_ui()
        self._log(f"Probe failed: {msg}", clear=False)
        self.statusbar.showMessage("Probe failed", 5000)

    def on_result_selected(self, row: int) -> None:

        if row < 0 or row >= len(self._summaries):
            self.results_panel.details.clear()
            return

        s = self._summaries[row]
        extra_flags = _load_extra_flags_config(self._settings)
        parts: list[str] = []
        parts.append(f"Family: {s.compiler_type}")
        parts.append(f"Platform: {s.platform}")
        parts.append(f"Series: {s.series}")
        parts.append(f"Standard: {self._current_std_for_report}")
        parts.append(f"Args used: {build_user_args_for_group(s.compiler_type, s.platform, s.series, self._current_std_for_report, extra_flags)}")
        try:
            rules = _load_library_rules(self._settings)
            if s.highest_supported is not None:
                libs_h = _effective_libraries_for_compiler(rules, s.compiler_type, s.highest_supported.compiler_id)
                libs_txt = ", ".join(f"{d['id']}@{d['version']}" for d in libs_h) if libs_h else "(none)"
                parts.append(f"Libraries (highest): {libs_txt}")
            if s.lowest_supported is not None:
                libs_l = _effective_libraries_for_compiler(rules, s.compiler_type, s.lowest_supported.compiler_id)
                libs_txt = ", ".join(f"{d['id']}@{d['version']}" for d in libs_l) if libs_l else "(none)"
                parts.append(f"Libraries (lowest): {libs_txt}")
        except Exception:

            pass

        if s.highest_supported is None:
            parts.append("Highest passing: None")
            parts.append("Lowest passing: None")
        else:
            hs = s.highest_supported
            ls = s.lowest_supported
            parts.append(f"Highest passing: {hs.compiler_name} (semver={hs.semver}) id={hs.compiler_id}")
            if ls is not None:
                parts.append(f"Lowest passing: {ls.compiler_name} (semver={ls.semver}) id={ls.compiler_id}")
            else:
                parts.append("Lowest passing: None")

        if s.inconclusive_reason is not None:
            parts.append("")
            parts.append(f"INCONCLUSIVE: {s.inconclusive_reason}")

        if s.first_failure is not None:
            ff = s.first_failure
            parts.append("")
            parts.append(f"First failure: {ff.compiler_name} (semver={ff.semver}) id={ff.compiler_id} code={ff.code}")
            if ff.stderr_text:
                parts.append("")
                parts.append("stderr/err:")
                parts.append(ff.stderr_text)

        parts.append("")
        parts.append("Compilers tested:")
        for a in s.attempts:
            if a.code == CODE_TIMEOUT:
                tag = "TIMEOUT"
            elif a.code == CODE_TRANSPORT_ERROR:
                tag = "TRANSPORT_ERROR"
            elif a.code == CODE_ABORTED:
                tag = "ABORTED"
            else:
                tag = "OK" if a.ok() else f"FAIL({a.code})"
            parts.append(f"{tag}  semver={a.semver}  {a.compiler_name}  id={a.compiler_id}")

        self.results_panel.set_details_ansi("\n".join(parts))
        self._schedule_save()

    def export_report_html(self) -> None:

        if not self._summaries:
            QMessageBox.information(self, "Export", "No report to export yet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Report (HTML)", "ce_report.html", "HTML Files (*.html)")
        if not path:
            return
        lib_rules = _load_library_rules(self._settings)
        html = render_report_html(self._current_std_for_report, self._summaries, lib_rules)
        try:
            Path(path).write_text(html, encoding="utf-8")
            self._log(f"Exported HTML report to {path}", clear=False)
        except Exception as e:
            LOG.exception("Export HTML failed")
            QMessageBox.critical(self, "Export failed", str(e))

    def export_report_json(self) -> None:

        if not self._summaries:
            QMessageBox.information(self, "Export", "No report to export yet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Report (JSON)", "ce_report.json", "JSON Files (*.json)")
        if not path:
            return
        report_d = summaries_to_report_dict(self._current_std_for_report, self._summaries)
        try:
            Path(path).write_text(json.dumps(report_d, indent=2, ensure_ascii=False), encoding="utf-8")
            self._log(f"Exported JSON report to {path}", clear=False)
        except Exception as e:
            LOG.exception("Export JSON failed")
            QMessageBox.critical(self, "Export failed", str(e))

    def reset_layout_and_session(self) -> None:

        self.abort_probe()
        self._settings.reset_all()

        self._restore_layout(use_defaults=True)

        self.editor.setText(CPP_SAMPLE)
        self.options_panel.cpp_std.setCurrentText("c++17")
        self.compilers_panel.filter_edit.clear()
        self.results_panel.list.clear()
        self.results_panel.details.clear()
        self.report_browser.clear()
        self._summaries.clear()
        self._current_std_for_report = "c++17"

        if self._compilers_loaded:
            self.compilers_panel.set_selected_groups(set())

        self._log("Reset.", clear=True)
        self.statusbar.showMessage("Reset", 5000)


def main():
    """Run the Qt application."""
    setup_logging()
    app = QApplication(sys.argv)

    settings = AppSettings()
    prefs = _load_preferences_state(settings, default_ui_pt=13, default_editor_pt=12)
    ui_pt_i = _clamp_int(prefs.ui_font_pt, 13, 8, 28)
    f = app.font()
    f.setPointSize(ui_pt_i)
    app.setFont(f)

    mono_pt_i = _clamp_int(prefs.editor_font_pt, 12, 8, 28)
    mono = choose_mono_font(mono_pt_i)

    theme_mgr = ThemeManager(Path(__file__).with_name("theme.json"))
    theme_mgr.set_mode(str(prefs.theme_mode or "auto"))
    theme_path = str(prefs.theme_path or "")
    if theme_path.strip():
        try:
            theme_mgr.set_theme_path(Path(theme_path).expanduser())
        except Exception:
            LOG.exception("Failed to load theme file at startup: %s", theme_path)

    ce = CeClient(base_url="https://godbolt.org", min_request_interval_s=0.12)

    w = MainWindow(theme_mgr, ce, mono, settings)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
