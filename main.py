"""
CETest: a small PyQt6 GUI for probing Compiler Explorer compilers.

You select a set of (compiler family, target arch) groups, then the app calls
Compiler Explorer's API to find the *oldest* compiler in each group that still
accepts the current source snippet for the selected C++ standard.

Implementation notes:
- Uses QThread workers to keep the UI responsive while calling the CE API.
- Uses a small in-memory compile cache to avoid repeating identical requests.
- Uses a binary search over compilers sorted by semver to minimize API calls.
"""

import hashlib
import http.client
import json
import logging
import re
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

from PyQt6.QtCore import Qt, QEvent, QObject, QThread, pyqtSignal, QSettings, QTimer
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QAction, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QListWidget,
    QListWidgetItem,
    QTextEdit,
    QTextBrowser,
    QSizePolicy,
    QDockWidget,
    QMenuBar,
    QTreeWidget,
    QTreeWidgetItem,
    QSplitter,
    QFileDialog,
    QMessageBox,
    QLineEdit,
    QToolBar,
    QStatusBar,
    QProgressBar,
)
from PyQt6.Qsci import QsciScintilla, QsciLexerCPP


LOG = logging.getLogger("cetest")

CODE_TIMEOUT = -100
CODE_TRANSPORT_ERROR = -101
CODE_ABORTED = -102


class CancelledByUser(Exception):
    """Raised when the user requests cancellation (abort button / shutdown)."""
    pass


def setup_logging() -> None:
    """Configure stderr logging for both console debugging and in-app diagnostics."""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


CPP_SAMPLE = "\nint main() {\n    \n}\n"


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
    # CE output may include ANSI escape sequences; convert those into inline
    # styled HTML spans so Qt rich-text widgets can display them.
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
        ensure_theme_json(theme_path)
        self._data = load_json(theme_path)
        if "dark" not in self._data or "light" not in self._data:
            raise ValueError("theme.json must contain top-level keys: 'dark' and 'light'")
        self._current_key: str | None = None

    def current_theme(self) -> Theme:
        key = "dark" if is_dark_palette(QApplication.palette()) else "light"
        self._current_key = key
        return self._theme_from_key(key)

    def refresh(self) -> None:
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


@dataclass(frozen=True)
class CompilerInfo:
    id: str
    name: str
    lang: str
    compiler_type: str
    semver: str | None
    instruction_set: str | None


def parse_semver_key(s: str | None) -> tuple[int, int, int, int, str]:
    # Turn a semver-ish string into a tuple suitable for sorting newest->oldest.
    # CE's semver field is not strict SemVer and may contain strings like
    # "trunk"/"git"/"nightly" which we treat as very new.
    if not s:
        return (0, 0, 0, 0, "")
    st = s.strip().lower()
    if any(k in st for k in ("trunk", "head", "git", "snapshot", "nightly", "tip")) and not re.match(r"^\d", st):
        return (9999, 9999, 9999, 9999, st)
    m = re.match(r"^\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$", s.strip())
    if not m:
        return (0, 0, 0, 0, s.strip())
    a = int(m.group(1) or 0)
    b = int(m.group(2) or 0)
    c = int(m.group(3) or 0)
    rest = (m.group(4) or "").strip()
    tweak = 0
    if "trunk" in rest.lower() or "git" in rest.lower():
        tweak = 1
    return (a, b, c, tweak, rest)


def normalize_arch(instruction_set: str | None) -> str:
    if not instruction_set:
        return "unknown"
    s = instruction_set.strip()
    return s if s else "unknown"


def parse_arch_from_name(name: str | None) -> str | None:
    if not name:
        return None
    s = name.lower()
    patterns = [
        ("x86-64", r"\bx86[-_]?64\b|\bamd64\b|\bx64\b"),
        ("x86", r"\bx86\b|\bi686\b"),
        ("aarch64", r"\baarch64\b|\barm64\b"),
        ("arm32", r"\barmv7\b|\barm32\b|\barm\b(?!64)"),
        ("riscv64", r"\briscv64\b"),
        ("ppc64le", r"\bppc64le\b"),
        ("s390x", r"\bs390x\b"),
        ("wasm32", r"\bwasm32\b"),
        ("wasm64", r"\bwasm64\b"),
        ("mips64", r"\bmips64\b"),
        ("mips", r"\bmips\b"),
        ("avr", r"\bavr\b"),
        ("6502", r"\b6502\b"),
    ]
    for canon, rx in patterns:
        if re.search(rx, s):
            return canon
    return None


def arch_label(c: CompilerInfo) -> str:
    a = normalize_arch(c.instruction_set)
    if a != "unknown":
        return a
    p = parse_arch_from_name(c.name)
    return p if p is not None else "unknown"


def normalize_family_token(token: str) -> str:
    ct = (token or "").strip().lower()
    if not ct:
        return "unknown"

    if "nvc++" in ct or "nvhpc" in ct:
        return "nvc++"

    if "icpx" in ct:
        return "icx"
    if "icx" in ct:
        return "icx"
    if "icc" in ct:
        return "icc"

    if "gcc" in ct or "g++" in ct or ct in ("gpp", "gxx", "gnu"):
        return "gcc"
    if "clang" in ct:
        return "clang"

    if "win32" in ct or "msvc" in ct or "visual" in ct or ct in ("cl", "vc", "win32-vc"):
        return "msvc"

    if "icx" in ct or "icc" in ct or "intel" in ct:
        return "intel"

    return ct


