"""Qt widgets and dialogs."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from PyQt6 import uic
from PyQt6.QtCore import Qt, QEvent, QObject, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QTextCursor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QSizePolicy,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PyQt6.Qsci import QsciLexerCPP, QsciScintilla

from cetest_core import Theme, default_rich_css, wrap_pre_html
from cetest_flags import ExtraFlagsConfig, _normalize_user_flags_text
from cetest_models import CompilerInfo, family_sort_key
from cetest_prefs import (
    LibraryRule,
    _load_extra_flags_config,
    _normalize_ce_library_id,
    _normalize_ce_library_version,
    _save_extra_flags_by_group,
    build_user_args_for_group,
)


class EditorWidget(QsciScintilla):

    def __init__(self, mono: QFont, parent: QWidget | None = None):

        super().__init__(parent)
        self._mono = mono
        self._lexer = QsciLexerCPP(self)
        self._setup_base()
        self.setLexer(self._lexer)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def _setup_base(self) -> None:

        self.setUtf8(True)
        self.setFont(self._mono)
        self.setMarginsFont(self._mono)

        self.setMarginType(0, QsciScintilla.MarginType.NumberMargin)
        self.setMarginLineNumbers(0, True)
        self.setMarginWidth(0, "00000")

        self.setIndentationsUseTabs(False)
        self.setTabWidth(4)
        self.setAutoIndent(True)
        self.setBackspaceUnindents(True)

        self.setBraceMatching(QsciScintilla.BraceMatch.SloppyBraceMatch)
        self.setFolding(QsciScintilla.FoldStyle.BoxedTreeFoldStyle)

        self._lexer.setDefaultFont(self._mono)
        for style in range(128):
            self._lexer.setFont(self._mono, style)

    def set_mono_font(self, mono: QFont) -> None:

        self._mono = mono
        self._setup_base()

    def apply_theme(self, theme: Theme) -> None:

        self.setPaper(theme.bg)
        self.setColor(theme.fg)

        self.setMarginsBackgroundColor(theme.margin_bg)
        self.setMarginsForegroundColor(theme.margin_fg)

        self.setCaretForegroundColor(theme.caret_fg)
        self.setCaretLineVisible(True)
        self.setCaretLineBackgroundColor(theme.caret_line)

        self.setSelectionBackgroundColor(theme.selection_bg)
        self.setSelectionForegroundColor(theme.selection_fg)

        self._lexer.setDefaultPaper(theme.bg)
        self._lexer.setPaper(theme.bg)
        self._lexer.setDefaultColor(theme.fg)
        self._lexer.setColor(theme.fg)

        def set_style(style_name: str, fg: QColor, bg: QColor) -> None:

            style = getattr(QsciLexerCPP, style_name, None)
            if style is not None:
                self._lexer.setColor(fg, style)
                self._lexer.setPaper(bg, style)

        for name in ("Default", "Identifier", "InactiveDefault", "InactiveIdentifier"):
            set_style(name, theme.fg, theme.bg)
        for name in ("Operator", "InactiveOperator"):
            set_style(name, theme.op, theme.bg)
        for name in ("Keyword", "KeywordSet2", "InactiveKeyword", "InactiveKeywordSet2"):
            set_style(name, theme.kw, theme.bg)
        for name in ("GlobalClass", "InactiveGlobalClass"):
            set_style(name, theme.ty, theme.bg)
        for name in ("Number", "InactiveNumber"):
            set_style(name, theme.num, theme.bg)
        for name in (
            "DoubleQuotedString",
            "SingleQuotedString",
            "RawString",
            "VerbatimString",
            "Regex",
            "InactiveDoubleQuotedString",
            "InactiveSingleQuotedString",
            "InactiveRawString",
            "InactiveVerbatimString",
            "InactiveRegex",
        ):
            set_style(name, theme.s, theme.bg)
        for name in (
            "Comment",
            "CommentLine",
            "CommentDoc",
            "CommentLineDoc",
            "CommentDocKeyword",
            "CommentDocKeywordError",
            "InactiveComment",
            "InactiveCommentLine",
            "InactiveCommentDoc",
            "InactiveCommentLineDoc",
            "InactiveCommentDocKeyword",
            "InactiveCommentDocKeywordError",
        ):
            set_style(name, theme.com, theme.bg)
        for name in ("PreProcessor", "PreProcessorComment", "InactivePreProcessor", "InactivePreProcessorComment"):
            set_style(name, theme.pp, theme.bg)


class CompilerArchTree(QTreeWidget):

    selection_changed = pyqtSignal()

    def __init__(self):

        super().__init__()
        self.setColumnCount(2)
        self.setHeaderLabels(["Family / Platform / Series", "Count"])
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        self.setIndentation(18)
        self.setExpandsOnDoubleClick(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.itemChanged.connect(lambda *_: self.selection_changed.emit())

    def set_data(self, families_to_platform_to_series_counts: dict[str, dict[str, dict[str, int]]]) -> None:

        self.blockSignals(True)
        self.clear()

        for fam in sorted(families_to_platform_to_series_counts.keys(), key=family_sort_key):
            by_platform = families_to_platform_to_series_counts[fam]
            total = sum(int(v) for by_series in by_platform.values() for v in by_series.values())

            parent = QTreeWidgetItem([fam, str(total)])
            parent.setFlags(parent.flags() | Qt.ItemFlag.ItemIsAutoTristate | Qt.ItemFlag.ItemIsUserCheckable)
            parent.setCheckState(0, Qt.CheckState.Unchecked)
            self.addTopLevelItem(parent)

            platforms = sorted(by_platform.items(), key=lambda kv: kv[0].casefold())
            for platform, by_series in platforms:
                plat_total = sum(int(v) for v in by_series.values())
                plat_it = QTreeWidgetItem([platform, str(int(plat_total))])
                plat_it.setFlags(plat_it.flags() | Qt.ItemFlag.ItemIsAutoTristate | Qt.ItemFlag.ItemIsUserCheckable)
                plat_it.setCheckState(0, Qt.CheckState.Unchecked)
                parent.addChild(plat_it)

                series_sorted = sorted(by_series.items(), key=lambda kv: kv[0].casefold())
                for series, count in series_sorted:
                    s_it = QTreeWidgetItem([series, str(int(count))])
                    s_it.setFlags(s_it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    s_it.setCheckState(0, Qt.CheckState.Unchecked)
                    plat_it.addChild(s_it)

        self.blockSignals(False)
        self.collapseAll()

        for i in range(self.topLevelItemCount()):
            it = self.topLevelItem(i)
            if it.text(0) in ("gcc", "mingw-gcc", "clang", "mingw-clang", "clang-cl", "msvc", "intel-icx", "intel-icc"):
                self.expandItem(it)

        self.resizeColumnToContents(0)
        self.resizeColumnToContents(1)
        self.selection_changed.emit()

    def selected_groups(self) -> list[tuple[str, str, str]]:

        out: list[tuple[str, str, str]] = []
        for i in range(self.topLevelItemCount()):
            fam_it = self.topLevelItem(i)
            fam = fam_it.text(0)
            for j in range(fam_it.childCount()):
                plat_it = fam_it.child(j)
                platform = plat_it.text(0)
                for k in range(plat_it.childCount()):
                    series_it = plat_it.child(k)
                    if series_it.checkState(0) == Qt.CheckState.Checked:
                        out.append((fam, platform, series_it.text(0)))
        return out

    def set_selected_groups(self, selected: set[str]) -> None:

        self.blockSignals(True)
        for i in range(self.topLevelItemCount()):
            fam_it = self.topLevelItem(i)
            fam = fam_it.text(0)
            for j in range(fam_it.childCount()):
                plat_it = fam_it.child(j)
                platform = plat_it.text(0)
                for k in range(plat_it.childCount()):
                    series_it = plat_it.child(k)
                    series = series_it.text(0)
                    key = f"{fam}|{platform}|{series}"
                    series_it.setCheckState(0, Qt.CheckState.Checked if key in selected else Qt.CheckState.Unchecked)
        self.blockSignals(False)
        self.selection_changed.emit()

    def set_check_for_visible(self, checked: bool) -> None:

        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.blockSignals(True)
        for i in range(self.topLevelItemCount()):
            fam_it = self.topLevelItem(i)
            if fam_it.isHidden():
                continue
            for j in range(fam_it.childCount()):
                plat_it = fam_it.child(j)
                if plat_it.isHidden():
                    continue
                for k in range(plat_it.childCount()):
                    series_it = plat_it.child(k)
                    if series_it.isHidden():
                        continue
                    series_it.setCheckState(0, state)
        self.blockSignals(False)
        self.selection_changed.emit()


class GroupExtraFlagsDialog(QDialog):

    def __init__(self, group_key: str, initial_flags: str, parent: QWidget | None = None):

        super().__init__(parent)
        self.setWindowTitle("Extra compiler flags (group)")
        self.setModal(True)

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Group:"))

        lab = QLabel(group_key)
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(lab)

        root.addWidget(QLabel("Extra flags (space-separated; newlines will be normalized):"))
        self._edit = QPlainTextEdit()
        self._edit.setPlaceholderText("Example: -DTESTING=1 -Wno-error")
        self._edit.setPlainText(str(initial_flags or ""))
        root.addWidget(self._edit)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def flags_text(self) -> str:

        return self._edit.toPlainText()


class LibraryRuleDialog(QDialog):



    def __init__(
        self,
        *,
        title: str,
        initial: LibraryRule | None,
        families: list[str],
        compiler_ids: list[str],
        libraries: list[CeLibraryInfo],
        parent: QWidget | None = None,
    ):

        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)

        self._families = list(families or [])
        self._compiler_ids = list(compiler_ids or [])
        self._libs = list(libraries or [])
        self._lib_by_id: dict[str, CeLibraryInfo] = {l.id: l for l in self._libs if l.id}

        root = QVBoxLayout(self)
        self._form = QFormLayout()
        root.addLayout(self._form)

        self.combo_scope = QComboBox()
        self.combo_scope.addItem("All compilers", "all")
        self.combo_scope.addItem("Compiler family", "family")
        self.combo_scope.addItem("Specific compiler id", "compiler")
        self._form.addRow("Scope", self.combo_scope)

        self.combo_family = QComboBox()
        self.combo_family.setEditable(True)
        self.combo_family.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo_family.setPlaceholderText("Example: gcc")
        self.combo_family.addItems(self._families)
        self._form.addRow("Family", self.combo_family)

        self.edit_compiler_id = QLineEdit()
        self.edit_compiler_id.setPlaceholderText("Example: g132")
        if self._compiler_ids:
            comp = QCompleter(self._compiler_ids)
            comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            comp.setFilterMode(Qt.MatchFlag.MatchContains)
            self.edit_compiler_id.setCompleter(comp)
        self._form.addRow("Compiler id", self.edit_compiler_id)

        self.combo_lib = QComboBox()
        self.combo_lib.setEditable(True)
        self.combo_lib.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo_lib.setPlaceholderText("Library id (example: fmt)")
        for l in self._libs:

            disp = f"{l.name} [{l.id}]" if l.name and l.name != l.id else l.id
            self.combo_lib.addItem(disp, l.id)
        self._form.addRow("Library", self.combo_lib)

        self.combo_version = QComboBox()
        self.combo_version.setEditable(True)
        self.combo_version.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo_version.setPlaceholderText("Version (example: 10.2.1)")
        self._form.addRow("Version", self.combo_version)

        self.combo_scope.currentIndexChanged.connect(self._sync_scope_widgets)
        self.combo_lib.currentIndexChanged.connect(self._sync_versions_from_lib)
        self.combo_lib.lineEdit().textEdited.connect(lambda *_: self._sync_versions_from_lib())

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)


        init = initial or LibraryRule(scope="all", target="", lib_id="", version="")
        self._set_initial(init)

    def _set_initial(self, r: LibraryRule) -> None:

        scope = (r.scope or "all").strip().lower()
        if scope == "family":
            self.combo_scope.setCurrentIndex(1)
        elif scope == "compiler":
            self.combo_scope.setCurrentIndex(2)
        else:
            self.combo_scope.setCurrentIndex(0)

        if r.target and scope == "family":
            idx = self.combo_family.findText(r.target)
            if idx >= 0:
                self.combo_family.setCurrentIndex(idx)
            else:
                self.combo_family.setCurrentText(r.target)
        if r.target and scope == "compiler":
            self.edit_compiler_id.setText(r.target)


        if r.lib_id:
            idx = -1
            for i in range(self.combo_lib.count()):
                if str(self.combo_lib.itemData(i) or "") == r.lib_id:
                    idx = i
                    break
            if idx >= 0:
                self.combo_lib.setCurrentIndex(idx)
            else:

                self.combo_lib.setCurrentText(r.lib_id)

        self._sync_versions_from_lib()
        if r.version:
            self.combo_version.setCurrentText(r.version)

        self._sync_scope_widgets()

    def _sync_scope_widgets(self) -> None:

        scope = str(self.combo_scope.currentData() or "all")
        show_family = scope == "family"
        show_compiler = scope == "compiler"

        self.combo_family.setVisible(show_family)
        lab_fam = self._form.labelForField(self.combo_family)
        if lab_fam is not None:
            lab_fam.setVisible(show_family)

        self.edit_compiler_id.setVisible(show_compiler)
        lab_cid = self._form.labelForField(self.edit_compiler_id)
        if lab_cid is not None:
            lab_cid.setVisible(show_compiler)

    def _current_lib_id(self) -> str:

        idx = int(self.combo_lib.currentIndex())
        if idx >= 0:
            data = self.combo_lib.itemData(idx)
            if isinstance(data, str) and data.strip():
                return data.strip()

        txt = str(self.combo_lib.currentText() or "").strip()

        m = re.search(r"\[([^\[\]]+)\]\s*$", txt)
        if m:
            return str(m.group(1)).strip()
        return txt

    def _sync_versions_from_lib(self) -> None:

        lid = self._current_lib_id()
        cur_ver = str(self.combo_version.currentText() or "").strip()
        self.combo_version.blockSignals(True)
        self.combo_version.clear()
        info = self._lib_by_id.get(lid)
        if info is not None:
            self.combo_version.addItems(info.versions)
        if cur_ver:
            self.combo_version.setCurrentText(cur_ver)
        self.combo_version.blockSignals(False)

    def _on_accept(self) -> None:

        try:
            _ = self.rule()
        except Exception as e:
            QMessageBox.warning(self, "Invalid rule", str(e))
            return
        self.accept()

    def rule(self) -> LibraryRule:

        scope = str(self.combo_scope.currentData() or "all").strip().lower()
        if scope not in ("all", "family", "compiler"):
            scope = "all"

        target = ""
        if scope == "family":
            target = str(self.combo_family.currentText() or "").strip()
            if not target:
                raise ValueError("Pick a compiler family.")
        elif scope == "compiler":
            target = str(self.edit_compiler_id.text() or "").strip()
            if not target:
                raise ValueError("Enter a compiler id.")

        lid = _normalize_ce_library_id(self._current_lib_id())
        ver = _normalize_ce_library_version(str(self.combo_version.currentText() or ""))
        if not lid:
            raise ValueError("Enter a library id.")
        if not ver:
            raise ValueError("Enter a library version.")

        return LibraryRule(scope=scope, target=target, lib_id=lid, version=ver)


class CompilersPanel(QWidget):

    selection_changed = pyqtSignal()
    filter_changed = pyqtSignal(str)
    extra_flags_changed = pyqtSignal()

    def __init__(self, settings: AppSettings | None = None):

        super().__init__()
        self._settings = settings
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        uic.loadUi(str(Path(__file__).with_name("ui") / "compilers_panel.ui"), self)


        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Family / Platform / Series", "Count"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(18)
        self.tree.setExpandsOnDoubleClick(True)
        self.tree.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        f = QFont()
        f.setPointSize(13)
        self.tree.setFont(f)
        self.filter_edit.setFont(f)

        self.tree.itemChanged.connect(lambda *_: self.selection_changed.emit())
        self.filter_edit.textChanged.connect(self._apply_filter)
        self.btn_all.clicked.connect(lambda: self.set_check_for_visible(True))
        self.btn_none.clicked.connect(lambda: self.set_check_for_visible(False))


        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)

    def _item_group_triplet(self, it: QTreeWidgetItem | None) -> tuple[str, str, str] | None:

        if it is None:
            return None
        plat = it.parent()
        if plat is None:
            return None
        fam_it = plat.parent()
        if fam_it is None:
            return None
        fam = fam_it.text(0).strip()
        platform = plat.text(0).strip()
        series = it.text(0).strip()
        if not fam or not platform or not series:
            return None
        return fam, platform, series

    def _on_tree_context_menu(self, pos) -> None:

        if self._settings is None:
            return

        it = self.tree.itemAt(pos)
        trip = self._item_group_triplet(it)
        if trip is None:
            return

        fam, platform, series = trip
        key = f"{fam}|{platform}|{series}"
        cfg = _load_extra_flags_config(self._settings)
        cur = str(cfg.extra_by_group.get(key, "") or "")

        menu = QMenu(self)
        act_set = menu.addAction("Set extra flags for this group…")
        act_clear = None
        if cur:
            act_clear = menu.addAction("Clear extra flags for this group")
        menu.addSeparator()
        act_show = menu.addAction("Show effective args for this group…")

        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return

        if chosen == act_show:
            args = build_user_args_for_group(fam, platform, series, "c++17", cfg)
            QMessageBox.information(self, "Effective args (example std=c++17)", f"{key}\n\n{args}")
            return

        if chosen == act_clear and cur:
            by_group = dict(cfg.extra_by_group)
            by_group.pop(key, None)
            _save_extra_flags_by_group(self._settings, by_group)
            self.extra_flags_changed.emit()
            return

        if chosen == act_set:
            dlg = GroupExtraFlagsDialog(key, cur, parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            new_txt = _normalize_user_flags_text(dlg.flags_text())
            by_group = dict(cfg.extra_by_group)
            if new_txt:
                by_group[key] = new_txt
            else:
                by_group.pop(key, None)
            _save_extra_flags_by_group(self._settings, by_group)
            self.extra_flags_changed.emit()

    def _apply_filter(self, text: str) -> None:

        t = (text or "").strip().casefold()
        self.filter_changed.emit(text)

        if not t:
            for i in range(self.tree.topLevelItemCount()):
                fam_it = self.tree.topLevelItem(i)
                fam_it.setHidden(False)
                for j in range(fam_it.childCount()):
                    plat_it = fam_it.child(j)
                    plat_it.setHidden(False)
                    for k in range(plat_it.childCount()):
                        plat_it.child(k).setHidden(False)
            return

        for i in range(self.tree.topLevelItemCount()):
            fam_it = self.tree.topLevelItem(i)
            fam_txt = fam_it.text(0).casefold()
            fam_match = t in fam_txt

            any_platform_visible = False
            for j in range(fam_it.childCount()):
                plat_it = fam_it.child(j)
                plat_txt = plat_it.text(0).casefold()
                plat_match = fam_match or (t in plat_txt)

                any_series_visible = False
                for k in range(plat_it.childCount()):
                    series_it = plat_it.child(k)
                    series_txt = series_it.text(0).casefold()
                    match = plat_match or (t in series_txt)
                    series_it.setHidden(not match)
                    any_series_visible = any_series_visible or match

                plat_it.setHidden(not (plat_match or any_series_visible))
                if plat_match:

                    for k in range(plat_it.childCount()):
                        plat_it.child(k).setHidden(False)
                    self.tree.expandItem(plat_it)

                any_platform_visible = any_platform_visible or (not plat_it.isHidden())

            fam_it.setHidden(not (fam_match or any_platform_visible))
            if fam_match:

                for j in range(fam_it.childCount()):
                    plat_it = fam_it.child(j)
                    plat_it.setHidden(False)
                    for k in range(plat_it.childCount()):
                        plat_it.child(k).setHidden(False)
                self.tree.expandItem(fam_it)

    def selected_groups(self) -> list[tuple[str, str, str]]:

        out: list[tuple[str, str, str]] = []
        for i in range(self.tree.topLevelItemCount()):
            fam_it = self.tree.topLevelItem(i)
            fam = fam_it.text(0)
            for j in range(fam_it.childCount()):
                plat_it = fam_it.child(j)
                platform = plat_it.text(0)
                for k in range(plat_it.childCount()):
                    series_it = plat_it.child(k)
                    if series_it.checkState(0) == Qt.CheckState.Checked:
                        out.append((fam, platform, series_it.text(0)))
        return out

    def set_data(self, families_to_platform_to_series_counts: dict[str, dict[str, dict[str, int]]]) -> None:

        self.tree.blockSignals(True)
        self.tree.clear()

        for fam in sorted(families_to_platform_to_series_counts.keys(), key=family_sort_key):
            by_platform = families_to_platform_to_series_counts[fam]
            total = sum(int(v) for by_series in by_platform.values() for v in by_series.values())

            parent = QTreeWidgetItem([fam, str(total)])
            parent.setFlags(parent.flags() | Qt.ItemFlag.ItemIsAutoTristate | Qt.ItemFlag.ItemIsUserCheckable)
            parent.setCheckState(0, Qt.CheckState.Unchecked)
            self.tree.addTopLevelItem(parent)

            platforms = sorted(by_platform.items(), key=lambda kv: kv[0].casefold())
            for platform, by_series in platforms:
                plat_total = sum(int(v) for v in by_series.values())
                plat_item = QTreeWidgetItem([platform, str(int(plat_total))])
                plat_item.setFlags(plat_item.flags() | Qt.ItemFlag.ItemIsAutoTristate | Qt.ItemFlag.ItemIsUserCheckable)
                plat_item.setCheckState(0, Qt.CheckState.Unchecked)
                parent.addChild(plat_item)

                series_sorted = sorted(by_series.items(), key=lambda kv: kv[0].casefold())
                for series, count in series_sorted:
                    s_it = QTreeWidgetItem([series, str(int(count))])
                    s_it.setFlags(s_it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    s_it.setCheckState(0, Qt.CheckState.Unchecked)
                    plat_item.addChild(s_it)

        self.tree.blockSignals(False)
        self.tree.collapseAll()

        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.text(0) in ("gcc", "mingw-gcc", "clang", "mingw-clang", "clang-cl", "msvc", "intel-icx", "intel-icc"):
                self.tree.expandItem(it)

        self.tree.resizeColumnToContents(0)
        self.tree.resizeColumnToContents(1)
        self.selection_changed.emit()

    def set_selected_groups(self, selected: set[str]) -> None:

        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            fam_it = self.tree.topLevelItem(i)
            fam = fam_it.text(0)
            for j in range(fam_it.childCount()):
                plat_it = fam_it.child(j)
                platform = plat_it.text(0)
                for k in range(plat_it.childCount()):
                    series_it = plat_it.child(k)
                    series = series_it.text(0)
                    key = f"{fam}|{platform}|{series}"
                    series_it.setCheckState(0, Qt.CheckState.Checked if key in selected else Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)
        self.selection_changed.emit()

    def set_check_for_visible(self, checked: bool) -> None:

        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            fam_it = self.tree.topLevelItem(i)
            if fam_it.isHidden():
                continue
            for j in range(fam_it.childCount()):
                plat_it = fam_it.child(j)
                if plat_it.isHidden():
                    continue
                for k in range(plat_it.childCount()):
                    series_it = plat_it.child(k)
                    if series_it.isHidden():
                        continue
                    series_it.setCheckState(0, state)
        self.tree.blockSignals(False)
        self.selection_changed.emit()


class OptionsPanel(QWidget):

    run_clicked = pyqtSignal()
    abort_clicked = pyqtSignal()
    sample_clicked = pyqtSignal()
    preferences_clicked = pyqtSignal()
    session_changed = pyqtSignal()
    compile_commands_changed = pyqtSignal(str)
    pp_settings_changed = pyqtSignal()

    def __init__(self):

        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        uic.loadUi(str(Path(__file__).with_name("ui") / "options_panel.ui"), self)
        self.cpp_std.setCurrentText("c++17")

        self.btn_probe.clicked.connect(self.run_clicked.emit)
        self.btn_abort.clicked.connect(self.abort_clicked.emit)
        self.btn_sample.clicked.connect(self.sample_clicked.emit)
        if hasattr(self, "btn_preferences"):
            self.btn_preferences.clicked.connect(self.preferences_clicked.emit)
        self.cpp_std.currentIndexChanged.connect(self.session_changed.emit)
        if hasattr(self, "editCompileCommandsPath"):
            self.editCompileCommandsPath.textChanged.connect(self.session_changed.emit)
            self.editCompileCommandsPath.textChanged.connect(lambda s: self.compile_commands_changed.emit(str(s)))
        if hasattr(self, "btnBrowseCompileCommands"):
            self.btnBrowseCompileCommands.clicked.connect(self._browse_compile_commands)
        if hasattr(self, "editExtraIncludeDirs"):
            self.editExtraIncludeDirs.textChanged.connect(self._emit_pp_changed)
        if hasattr(self, "checkInlineOnce"):
            self.checkInlineOnce.stateChanged.connect(lambda *_: self._emit_pp_changed())
        if hasattr(self, "checkAutoCopy"):
            self.checkAutoCopy.stateChanged.connect(lambda *_: self._emit_pp_changed())
        if hasattr(self, "checkStripPragmaOnce"):
            self.checkStripPragmaOnce.stateChanged.connect(lambda *_: self._emit_pp_changed())
        if hasattr(self, "checkEmitLineDirectives"):
            self.checkEmitLineDirectives.stateChanged.connect(lambda *_: self._emit_pp_changed())
        if hasattr(self, "checkIncludeDebugComments"):
            self.checkIncludeDebugComments.stateChanged.connect(lambda *_: self._emit_pp_changed())

    def selected_cpp_std(self) -> str:

        return self.cpp_std.currentText().strip()

    def _emit_pp_changed(self) -> None:

        self.session_changed.emit()
        self.pp_settings_changed.emit()

    def compile_commands_path(self) -> str:

        if not hasattr(self, "editCompileCommandsPath"):
            return ""
        return (self.editCompileCommandsPath.text() or "").strip()

    def set_compile_commands_path(self, s: str) -> None:

        if hasattr(self, "editCompileCommandsPath"):
            self.editCompileCommandsPath.setText(s or "")

    def extra_include_dirs_text(self) -> str:

        if not hasattr(self, "editExtraIncludeDirs"):
            return ""
        return self.editExtraIncludeDirs.toPlainText()

    def set_extra_include_dirs_text(self, s: str) -> None:

        if hasattr(self, "editExtraIncludeDirs"):
            self.editExtraIncludeDirs.setPlainText(s or "")

    def inline_once(self) -> bool:

        return bool(self.checkInlineOnce.isChecked()) if hasattr(self, "checkInlineOnce") else True

    def set_inline_once(self, v: bool) -> None:

        if hasattr(self, "checkInlineOnce"):
            self.checkInlineOnce.setChecked(bool(v))

    def auto_copy(self) -> bool:

        return bool(self.checkAutoCopy.isChecked()) if hasattr(self, "checkAutoCopy") else True

    def set_auto_copy(self, v: bool) -> None:

        if hasattr(self, "checkAutoCopy"):
            self.checkAutoCopy.setChecked(bool(v))

    def strip_pragma_once(self) -> bool:

        return bool(self.checkStripPragmaOnce.isChecked()) if hasattr(self, "checkStripPragmaOnce") else True

    def set_strip_pragma_once(self, v: bool) -> None:

        if hasattr(self, "checkStripPragmaOnce"):
            self.checkStripPragmaOnce.setChecked(bool(v))

    def include_debug_comments(self) -> bool:

        return bool(self.checkIncludeDebugComments.isChecked()) if hasattr(self, "checkIncludeDebugComments") else False

    def set_include_debug_comments(self, v: bool) -> None:

        if hasattr(self, "checkIncludeDebugComments"):
            self.checkIncludeDebugComments.setChecked(bool(v))

    def emit_line_directives(self) -> bool:
        return bool(self.checkEmitLineDirectives.isChecked()) if hasattr(self, "checkEmitLineDirectives") else True

    def set_emit_line_directives(self, v: bool) -> None:
        if hasattr(self, "checkEmitLineDirectives"):
            self.checkEmitLineDirectives.setChecked(bool(v))

    def _browse_compile_commands(self) -> None:

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select compile_commands.json",
            "",
            "JSON Files (*.json);;All Files (*)",
        )
        if path and hasattr(self, "editCompileCommandsPath"):
            self.editCompileCommandsPath.setText(path)


class LogPanel(QWidget):

    def __init__(self, mono: QFont):

        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        uic.loadUi(str(Path(__file__).with_name("ui") / "log_panel.ui"), self)
        self.text.document().setDefaultStyleSheet(default_rich_css(mono))
        self.btn_clear.clicked.connect(lambda: self.text.setHtml(""))

    def set_ansi_text(self, s: str) -> None:

        s2 = s.rstrip("\n")
        self.text.setHtml(wrap_pre_html(s2, "logline"))
        self.text.moveCursor(QTextCursor.MoveOperation.End)

    def append_ansi(self, s: str) -> None:

        s2 = s.rstrip("\n")
        cur = self.text.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.insertHtml(wrap_pre_html(s2, "logline"))
        cur.insertHtml("<br/>")
        self.text.setTextCursor(cur)
        self.text.moveCursor(QTextCursor.MoveOperation.End)


class ResultsPanel(QWidget):

    result_row_changed = pyqtSignal(int)

    def __init__(self, mono: QFont):

        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        uic.loadUi(str(Path(__file__).with_name("ui") / "results_panel.ui"), self)
        self.list.currentRowChanged.connect(self.result_row_changed.emit)
        self.details.document().setDefaultStyleSheet(default_rich_css(mono))
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([360, 900])

    def set_details_ansi(self, s: str) -> None:

        self.details.setHtml(wrap_pre_html(s.rstrip("\n"), "details"))


