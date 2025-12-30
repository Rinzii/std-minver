"""Core helpers and shared utilities."""

import hashlib
import http.client
import json
import logging
import re
import shlex
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from PyQt6 import uic
from PyQt6.QtCore import Qt, QEvent, QObject, QThread, pyqtSignal, QSettings, QTimer
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QTextEdit,
    QPlainTextEdit,
    QTextBrowser,
    QSizePolicy,
    QDockWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QFileDialog,
    QMessageBox,
    QLineEdit,
    QDialog,
    QDialogButtonBox,
    QStatusBar,
    QProgressBar,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QComboBox,
    QMenu,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QAbstractItemView,
    QCompleter,
)
from PyQt6.Qsci import QsciScintilla, QsciLexerCPP


LOG = logging.getLogger("cetest")

CODE_TIMEOUT = -100
CODE_TRANSPORT_ERROR = -101
CODE_ABORTED = -102


class CancelledByUser(Exception):


    pass


def setup_logging() -> None:


    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


CPP_SAMPLE = "\nint main() {\n    \n}\n"


def decode_text_file_for_editor(data: bytes) -> tuple[str, str]:

    if b"\x00" in data:
        raise ValueError("This looks like a binary file (contains NUL bytes).")

    for enc in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            pass

    return data.decode("utf-8", errors="replace"), "utf-8 (replacement)"


def is_dark_palette(palette) -> bool:

    c = palette.color(palette.ColorRole.Window)
    return (c.red() * 0.2126 + c.green() * 0.7152 + c.blue() * 0.0722) < 128.0


def choose_mono_font(point_size: int) -> QFont:

    preferred = ["Menlo", "SF Mono", "Monaco", "DejaVu Sans Mono", "Liberation Mono", "Courier New"]
    families = set(QFontDatabase.families())
    for name in preferred:
        if name in families:
            f = QFont(name, point_size)
            f.setStyleHint(QFont.StyleHint.Monospace)
            f.setFixedPitch(True)
            return f
    f = QFont()
    f.setPointSize(point_size)
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setFixedPitch(True)
    return f