def guess_family(c: CompilerInfo) -> str:
    # Group compilers into consistent "families" (gcc/clang/msvc/etc). The CE
    # field is sometimes ambiguous, so we also use name/id heuristics.
    fam = normalize_family_token(c.compiler_type)
    if fam != "unknown" and fam != "intel":
        return fam

    hay = f"{c.name} {c.id} {c.compiler_type}".lower()

    if "nvc++" in hay or "nvhpc" in hay:
        return "nvc++"

    if re.search(r"\bicpx\b", hay) or "oneapi" in hay or "dpc++" in hay or "dpcpp" in hay:
        return "icx"
    if re.search(r"\bicx\b", hay) or "intel icx" in hay:
        return "icx"
    if re.search(r"\bicc\b", hay) or "intel icc" in hay:
        return "icc"

    if "g++" in hay or re.search(r"\bgcc\b", hay) or "gcc-" in hay or "mingw" in hay:
        return "gcc"
    if "clang" in hay:
        return "clang"

    if "msvc" in hay or "win32" in hay or "visual studio" in hay or "win32-vc" in hay or re.search(r"\bcl\b", hay):
        return "msvc"

    if c.id and c.id[0].lower() == "g":
        return "gcc"
    if c.id and c.id[0].lower() == "c":
        return "clang"

    if fam == "intel":
        return "intel"
    return "unknown"


def family_sort_key(fam: str) -> tuple[int, str]:
    pri = {"gcc": 0, "clang": 1, "msvc": 2, "icx": 3, "icc": 4, "nvc++": 5}
    return (pri.get(fam, 50), fam.casefold())


def std_flags_for_family(fam: str, std: str) -> str:
    f = fam.strip().lower()
    s = std.strip().lower()

    if f == "msvc":
        m = {
            "c++11": "/std:c++14",
            "c++14": "/std:c++14",
            "c++17": "/std:c++17",
            "c++20": "/std:c++20",
            "c++23": "/std:c++latest",
            "c++26": "/std:c++latest",
        }
        std_part = m.get(s, "/std:c++17")
        return f"{std_part} /Zs"

    return f"-std={s} -fsyntax-only"


class RateLimiter:
    """Thread-safe rate limiter enforcing a minimum interval between API calls."""

    def __init__(self, min_interval_s: float):
        self._min = float(min_interval_s)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self, abort_event: threading.Event | None = None) -> None:
        # Simple process-wide rate limiting so we don't hammer the CE API.
        with self._lock:
            if abort_event is not None and abort_event.is_set():
                raise CancelledByUser()
            now = time.monotonic()
            dt = now - self._last
            if dt < self._min:
                sleep_s = self._min - dt
                if abort_event is None:
                    time.sleep(sleep_s)
                else:
                    end = time.monotonic() + sleep_s
                    while time.monotonic() < end:
                        if abort_event.is_set():
                            raise CancelledByUser()
                        time.sleep(0.02)
            self._last = time.monotonic()


class AppSettings:
    """Thin wrapper over QSettings for persisting window layout + session state."""

    ORG = "IanTools"
    APP = "CETest"

    K_GEOMETRY = "main/geometry"
    K_STATE = "main/windowState"

    K_EDITOR = "session/editorText"
    K_CPPSTD = "session/cppStd"
    K_SELECTED = "session/selectedGroups"
    K_COMPILERS_FILTER = "session/compilersFilter"
    K_RESULTS_SPLIT = "session/resultsSplitterSizes"

    K_LAST_RESULT_ROW = "session/lastResultRow"
    K_LAST_REPORT_JSON = "session/lastReportJson"
    K_LAST_REPORT_HTML = "session/lastReportHtml"

    def __init__(self):
        self._s = QSettings(self.ORG, self.APP)

    def set_value(self, key: str, value: Any) -> None:
        self._s.setValue(key, value)

    def get_value(self, key: str, default: Any = None) -> Any:
        return self._s.value(key, default)

    def reset_all(self) -> None:
        keys = (
            self.K_GEOMETRY,
            self.K_STATE,
            self.K_EDITOR,
            self.K_CPPSTD,
            self.K_SELECTED,
            self.K_COMPILERS_FILTER,
            self.K_RESULTS_SPLIT,
            self.K_LAST_RESULT_ROW,
            self.K_LAST_REPORT_JSON,
            self.K_LAST_REPORT_HTML,
        )
        for k in keys:
            self._s.remove(k)


