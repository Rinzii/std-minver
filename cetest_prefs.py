"""Preferences and persistent settings."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import QSettings

from cetest_flags import ExtraFlagsConfig, _flag_style_for_family, _normalize_user_flags_text, std_flags_for_family
from cetest_models import normalize_family_token


@dataclass(frozen=True)
class LibraryRule:



    scope: str
    target: str
    lib_id: str
    version: str

    def key(self) -> tuple[str, str, str, str]:

        return (self.scope, self.target, self.lib_id, self.version)


def _normalize_ce_library_id(s: str) -> str:

    return str(s or "").strip()


def _normalize_ce_library_version(s: str) -> str:

    return str(s or "").strip()


def _clamp_int(value: object, default: int, lo: int, hi: int) -> int:

    try:
        v = int(value)
    except Exception:
        v = int(default)
    return max(int(lo), min(int(hi), v))


def _load_library_rules(settings: "AppSettings") -> list[LibraryRule]:

    raw = settings.get_value(AppSettings.K_LIBS_RULES_JSON, "[]")
    try:
        data = raw if isinstance(raw, list) else json.loads(str(raw or "[]"))
    except Exception:
        data = []
    if not isinstance(data, list):
        return []

    out: list[LibraryRule] = []
    for it in data:
        if not isinstance(it, dict):
            continue
        scope = str(it.get("scope") or "").strip().lower()
        if scope not in ("all", "family", "compiler"):
            continue
        target = str(it.get("target") or "").strip()
        lib_id = _normalize_ce_library_id(str(it.get("id") or it.get("lib_id") or ""))
        version = _normalize_ce_library_version(str(it.get("version") or ""))
        if not lib_id or not version:
            continue
        if scope == "all":
            target = ""
        if scope in ("family", "compiler") and not target:
            continue
        out.append(LibraryRule(scope=scope, target=target, lib_id=lib_id, version=version))


    seen: set[tuple[str, str, str, str]] = set()
    clean: list[LibraryRule] = []
    for r in out:
        k = r.key()
        if k in seen:
            continue
        seen.add(k)
        clean.append(r)
    return clean


def _save_library_rules(settings: "AppSettings", rules: list[LibraryRule]) -> None:

    data = [
        {"scope": r.scope, "target": r.target, "id": r.lib_id, "version": r.version}
        for r in (rules or [])
        if r.lib_id and r.version and r.scope in ("all", "family", "compiler")
    ]
    settings.set_value(AppSettings.K_LIBS_RULES_JSON, json.dumps(data, ensure_ascii=False, sort_keys=False))


def _effective_libraries_for_compiler(rules: list[LibraryRule], family: str, compiler_id: str) -> list[dict[str, str]]:


    fam = str(family or "").strip()
    cid = str(compiler_id or "").strip()

    chosen_by_id: dict[str, str] = {}
    order: list[str] = []

    def apply_rule(r: LibraryRule) -> None:

        lid = _normalize_ce_library_id(r.lib_id)
        ver = _normalize_ce_library_version(r.version)
        if not lid or not ver:
            return
        if lid not in chosen_by_id:
            order.append(lid)
        chosen_by_id[lid] = ver

    for r in rules or []:
        if r.scope == "all":
            apply_rule(r)
        elif r.scope == "family" and r.target == fam:
            apply_rule(r)
        elif r.scope == "compiler" and r.target == cid:
            apply_rule(r)

    return [{"id": lid, "version": chosen_by_id[lid]} for lid in order if lid in chosen_by_id]


@dataclass(frozen=True)
class PreferencesState:




    theme_mode: str
    theme_path: str
    ui_font_pt: int
    editor_font_pt: int


    compile_commands_path: str
    extra_include_dirs_text: str
    inline_once: bool
    auto_copy: bool
    strip_pragma_once: bool
    emit_line_directives: bool
    include_debug_comments: bool


    extra_flags_gnu: str
    extra_flags_msvc: str


    library_rules: list[LibraryRule]


def _load_preferences_state(settings: "AppSettings", *, default_ui_pt: int = 13, default_editor_pt: int = 12) -> PreferencesState:

    mode = settings.get_value(AppSettings.K_THEME_MODE, "auto")
    theme_path = settings.get_value(AppSettings.K_THEME_PATH, "")
    ui_pt = settings.get_value(AppSettings.K_UI_FONT_PT, default_ui_pt)
    ed_pt = settings.get_value(AppSettings.K_EDITOR_FONT_PT, default_editor_pt)

    cc = settings.get_value(AppSettings.K_PP_COMPILE_COMMANDS, "")
    extra = settings.get_value(AppSettings.K_PP_EXTRA_INCLUDE_DIRS, "")
    inline_once = settings.get_value(AppSettings.K_PP_INLINE_ONCE, True)
    auto_copy = settings.get_value(AppSettings.K_PP_AUTO_COPY, True)
    strip_po = settings.get_value(AppSettings.K_PP_STRIP_PRAGMA_ONCE, True)
    emit_line = settings.get_value(AppSettings.K_PP_EMIT_LINE_DIRECTIVES, True)
    dbg = settings.get_value(AppSettings.K_PP_DEBUG_COMMENTS, False)

    extra_gnu = settings.get_value(AppSettings.K_FLAGS_EXTRA_GNU, "")
    extra_msvc = settings.get_value(AppSettings.K_FLAGS_EXTRA_MSVC, "")

    rules = _load_library_rules(settings)

    return PreferencesState(
        theme_mode=str(mode or "auto"),
        theme_path=str(theme_path or ""),
        ui_font_pt=_clamp_int(ui_pt, default_ui_pt, 8, 28),
        editor_font_pt=_clamp_int(ed_pt, default_editor_pt, 8, 28),
        compile_commands_path=str(cc or "").strip(),
        extra_include_dirs_text=str(extra or ""),
        inline_once=bool(inline_once),
        auto_copy=bool(auto_copy),
        strip_pragma_once=bool(strip_po),
        emit_line_directives=bool(emit_line),
        include_debug_comments=bool(dbg),
        extra_flags_gnu=_normalize_user_flags_text(str(extra_gnu or "")),
        extra_flags_msvc=_normalize_user_flags_text(str(extra_msvc or "")),
        library_rules=rules,
    )


def _save_preferences_state(settings: "AppSettings", p: PreferencesState) -> None:

    settings.set_value(AppSettings.K_THEME_MODE, str(p.theme_mode or "auto"))
    settings.set_value(AppSettings.K_THEME_PATH, str(p.theme_path or "").strip())
    settings.set_value(AppSettings.K_UI_FONT_PT, int(p.ui_font_pt))
    settings.set_value(AppSettings.K_EDITOR_FONT_PT, int(p.editor_font_pt))

    settings.set_value(AppSettings.K_PP_COMPILE_COMMANDS, str(p.compile_commands_path or "").strip())
    settings.set_value(AppSettings.K_PP_EXTRA_INCLUDE_DIRS, str(p.extra_include_dirs_text or ""))
    settings.set_value(AppSettings.K_PP_INLINE_ONCE, bool(p.inline_once))
    settings.set_value(AppSettings.K_PP_AUTO_COPY, bool(p.auto_copy))
    settings.set_value(AppSettings.K_PP_STRIP_PRAGMA_ONCE, bool(p.strip_pragma_once))
    settings.set_value(AppSettings.K_PP_EMIT_LINE_DIRECTIVES, bool(p.emit_line_directives))
    settings.set_value(AppSettings.K_PP_DEBUG_COMMENTS, bool(p.include_debug_comments))

    settings.set_value(AppSettings.K_FLAGS_EXTRA_GNU, _normalize_user_flags_text(str(p.extra_flags_gnu or "")))
    settings.set_value(AppSettings.K_FLAGS_EXTRA_MSVC, _normalize_user_flags_text(str(p.extra_flags_msvc or "")))

    _save_library_rules(settings, list(p.library_rules or []))


def _apply_preferences_to_options_panel(panel: "OptionsPanel", p: PreferencesState) -> None:


    if hasattr(panel, "set_compile_commands_path"):
        panel.set_compile_commands_path(str(p.compile_commands_path or ""))
    if hasattr(panel, "set_extra_include_dirs_text"):
        panel.set_extra_include_dirs_text(str(p.extra_include_dirs_text or ""))
    if hasattr(panel, "set_inline_once"):
        panel.set_inline_once(bool(p.inline_once))
    if hasattr(panel, "set_auto_copy"):
        panel.set_auto_copy(bool(p.auto_copy))
    if hasattr(panel, "set_strip_pragma_once"):
        panel.set_strip_pragma_once(bool(p.strip_pragma_once))
    if hasattr(panel, "set_emit_line_directives"):
        panel.set_emit_line_directives(bool(p.emit_line_directives))
    if hasattr(panel, "set_include_debug_comments"):
        panel.set_include_debug_comments(bool(p.include_debug_comments))


def _load_extra_flags_config(settings: "AppSettings") -> ExtraFlagsConfig:


    p = _load_preferences_state(settings)
    gnu = _normalize_user_flags_text(str(p.extra_flags_gnu or ""))
    msvc = _normalize_user_flags_text(str(p.extra_flags_msvc or ""))

    raw = settings.get_value(AppSettings.K_FLAGS_EXTRA_BY_GROUP_JSON, "{}")
    by_group: dict[str, str] = {}
    try:
        if isinstance(raw, dict):
            data = raw
        else:
            data = json.loads(str(raw or "{}"))
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(k, str):
                    by_group[k] = _normalize_user_flags_text(str(v or ""))
    except Exception:

        by_group = {}


    by_group = {k: v for k, v in by_group.items() if v}
    return ExtraFlagsConfig(extra_gnu=gnu, extra_msvc=msvc, extra_by_group=by_group)


def _save_extra_flags_by_group(settings: "AppSettings", by_group: dict[str, str]) -> None:


    clean = {str(k): _normalize_user_flags_text(str(v or "")) for k, v in (by_group or {}).items()}
    clean = {k: v for k, v in clean.items() if v}
    settings.set_value(AppSettings.K_FLAGS_EXTRA_BY_GROUP_JSON, json.dumps(clean, ensure_ascii=False, sort_keys=True))


def build_user_args_for_group(fam: str, platform: str, series: str, cpp_std: str, extra: ExtraFlagsConfig) -> str:

    """Build the CE userArguments string for a (family, platform, series) group."""
    base = std_flags_for_family(fam, cpp_std)
    style = _flag_style_for_family(fam)
    global_extra = extra.extra_msvc if style == "msvc" else extra.extra_gnu
    group_extra = extra.extra_for_group(fam, platform, series)
    parts = [base, global_extra, group_extra]
    return _normalize_user_flags_text(" ".join(p for p in parts if p))


def _canonicalize_saved_family(fam: str) -> str:

    f = (fam or "").strip()
    if f == "icx":
        return "intel-icx"
    if f == "icc":
        return "intel-icc"
    return f


def _migrate_selected_group_keys(
    selected: set[str],
    families_to_platform_to_series_counts: dict[str, dict[str, dict[str, int]]],
) -> set[str]:


    out: set[str] = set()

    def add_all_under(fam: str, platform: str | None = None) -> None:

        by_platform = families_to_platform_to_series_counts.get(fam, {})
        if platform is None:
            for p, by_series in by_platform.items():
                for s in by_series.keys():
                    out.add(f"{fam}|{p}|{s}")
            return
        by_series = by_platform.get(platform, {})
        for s in by_series.keys():
            out.add(f"{fam}|{platform}|{s}")

    for key in selected:
        parts = [p.strip() for p in str(key).split("|")]
        parts = [p for p in parts if p]
        if not parts:
            continue

        if len(parts) == 1:
            fam = _canonicalize_saved_family(parts[0])
            add_all_under(fam)
            continue

        if len(parts) == 2:
            fam = _canonicalize_saved_family(parts[0])
            platform = parts[1]
            add_all_under(fam, platform=platform)
            continue

        fam = _canonicalize_saved_family(parts[0])
        platform = parts[1]
        series = "|".join(parts[2:])
        if (
            fam in families_to_platform_to_series_counts
            and platform in families_to_platform_to_series_counts[fam]
            and series in families_to_platform_to_series_counts[fam][platform]
        ):
            out.add(f"{fam}|{platform}|{series}")
        else:

            add_all_under(fam, platform=platform)

    return out


class RateLimiter:



    def __init__(self, min_interval_s: float):

        self._min = float(min_interval_s)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self, abort_event: threading.Event | None = None) -> None:


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



    ORG = "IanTools"
    APP = "CETest"

    K_GEOMETRY = "main/geometry"
    K_STATE = "main/windowState"

    K_EDITOR = "session/editorText"
    K_CPPSTD = "session/cppStd"
    K_SELECTED = "session/selectedGroups"
    K_COMPILERS_FILTER = "session/compilersFilter"
    K_RESULTS_SPLIT = "session/resultsSplitterSizes"

    K_LAST_OPEN_PATH = "session/lastOpenPath"

    K_LAST_RESULT_ROW = "session/lastResultRow"
    K_LAST_REPORT_JSON = "session/lastReportJson"
    K_LAST_REPORT_HTML = "session/lastReportHtml"


    K_THEME_MODE = "appearance/themeMode"
    K_THEME_PATH = "appearance/themePath"
    K_UI_FONT_PT = "appearance/uiFontPt"
    K_EDITOR_FONT_PT = "appearance/editorFontPt"


    K_PP_COMPILE_COMMANDS = "preprocessor/compileCommandsPath"
    K_PP_EXTRA_INCLUDE_DIRS = "preprocessor/extraIncludeDirs"
    K_PP_INLINE_ONCE = "preprocessor/inlineOnce"
    K_PP_AUTO_COPY = "preprocessor/autoCopyClipboard"
    K_PP_STRIP_PRAGMA_ONCE = "preprocessor/stripPragmaOnce"
    K_PP_EMIT_LINE_DIRECTIVES = "preprocessor/emitLineDirectives"
    K_PP_DEBUG_COMMENTS = "preprocessor/includeDebugComments"




    K_FLAGS_EXTRA_GNU = "compilerFlags/extraGnu"
    K_FLAGS_EXTRA_MSVC = "compilerFlags/extraMsvc"
    K_FLAGS_EXTRA_BY_GROUP_JSON = "compilerFlags/extraByGroupJson"






    K_LIBS_RULES_JSON = "compilerLibraries/rulesJson"

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
            self.K_LAST_OPEN_PATH,
            self.K_LAST_RESULT_ROW,
            self.K_LAST_REPORT_JSON,
            self.K_LAST_REPORT_HTML,
            self.K_THEME_MODE,
            self.K_THEME_PATH,
            self.K_UI_FONT_PT,
            self.K_EDITOR_FONT_PT,
            self.K_PP_COMPILE_COMMANDS,
            self.K_PP_EXTRA_INCLUDE_DIRS,
            self.K_PP_INLINE_ONCE,
            self.K_PP_AUTO_COPY,
            self.K_PP_STRIP_PRAGMA_ONCE,
            self.K_PP_EMIT_LINE_DIRECTIVES,
            self.K_PP_DEBUG_COMMENTS,
            self.K_FLAGS_EXTRA_GNU,
            self.K_FLAGS_EXTRA_MSVC,
            self.K_FLAGS_EXTRA_BY_GROUP_JSON,
            self.K_LIBS_RULES_JSON,
        )
        for k in keys:
            self._s.remove(k)