def load_json(path: Path) -> dict[str, Any]:

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def ensure_theme_json(path: Path) -> None:

    if path.exists():
        return
    data = {
        "dark": {
            "bg": "#1e1e1e",
            "fg": "#d4d4d4",
            "margin_bg": "#252526",
            "margin_fg": "#9aa0a6",
            "caret_fg": "#d4d4d4",
            "caret_line": "#2a2d2e",
            "selection_bg": "#264f78",
            "selection_fg": "#ffffff",
            "kw": "#c586c0",
            "ty": "#4ec9b0",
            "num": "#b5cea8",
            "str": "#ce9178",
            "com": "#6a9955",
            "pp": "#9cdcfe",
            "op": "#d4d4d4",
        },
        "light": {
            "bg": "#ffffff",
            "fg": "#1e1e1e",
            "margin_bg": "#f3f3f3",
            "margin_fg": "#666666",
            "caret_fg": "#1e1e1e",
            "caret_line": "#f5f5f5",
            "selection_bg": "#add6ff",
            "selection_fg": "#000000",
            "kw": "#7a3e9d",
            "ty": "#0b7d6e",
            "num": "#098658",
            "str": "#a31515",
            "com": "#008000",
            "pp": "#0451a5",
            "op": "#1e1e1e",
        },
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _escape_html(s: str) -> str:

    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


ANSI_RE = re.compile(r"\x1b\[([0-9;]*)([A-Za-z])")


@dataclass
class AnsiState:

    fg: str | None = None
    bg: str | None = None
    bold: bool = False
    underline: bool = False
    inverse: bool = False

    def reset(self) -> None:

        self.fg = None
        self.bg = None
        self.bold = False
        self.underline = False
        self.inverse = False

    def style_css(self) -> str:

        fg = self.fg
        bg = self.bg
        if self.inverse:
            fg, bg = bg, fg
        parts: list[str] = []
        if fg is not None:
            parts.append(f"color: {fg};")
        if bg is not None:
            parts.append(f"background-color: {bg};")
        if self.bold:
            parts.append("font-weight: 600;")
        if self.underline:
            parts.append("text-decoration: underline;")
        return " ".join(parts)


FG_COLORS = {
    30: "#000000",
    31: "#cc3333",
    32: "#22aa22",
    33: "#bb9900",
    34: "#3366cc",
    35: "#aa44aa",
    36: "#22aaaa",
    37: "#cccccc",
    90: "#808080",
    91: "#ff5555",
    92: "#55dd55",
    93: "#ffd866",
    94: "#6699ff",
    95: "#ff77ff",
    96: "#55ffff",
    97: "#ffffff",
}

BG_COLORS = {
    40: "#000000",
    41: "#660000",
    42: "#006600",
    43: "#665500",
    44: "#000066",
    45: "#660066",
    46: "#006666",
    47: "#888888",
    100: "#444444",
    101: "#aa0000",
    102: "#00aa00",
    103: "#aa8800",
    104: "#0000aa",
    105: "#aa00aa",
    106: "#00aaaa",
    107: "#dddddd",
}


def ansi_to_html_spans(text: str) -> str:


    # Qt rich text won't render ANSI escapes; convert them into styled spans.

    state = AnsiState()
    out: list[str] = []
    pos = 0

    def emit(seg: str) -> None:

        if not seg:
            return
        esc = _escape_html(seg)
        css = state.style_css()
        if css:
            out.append(f"<span style='{css}'>{esc}</span>")
        else:
            out.append(esc)

    for m in ANSI_RE.finditer(text):
        start, end = m.span()
        params = m.group(1)
        cmd = m.group(2)

        emit(text[pos:start])

        if cmd == "K":
            pos = end
            continue
        if cmd != "m":
            pos = end
            continue

        if not params:
            codes = [0]
        else:
            parts = params.split(";")
            codes = []
            for p in parts:
                if p.strip() == "":
                    continue
                try:
                    codes.append(int(p, 10))
                except ValueError:
                    pass
            if not codes:
                codes = [0]

        for c in codes:
            if c == 0:
                state.reset()
            elif c == 1:
                state.bold = True
            elif c == 22:
                state.bold = False
            elif c == 4:
                state.underline = True
            elif c == 24:
                state.underline = False
            elif c == 7:
                state.inverse = True
            elif c == 27:
                state.inverse = False
            elif c == 39:
                state.fg = None
            elif c == 49:
                state.bg = None
            elif c in FG_COLORS:
                state.fg = FG_COLORS[c]
            elif c in BG_COLORS:
                state.bg = BG_COLORS[c]

        pos = end

    emit(text[pos:])
    return "".join(out)


def wrap_pre_html(text: str, css_class: str) -> str:

    return f"<pre class='{css_class}'>{ansi_to_html_spans(text)}</pre>"


def default_rich_css(mono: QFont) -> str:

    fam = mono.family().replace("'", "\\'")
    pt = mono.pointSize()
    return (
        "pre { margin: 0; white-space: pre-wrap; }"
        "pre.logline { margin: 0; }"
        "pre.details { margin: 0; }"
        f"pre, code, tt {{ font-family: '{fam}'; font-size: {pt}pt; }}"
    )


@dataclass(frozen=True)
class Theme:

    bg: QColor
    fg: QColor
    margin_bg: QColor
    margin_fg: QColor
    caret_fg: QColor
    caret_line: QColor
    selection_bg: QColor
    selection_fg: QColor
    kw: QColor
    ty: QColor
    num: QColor
    s: QColor
    com: QColor
    pp: QColor
    op: QColor


class ThemeManager(QObject):

    theme_changed = pyqtSignal(object)

    def __init__(self, theme_path: Path):

        super().__init__()
        self._theme_path = theme_path
        ensure_theme_json(self._theme_path)
        self._data = load_json(self._theme_path)
        if "dark" not in self._data or "light" not in self._data:
            raise ValueError("theme.json must contain top-level keys: 'dark' and 'light'")
        self._current_key: str | None = None
        self._mode: str = "auto"

    def set_mode(self, mode: str) -> None:

        m = (mode or "").strip().lower()
        if m not in ("auto", "dark", "light"):
            m = "auto"
        if m != self._mode:
            self._mode = m
            self.theme_changed.emit(self.current_theme())

    def set_theme_path(self, path: Path) -> None:

        self._theme_path = path
        ensure_theme_json(self._theme_path)
        self._data = load_json(self._theme_path)
        self._current_key = None
        self.theme_changed.emit(self.current_theme())

    def current_theme(self) -> Theme:

        if self._mode in ("dark", "light"):
            key = self._mode
        else:
            key = "dark" if is_dark_palette(QApplication.palette()) else "light"
        self._current_key = key
        return self._theme_from_key(key)

    def refresh(self) -> None:

        if self._mode in ("dark", "light"):
            return
        key = "dark" if is_dark_palette(QApplication.palette()) else "light"
        if key != self._current_key:
            self._current_key = key
            LOG.debug("Theme changed to %s", key)
            self.theme_changed.emit(self._theme_from_key(key))

    def _theme_from_key(self, key: str) -> Theme:

        t = self._data[key]

        def qc(name: str) -> QColor:

            v = t.get(name)
            if not isinstance(v, str):
                raise ValueError(f"theme.json missing/invalid '{key}.{name}'")
            return QColor(v)

        return Theme(
            bg=qc("bg"),
            fg=qc("fg"),
            margin_bg=qc("margin_bg"),
            margin_fg=qc("margin_fg"),
            caret_fg=qc("caret_fg"),
            caret_line=qc("caret_line"),
            selection_bg=qc("selection_bg"),
            selection_fg=qc("selection_fg"),
            kw=qc("kw"),
            ty=qc("ty"),
            num=qc("num"),
            s=qc("str"),
            com=qc("com"),
            pp=qc("pp"),
            op=qc("op"),
        )