class CeClient:
    """Minimal Compiler Explorer API client with retry/backoff and caching."""

    def __init__(self, base_url: str = "https://godbolt.org", min_request_interval_s: float = 0.12):
        self.base_url = base_url.rstrip("/")
        self._rate = RateLimiter(min_request_interval_s)
        self._compile_cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout_s: float = 45.0,
        abort_event: threading.Event | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        # Retry transient CE issues (429, 5xx, timeouts) so the UI is resilient.
        max_retries = 6
        backoff = 0.6

        for attempt in range(max_retries):
            if abort_event is not None and abort_event.is_set():
                raise CancelledByUser()

            self._rate.wait(abort_event=abort_event)

            LOG.debug("HTTP %s %s attempt=%d timeout=%.1fs", method, url, attempt + 1, timeout_s)
            req = urllib.request.Request(url, data=data, headers=headers, method=method)

            try:
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    raw = resp.read()
                if abort_event is not None and abort_event.is_set():
                    raise CancelledByUser()
                return json.loads(raw.decode("utf-8"))
            except CancelledByUser:
                raise
            except urllib.error.HTTPError as e:
                code = int(getattr(e, "code", 0) or 0)
                body_text = ""
                try:
                    body_text = e.read().decode("utf-8", errors="replace")
                except Exception:
                    body_text = ""
                retryable = code in (408, 429, 500, 502, 503, 504)
                if not retryable or attempt == max_retries - 1:
                    LOG.error("HTTPError code=%d url=%s body=%s", code, url, body_text[:4000])
                    raise
                ra = e.headers.get("Retry-After") if hasattr(e, "headers") and e.headers else None
                if ra is not None:
                    try:
                        wait_s = float(ra)
                    except ValueError:
                        wait_s = backoff
                else:
                    wait_s = backoff
                LOG.warning("Retrying after HTTP %d in %.2fs", code, wait_s)
                self._sleep_abortable(wait_s, abort_event)
                backoff = min(backoff * 2.0, 6.0)
            except (TimeoutError, socket.timeout) as e:
                if attempt == max_retries - 1:
                    LOG.error("Timeout url=%s err=%s", url, str(e))
                    raise
                LOG.warning("Retrying after timeout in %.2fs: %s", backoff, str(e))
                self._sleep_abortable(backoff, abort_event)
                backoff = min(backoff * 2.0, 6.0)
            except (http.client.RemoteDisconnected, ConnectionResetError, ConnectionAbortedError, ConnectionError) as e:
                if attempt == max_retries - 1:
                    LOG.error("Connection error url=%s err=%s", url, str(e))
                    raise
                LOG.warning("Retrying after connection error in %.2fs: %s", backoff, str(e))
                self._sleep_abortable(backoff, abort_event)
                backoff = min(backoff * 2.0, 6.0)
            except urllib.error.URLError as e:
                if attempt == max_retries - 1:
                    LOG.error("URLError url=%s err=%s", url, str(e))
                    raise
                LOG.warning("Retrying after URLError in %.2fs: %s", backoff, str(e))
                self._sleep_abortable(backoff, abort_event)
                backoff = min(backoff * 2.0, 6.0)

        raise RuntimeError("unreachable")

    @staticmethod
    def _sleep_abortable(secs: float, abort_event: threading.Event | None) -> None:
        if abort_event is None:
            time.sleep(secs)
            return
        end = time.monotonic() + secs
        while time.monotonic() < end:
            if abort_event.is_set():
                raise CancelledByUser()
            time.sleep(0.02)

    def list_compilers_cpp(self, abort_event: threading.Event | None = None) -> list["CompilerInfo"]:
        lang_id = quote("c++", safe="")
        fields = "id,name,lang,compilerType,semver,instructionSet"
        items = self._request_json(
            "GET",
            f"/api/compilers/{lang_id}?fields={fields}",
            timeout_s=45.0,
            abort_event=abort_event,
        )
        if isinstance(items, dict) and "compilers" in items and isinstance(items["compilers"], list):
            items = items["compilers"]
        if not isinstance(items, list):
            return []

        out: list[CompilerInfo] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            cid = str(it.get("id") or "")
            name = str(it.get("name") or "")
            lang = str(it.get("lang") or "")
            ct = str(it.get("compilerType") or "")
            semver = it.get("semver")
            instruction_set = it.get("instructionSet")

            if not cid or lang != "c++":
                continue

            nm = name.lower()
            if "ex-wine" in nm:
                continue

            out.append(
                CompilerInfo(
                    id=cid,
                    name=name,
                    lang=lang,
                    compiler_type=ct,
                    semver=(str(semver) if semver is not None else None),
                    instruction_set=(str(instruction_set) if instruction_set is not None else None),
                )
            )

        LOG.debug("Loaded %d raw C++ compilers", len(out))
        return out

    def compile_cached(
        self,
        compiler_id: str,
        source: str,
        user_arguments: str,
        abort_event: threading.Event | None = None,
        timeout_s: float = 45.0,
    ) -> dict[str, Any]:
        # Cache by (compiler id, flags, source hash). This reduces requests
        # because the binary search may revisit previously tested indices.
        h = hashlib.sha1(source.encode("utf-8")).hexdigest()
        key = (compiler_id, user_arguments, h)
        hit = self._compile_cache.get(key)
        if hit is not None:
            LOG.debug("Compile cache hit compiler_id=%s", compiler_id)
            return hit

        payload = {
            "source": source,
            "options": {
                "userArguments": user_arguments,
                "compilerOptions": {"skipAsm": True, "executorRequest": False, "overrides": []},
                "filters": {
                    "binary": False,
                    "binaryObject": False,
                    "commentOnly": True,
                    "demangle": True,
                    "directives": True,
                    "execute": False,
                    "intel": True,
                    "labels": True,
                    "libraryCode": False,
                    "trim": True,
                    "debugCalls": False,
                },
                "tools": [],
                "libraries": [],
            },
            "lang": "c++",
            "allowStoreCodeDebug": True,
        }

        LOG.debug("Compile compiler_id=%s args=%s source_len=%d", compiler_id, user_arguments, len(source))
        resp = self._request_json(
            "POST",
            f"/api/compiler/{compiler_id}/compile",
            payload,
            timeout_s=timeout_s,
            abort_event=abort_event,
        )
        self._compile_cache[key] = resp
        return resp


class CeLoadWorker(QObject):
    """Worker object run in a QThread to load the compiler list without blocking the UI."""

    loaded = pyqtSignal(object)
    failed = pyqtSignal(str)
    aborted = pyqtSignal()

    def __init__(self, ce: CeClient, abort_event: threading.Event):
        super().__init__()
        self._ce = ce
        self._abort = abort_event

    def run(self):
        try:
            self.loaded.emit(self._ce.list_compilers_cpp(abort_event=self._abort))
        except CancelledByUser:
            self.aborted.emit()
        except Exception as e:
            LOG.exception("Compiler load failed")
            self.failed.emit(str(e))


@dataclass(frozen=True)
class CeAttempt:
    """One compile attempt against a specific compiler (id+version) for a given group."""

    arch: str
    compiler_type: str
    compiler_id: str
    compiler_name: str
    semver: str | None
    code: int
    stderr_text: str

    def ok(self) -> bool:
        return self.code == 0


@dataclass(frozen=True)
class CeGroupSummary:
    """Aggregate probe results for a single (family, arch) group."""

    arch: str
    compiler_type: str
    highest_supported: CeAttempt | None
    lowest_supported: CeAttempt | None
    first_failure: CeAttempt | None
    attempts: list[CeAttempt]
    inconclusive_reason: str | None = None


def stderr_text_from_resp(resp: dict[str, Any]) -> str:
    """Extract a readable stderr string from a Compiler Explorer compile response."""
    parts = resp.get("stderr", [])
    if not isinstance(parts, list):
        return ""
    lines: list[str] = []
    for it in parts:
        if isinstance(it, dict) and "text" in it:
            lines.append(str(it["text"]))
        elif isinstance(it, str):
            lines.append(it)
    return "\n".join(lines).strip()


class CeProbeWorker(QObject):
    """Worker that probes all selected groups and emits incremental results to the UI."""

    group_done = pyqtSignal(object)
    finished = pyqtSignal()
    failed = pyqtSignal(str)
    aborted = pyqtSignal()

    def __init__(
        self,
        ce: CeClient,
        jobs: list[tuple[str, str, list[CompilerInfo]]],
        source: str,
        cpp_std: str,
        abort_event: threading.Event,
    ):
        super().__init__()
        self._ce = ce
        self._jobs = jobs
        self._source = source
        self._cpp_std = cpp_std
        self._abort = abort_event

    def _cancelled(self) -> bool:
        if self._abort.is_set():
            return True
        t = QThread.currentThread()
        return bool(t and t.isInterruptionRequested())

    def run(self):
        try:
            for arch, compiler_type, compilers in self._jobs:
                if self._cancelled():
                    raise CancelledByUser()

                try:
                    LOG.debug("Probe start group fam=%s arch=%s candidates=%d", compiler_type, arch, len(compilers))
                    summary = self._probe_group_binary(arch, compiler_type, compilers)
                    self.group_done.emit(summary)
                except CancelledByUser:
                    raise
                except Exception as e:
                    LOG.exception("Group probe failed fam=%s arch=%s", compiler_type, arch)
                    summary = CeGroupSummary(
                        arch=arch,
                        compiler_type=compiler_type,
                        highest_supported=None,
                        lowest_supported=None,
                        first_failure=CeAttempt(
                            arch=arch,
                            compiler_type=compiler_type,
                            compiler_id="",
                            compiler_name="(probe error)",
                            semver=None,
                            code=CODE_TRANSPORT_ERROR,
                            stderr_text=str(e),
                        ),
                        attempts=[],
                        inconclusive_reason=f"Probe error: {e}",
                    )
                    self.group_done.emit(summary)

            self.finished.emit()
        except CancelledByUser:
            self.aborted.emit()
        except Exception as e:
            LOG.exception("Probe failed")
            self.failed.emit(str(e))

    def _probe_group_binary(self, arch: str, fam: str, compilers: list[CompilerInfo]) -> CeGroupSummary:
        # Compilers are pre-sorted newest->oldest by semver.
        # Goal: find the boundary where compilation flips from OK -> FAIL using
        # binary search to minimize the number of CE calls.
        n = len(compilers)
        attempts_by_idx: dict[int, CeAttempt] = {}
        user_args = std_flags_for_family(fam, self._cpp_std)
        inconclusive_reason: str | None = None

        def test(i: int) -> CeAttempt:
            nonlocal inconclusive_reason
            if self._cancelled():
                raise CancelledByUser()

            hit = attempts_by_idx.get(i)
            if hit is not None:
                return hit

            ci = compilers[i]
            try:
                resp = self._ce.compile_cached(ci.id, self._source, user_args, abort_event=self._abort, timeout_s=45.0)
                code = int(resp.get("code", -1))
                att = CeAttempt(
                    arch=arch,
                    compiler_type=fam,
                    compiler_id=ci.id,
                    compiler_name=ci.name,
                    semver=ci.semver,
                    code=code,
                    stderr_text=stderr_text_from_resp(resp),
                )
            except CancelledByUser:
                raise
            except (TimeoutError, socket.timeout) as e:
                inconclusive_reason = f"Timeout talking to Compiler Explorer (compiler id={ci.id})"
                att = CeAttempt(
                    arch=arch,
                    compiler_type=fam,
                    compiler_id=ci.id,
                    compiler_name=ci.name,
                    semver=ci.semver,
                    code=CODE_TIMEOUT,
                    stderr_text=str(e),
                )
            except Exception as e:
                inconclusive_reason = f"Transport error talking to Compiler Explorer (compiler id={ci.id})"
                att = CeAttempt(
                    arch=arch,
                    compiler_type=fam,
                    compiler_id=ci.id,
                    compiler_name=ci.name,
                    semver=ci.semver,
                    code=CODE_TRANSPORT_ERROR,
                    stderr_text=str(e),
                )

            attempts_by_idx[i] = att
            return att

        if n == 0:
            return CeGroupSummary(
                arch=arch,
                compiler_type=fam,
                highest_supported=None,
                lowest_supported=None,
                first_failure=None,
                attempts=[],
                inconclusive_reason=None,
            )

        newest = test(0)
        if inconclusive_reason is not None:
            tested = [attempts_by_idx[i] for i in sorted(attempts_by_idx.keys())]
            return CeGroupSummary(
                arch=arch,
                compiler_type=fam,
                highest_supported=None,
                lowest_supported=None,
                first_failure=newest,
                attempts=tested,
                inconclusive_reason=inconclusive_reason,
            )

        if not newest.ok():
            return CeGroupSummary(
                arch=arch,
                compiler_type=fam,
                highest_supported=None,
                lowest_supported=None,
                first_failure=newest,
                attempts=[newest],
                inconclusive_reason=None,
            )

        if n == 1:
            return CeGroupSummary(
                arch=arch,
                compiler_type=fam,
                highest_supported=newest,
                lowest_supported=newest,
                first_failure=None,
                attempts=[newest],
                inconclusive_reason=None,
            )

        oldest = test(n - 1)
        if inconclusive_reason is not None:
            tested = [attempts_by_idx[i] for i in sorted(attempts_by_idx.keys())]
            return CeGroupSummary(
                arch=arch,
                compiler_type=fam,
                highest_supported=newest,
                lowest_supported=None,
                first_failure=oldest,
                attempts=tested,
                inconclusive_reason=inconclusive_reason,
            )

        if oldest.ok():
            tested = [attempts_by_idx[i] for i in sorted(attempts_by_idx.keys())]
            return CeGroupSummary(
                arch=arch,
                compiler_type=fam,
                highest_supported=newest,
                lowest_supported=oldest,
                first_failure=None,
                attempts=tested,
                inconclusive_reason=None,
            )

        low = 0
        high = n - 1
        while high - low > 1:
            if self._cancelled():
                raise CancelledByUser()
            mid = (low + high) // 2
            am = test(mid)
            if inconclusive_reason is not None:
                tested = [attempts_by_idx[i] for i in sorted(attempts_by_idx.keys())]
                return CeGroupSummary(
                    arch=arch,
                    compiler_type=fam,
                    highest_supported=newest,
                    lowest_supported=None,
                    first_failure=am,
                    attempts=tested,
                    inconclusive_reason=inconclusive_reason,
                )
            if am.ok():
                low = mid
            else:
                high = mid

        lowest_ok = test(low)
        first_fail = test(high)
        tested = [attempts_by_idx[i] for i in sorted(attempts_by_idx.keys())]

        if inconclusive_reason is not None:
            return CeGroupSummary(
                arch=arch,
                compiler_type=fam,
                highest_supported=newest,
                lowest_supported=None,
                first_failure=first_fail,
                attempts=tested,
                inconclusive_reason=inconclusive_reason,
            )

        return CeGroupSummary(
            arch=arch,
            compiler_type=fam,
            highest_supported=newest,
            lowest_supported=lowest_ok if lowest_ok.ok() else None,
            first_failure=first_fail if not first_fail.ok() else None,
            attempts=tested,
            inconclusive_reason=None,
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
        self.setHeaderLabels(["Target", "Count"])
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        self.setIndentation(18)
        self.setExpandsOnDoubleClick(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.itemChanged.connect(lambda *_: self.selection_changed.emit())

    def set_data(self, families_to_arch_counts: dict[str, dict[str, int]]) -> None:
        self.blockSignals(True)
        self.clear()

        for fam in sorted(families_to_arch_counts.keys(), key=family_sort_key):
            arch_counts = families_to_arch_counts[fam]
            total = sum(int(v) for v in arch_counts.values())

            parent = QTreeWidgetItem([fam, str(total)])
            parent.setFlags(parent.flags() | Qt.ItemFlag.ItemIsAutoTristate | Qt.ItemFlag.ItemIsUserCheckable)
            parent.setCheckState(0, Qt.CheckState.Unchecked)
            self.addTopLevelItem(parent)

            arches = sorted(arch_counts.items(), key=lambda kv: kv[0].casefold())
            for arch, count in arches:
                child = QTreeWidgetItem([arch, str(int(count))])
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Unchecked)
                parent.addChild(child)

        self.blockSignals(False)
        self.collapseAll()

        for i in range(self.topLevelItemCount()):
            it = self.topLevelItem(i)
            if it.text(0) in ("gcc", "clang", "msvc"):
                self.expandItem(it)

        self.resizeColumnToContents(0)
        self.resizeColumnToContents(1)
        self.selection_changed.emit()

    def selected_groups(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for i in range(self.topLevelItemCount()):
            parent = self.topLevelItem(i)
            fam = parent.text(0)
            for j in range(parent.childCount()):
                child = parent.child(j)
                if child.checkState(0) == Qt.CheckState.Checked:
                    out.append((fam, child.text(0)))
        return out

    def set_selected_groups(self, selected: set[str]) -> None:
        self.blockSignals(True)
        for i in range(self.topLevelItemCount()):
            parent = self.topLevelItem(i)
            fam = parent.text(0)
            for j in range(parent.childCount()):
                child = parent.child(j)
                arch = child.text(0)
                key = f"{fam}|{arch}"
                child.setCheckState(0, Qt.CheckState.Checked if key in selected else Qt.CheckState.Unchecked)
        self.blockSignals(False)
        self.selection_changed.emit()

    def set_check_for_visible(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.blockSignals(True)
        for i in range(self.topLevelItemCount()):
            parent = self.topLevelItem(i)
            if parent.isHidden():
                continue
            for j in range(parent.childCount()):
                child = parent.child(j)
                if child.isHidden():
                    continue
                child.setCheckState(0, state)
        self.blockSignals(False)
        self.selection_changed.emit()


class CompilersPanel(QWidget):
    selection_changed = pyqtSignal()
    filter_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(QLabel("Compilers"))

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter family or arch...")
        hl.addWidget(self.filter_edit, 1)

        self.btn_all = QPushButton("Select visible")
        self.btn_none = QPushButton("Clear visible")
        hl.addWidget(self.btn_all)
        hl.addWidget(self.btn_none)

        self.tree = CompilerArchTree()

        f = QFont()
        f.setPointSize(13)
        self.tree.setFont(f)
        self.filter_edit.setFont(f)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addWidget(self.tree, 1)

        self.tree.selection_changed.connect(self.selection_changed.emit)
        self.filter_edit.textChanged.connect(self._apply_filter)
        self.btn_all.clicked.connect(lambda: self.tree.set_check_for_visible(True))
        self.btn_none.clicked.connect(lambda: self.tree.set_check_for_visible(False))

    def _apply_filter(self, text: str) -> None:
        t = (text or "").strip().casefold()
        self.filter_changed.emit(text)

        if not t:
            for i in range(self.tree.topLevelItemCount()):
                parent = self.tree.topLevelItem(i)
                parent.setHidden(False)
                for j in range(parent.childCount()):
                    parent.child(j).setHidden(False)
            return

        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            fam = parent.text(0).casefold()
            fam_match = t in fam
            any_child = False
            for j in range(parent.childCount()):
                child = parent.child(j)
                arch = child.text(0).casefold()
                match = fam_match or (t in arch)
                child.setHidden(not match)
                any_child = any_child or match
            parent.setHidden(not (fam_match or any_child))
            if fam_match:
                for j in range(parent.childCount()):
                    parent.child(j).setHidden(False)
                self.tree.expandItem(parent)

    def selected_groups(self) -> list[tuple[str, str]]:
        return self.tree.selected_groups()


class OptionsPanel(QWidget):
    run_clicked = pyqtSignal()
    abort_clicked = pyqtSignal()
    sample_clicked = pyqtSignal()
    session_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.cpp_std = QComboBox()
        self.cpp_std.addItems(["c++11", "c++14", "c++17", "c++20", "c++23", "c++26"])
        self.cpp_std.setCurrentText("c++17")

        self.btn_probe = QPushButton("Probe")
        self.btn_abort = QPushButton("Abort")
        self.btn_abort.setEnabled(False)
        self.btn_sample = QPushButton("Insert empty main")

        row = QWidget()
        row_l = QHBoxLayout(row)
        row_l.setContentsMargins(0, 0, 0, 0)
        row_l.addWidget(QLabel("C++ standard:"))
        row_l.addWidget(self.cpp_std, 1)
        row_l.addWidget(self.btn_probe)
        row_l.addWidget(self.btn_abort)
        row_l.addWidget(self.btn_sample)

        layout = QVBoxLayout(self)
        layout.addWidget(row)
        layout.addStretch(1)

        self.btn_probe.clicked.connect(self.run_clicked.emit)
        self.btn_abort.clicked.connect(self.abort_clicked.emit)
        self.btn_sample.clicked.connect(self.sample_clicked.emit)
        self.cpp_std.currentIndexChanged.connect(self.session_changed.emit)

    def selected_cpp_std(self) -> str:
        return self.cpp_std.currentText().strip()


class LogPanel(QWidget):
    def __init__(self, mono: QFont):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setAcceptRichText(True)
        self.text.document().setDefaultStyleSheet(default_rich_css(mono))

        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(lambda: self.text.setHtml(""))

        top = QWidget()
        tl = QHBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(QLabel("Log"))
        tl.addStretch(1)
        tl.addWidget(self.btn_clear)

        layout = QVBoxLayout(self)
        layout.addWidget(top)
        layout.addWidget(self.text, 1)

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

        self.list = QListWidget()
        self.list.currentRowChanged.connect(self.result_row_changed.emit)

        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setAcceptRichText(True)
        self.details.document().setDefaultStyleSheet(default_rich_css(mono))

        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.split.addWidget(self.list)
        self.split.addWidget(self.details)
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([360, 900])

        layout = QVBoxLayout(self)
        layout.addWidget(self.split)

    def set_details_ansi(self, s: str) -> None:
        self.details.setHtml(wrap_pre_html(s.rstrip("\n"), "details"))


def render_report_html(standard: str, summaries: list[CeGroupSummary]) -> str:
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
            f" [{overall_lowest.compiler_type} | {overall_lowest.arch}]"
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
    html.append(f"<div><b>Groups tested:</b> {total_groups} (inconclusive: {total_inconclusive})</div>")
    html.append(f"<div><b>Total compilers tested:</b> {total_attempts} (OK: {total_ok}, FAIL: {total_fail})</div>")
    html.append(f"<div><b>Overall lowest passing (group-wise):</b> {_escape_html(overall_line)}</div>")
    html.append("</div>")

    html.append("<h3>Summary</h3>")
    html.append("<table>")
    html.append(
        "<tr><th>Family</th><th>Arch</th><th>Highest passing</th><th>Lowest passing</th>"
        "<th>First failure</th><th>Tested</th><th>Status</th></tr>"
    )

    for fam in sorted(by_fam.keys(), key=family_sort_key):
        groups = sorted(by_fam[fam], key=lambda g: g.arch.casefold())
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
                f"<td>{_escape_html(g.arch)}</td>"
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
            "arch": a.arch,
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
                "arch": s.arch,
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
            arch=str(x.get("arch") or "unknown"),
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
        arch = str(g.get("arch") or "unknown")
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
                arch=arch,
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
    """Main UI/controller: wires widgets to CE client + background workers."""

    def __init__(self, theme_mgr: ThemeManager, ce: CeClient, mono: QFont, settings: AppSettings):
        super().__init__()
        self._theme_mgr = theme_mgr
        self._ce = ce
        self._settings = settings

        self._by_family_arch: dict[str, dict[str, list[CompilerInfo]]] = {}
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

        self.setWindowTitle("CE Multi-Compiler Tester")
        self.resize(1700, 950)

        self.setDockOptions(
            QMainWindow.DockOption.AllowNestedDocks
            | QMainWindow.DockOption.AllowTabbedDocks
            | QMainWindow.DockOption.AnimatedDocks
            | QMainWindow.DockOption.GroupedDragging
        )

        self.setCentralWidget(QWidget())

        self.statusbar = QStatusBar(self)
        self.setStatusBar(self.statusbar)

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
        self.compilers_panel = CompilersPanel()
        self.results_panel = ResultsPanel(mono)
        self.log_panel = LogPanel(mono)

        self.report_browser = QTextBrowser()
        self.report_browser.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.dock_editor = self._mk_dock("Editor", self.editor)
        self.dock_compilers = self._mk_dock("Compilers", self.compilers_panel)
        self.dock_options = self._mk_dock("Options", self.options_panel)
        self.dock_results = self._mk_dock("Results", self.results_panel)
        self.dock_log = self._mk_dock("Log", self.log_panel)
        self.dock_report = self._mk_dock("Report", self.report_browser)

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

        self._setup_menu_and_toolbar()

        self.options_panel.run_clicked.connect(self.probe_lowest_supported)
        self.options_panel.abort_clicked.connect(self.abort_probe)
        self.options_panel.sample_clicked.connect(self.insert_sample)
        self.options_panel.session_changed.connect(self._schedule_save)

        self.compilers_panel.selection_changed.connect(self._schedule_save)
        self.compilers_panel.filter_changed.connect(lambda *_: self._schedule_save())

        self.results_panel.result_row_changed.connect(self.on_result_selected)
        self.results_panel.split.splitterMoved.connect(lambda *_: self._schedule_save())

        self.editor.textChanged.connect(self._schedule_save)

        self._theme_mgr.theme_changed.connect(self._on_theme_changed)
        self._on_theme_changed(self._theme_mgr.current_theme())

        self._apply_saved_session_early()
        self._restore_layout(use_defaults=False)

        self._log("Loading compilers from Compiler Explorer...", clear=True)
        self._start_load_compilers()

    def _setup_menu_and_toolbar(self) -> None:
        menubar = QMenuBar(self)
        self.setMenuBar(menubar)

        file_menu = menubar.addMenu("File")
        export_html = QAction("Export Report (HTML)...", self)
        export_json = QAction("Export Report (JSON)...", self)
        export_html.triggered.connect(self.export_report_html)
        export_json.triggered.connect(self.export_report_json)
        file_menu.addAction(export_html)
        file_menu.addAction(export_json)

        view_menu = menubar.addMenu("View")
        reset_act = QAction("Reset Layout / Session", self)
        reset_act.triggered.connect(self.reset_layout_and_session)
        view_menu.addAction(reset_act)

        tb = QToolBar("Main", self)
        tb.setMovable(True)
        self.addToolBar(tb)

        act_probe = QAction("Probe", self)
        act_probe.triggered.connect(self.probe_lowest_supported)
        act_abort = QAction("Abort", self)
        act_abort.triggered.connect(self.abort_probe)
        act_sample = QAction("Insert empty main", self)
        act_sample.triggered.connect(self.insert_sample)

        tb.addAction(act_probe)
        tb.addAction(act_abort)
        tb.addAction(act_sample)

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
        self._settings.set_value(AppSettings.K_COMPILERS_FILTER, self.compilers_panel.filter_edit.text())
        self._settings.set_value(AppSettings.K_RESULTS_SPLIT, self.results_panel.split.sizes())

        selected = [f"{fam}|{arch}" for fam, arch in self.compilers_panel.selected_groups()]
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
        self.report_browser.setStyleSheet(
            f"QTextBrowser {{ background: {theme.bg.name()}; color: {theme.fg.name()}; }}"
        )
        self.results_panel.details.setStyleSheet(
            f"QTextEdit {{ background: {theme.bg.name()}; color: {theme.fg.name()}; }}"
        )
        self.log_panel.text.setStyleSheet(
            f"QTextEdit {{ background: {theme.bg.name()}; color: {theme.fg.name()}; }}"
        )

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
        by_family_arch: dict[str, dict[str, list[CompilerInfo]]] = {}
        fam_counts: dict[str, int] = {}

        for c in compilers:
            fam = guess_family(c)
            arch = arch_label(c)
            by_family_arch.setdefault(fam, {}).setdefault(arch, []).append(c)
            fam_counts[fam] = fam_counts.get(fam, 0) + 1

        for by_arch in by_family_arch.values():
            for lst in by_arch.values():
                lst.sort(key=lambda x: parse_semver_key(x.semver), reverse=True)

        self._by_family_arch = by_family_arch

        families_to_arch_counts: dict[str, dict[str, int]] = {}
        for fam, by_arch in by_family_arch.items():
            families_to_arch_counts[fam] = {arch: len(lst) for arch, lst in by_arch.items()}

        self.compilers_panel.tree.set_data(families_to_arch_counts)

        saved_sel = self._settings.get_value(AppSettings.K_SELECTED, [])
        if isinstance(saved_sel, list):
            sel_set = {str(x) for x in saved_sel}
            self.compilers_panel.tree.set_selected_groups(sel_set)

        fam_count = len(by_family_arch)
        arch_count = len({a for by_arch in by_family_arch.values() for a in by_arch.keys()})
        top = sorted(fam_counts.items(), key=lambda kv: (family_sort_key(kv[0]), -kv[1]))
        top_txt = ", ".join(f"{k}={v}" for k, v in top[:16])

        self._log(
            f"Loaded {len(compilers)} C++ compilers across {fam_count} families and {arch_count} architectures. {top_txt}",
            clear=True,
        )
        self.statusbar.showMessage("Compilers loaded", 5000)

    def _on_compilers_failed(self, msg: str) -> None:
        self._log(f"Failed to load compilers from Compiler Explorer: {msg}", clear=True)
        self.options_panel.btn_probe.setEnabled(False)
        self.statusbar.showMessage("Failed to load compilers", 5000)

    def insert_sample(self) -> None:
        self.editor.setText(CPP_SAMPLE)

    def _build_jobs(self) -> list[tuple[str, str, list[CompilerInfo]]]:
        jobs: list[tuple[str, str, list[CompilerInfo]]] = []
        groups = self.compilers_panel.selected_groups()
        for fam, arch in groups:
            lst = self._by_family_arch.get(fam, {}).get(arch, [])
            if lst:
                jobs.append((arch, fam, lst))
        jobs.sort(key=lambda t: (family_sort_key(t[1]), t[0].casefold()))
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
            self._log("Select at least one family and arch in the Compilers pane.", clear=False)
            self.statusbar.showMessage("Nothing selected", 4000)
            return

        self._probe_abort = threading.Event()

        source = self.editor.text()
        std = self.options_panel.selected_cpp_std()

        self._current_std_for_report = std
        self._summaries.clear()
        self.results_panel.list.clear()
        self.results_panel.details.clear()
        self.report_browser.clear()

        self.options_panel.btn_probe.setEnabled(False)
        self.options_panel.btn_abort.setEnabled(True)
        self._set_busy(True)
        self.statusbar.showMessage("Probing...", 0)
        self._log(f"Probing {len(jobs)} group(s) using binary search (std={std})...", clear=False)

        self._probe_thread = QThread(self)
        self._probe_worker = CeProbeWorker(self._ce, jobs, source, std, self._probe_abort)
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
        for s in sorted(self._summaries, key=lambda x: (family_sort_key(x.compiler_type), x.arch.casefold())):
            label = f"{s.compiler_type} | {s.arch}"
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
        html = render_report_html(self._current_std_for_report, self._summaries)
        self.report_browser.setHtml(html)

    def _on_group_done(self, summary: CeGroupSummary) -> None:
        self._summaries.append(summary)
        self._summaries.sort(key=lambda x: (family_sort_key(x.compiler_type), x.arch.casefold()))
        self._rebuild_results_list()
        self._refresh_report_view()

        label = f"{summary.compiler_type} | {summary.arch}"
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
        parts: list[str] = []
        parts.append(f"Family: {s.compiler_type}")
        parts.append(f"Arch: {s.arch}")
        parts.append(f"Standard: {self._current_std_for_report}")
        parts.append(f"Args used: {std_flags_for_family(s.compiler_type, self._current_std_for_report)}")

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
        html = render_report_html(self._current_std_for_report, self._summaries)
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
            self.compilers_panel.tree.set_selected_groups(set())

        self._log("Reset.", clear=True)
        self.statusbar.showMessage("Reset", 5000)


def main():
    setup_logging()
    app = QApplication(sys.argv)
    mono = choose_mono_font(12)

    settings = AppSettings()
    theme_mgr = ThemeManager(Path(__file__).with_name("theme.json"))
    ce = CeClient(base_url="https://godbolt.org", min_request_interval_s=0.12)

    w = MainWindow(theme_mgr, ce, mono, settings)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
