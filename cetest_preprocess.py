"""Include flattening and compile_commands parsing."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

from cetest_core import decode_text_file_for_editor


def _norm_abs(p: Path) -> str:

    try:
        return str(p.expanduser().resolve())
    except Exception:
        return str(p.expanduser().absolute())


def _split_lines_paths(txt: str) -> list[Path]:

    out: list[Path] = []
    for raw in (txt or "").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        out.append(Path(s).expanduser())
    return out


def _iter_include_dirs_from_args(args: list[str], directory: Path) -> tuple[list[Path], list[Path], list[Path]]:

    # Parse -I/-iquote/-isystem include paths from a compile_commands argv list.

    quote_dirs: list[Path] = []
    user_dirs: list[Path] = []
    sys_dirs: list[Path] = []

    i = 0
    while i < len(args):
        a = args[i]
        v: str | None = None
        kind: str | None = None

        if a == "-I":
            kind = "I"
            i += 1
            v = args[i] if i < len(args) else None
        elif a.startswith("-I") and len(a) > 2:
            kind = "I"
            v = a[2:]
        elif a == "-iquote":
            kind = "iquote"
            i += 1
            v = args[i] if i < len(args) else None
        elif a.startswith("-iquote") and len(a) > len("-iquote"):
            kind = "iquote"
            v = a[len("-iquote") :]
        elif a == "-isystem":
            kind = "isystem"
            i += 1
            v = args[i] if i < len(args) else None
        elif a.startswith("-isystem") and len(a) > len("-isystem"):
            kind = "isystem"
            v = a[len("-isystem") :]

        if kind is not None and v is not None:
            p = Path(v.strip().strip('"')).expanduser()
            if not p.is_absolute():
                p = directory / p
            try:
                p = p.resolve()
            except Exception:
                p = p.absolute()

            if kind == "iquote":
                quote_dirs.append(p)
            elif kind == "I":
                user_dirs.append(p)
            else:
                sys_dirs.append(p)

        i += 1

    return quote_dirs, user_dirs, sys_dirs


def _load_compile_commands_include_dirs(cc_path: Path, selected_file: Path) -> tuple[list[Path], list[Path], list[Path], dict[str, Any]]:

    """Extract include dirs from compile_commands.json for (or near) a selected file."""
    # Headers often aren't listed in compile_commands.json; pick the closest TU by path prefix.

    try:
        raw = cc_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as e:
        raise ValueError(f"Failed to read/parse compile_commands.json: {e}") from e

    if not isinstance(data, list):
        raise ValueError("compile_commands.json must be a JSON array.")

    sel_abs = _norm_abs(selected_file)
    sel_cf = sel_abs.casefold()
    sel_p = Path(sel_abs)
    sel_parent = sel_p.parent

    best: dict[str, Any] | None = None
    best_fp_abs: str = ""
    best_is_exact = False
    best_score = -1

    def _common_prefix_len(a: Path, b: Path) -> int:

        ap = a.parts
        bp = b.parts
        n = 0
        for x, y in zip(ap, bp):
            if x.casefold() != y.casefold():
                break
            n += 1
        return n

    candidates: list[tuple[dict[str, Any], str, Path]] = []
    for ent in data:
        if not isinstance(ent, dict):
            continue
        f = ent.get("file")
        d = ent.get("directory")
        if not isinstance(f, str):
            continue
        dir_p = Path(d).expanduser() if isinstance(d, str) and d.strip() else selected_file.parent
        fp = dir_p / Path(f).expanduser() if not Path(f).is_absolute() else Path(f).expanduser()
        fp_abs = _norm_abs(fp)
        fp_p = Path(fp_abs)
        candidates.append((ent, fp_abs, fp_p))
        if fp_abs.casefold() == sel_cf:
            # Exact match: use the compile entry for this file verbatim.
            best = ent
            best_fp_abs = fp_abs
            best_is_exact = True
            best_score = 10_000_000
            break

    if best is None:

        # Fallback: choose the entry in the closest directory subtree to the selected file.

        for ent, fp_abs, fp_p in candidates:
            score = _common_prefix_len(sel_parent, fp_p.parent)
            if score > best_score:
                best = ent
                best_fp_abs = fp_abs
                best_is_exact = False
                best_score = score

        if best is None or best_score <= 0:
            return [], [], [], {"matched": False, "reason": "no suitable entry found", "selected": sel_abs}

    directory = Path(str(best.get("directory") or "")).expanduser()
    if not str(directory).strip():
        directory = selected_file.parent

    args: list[str] = []
    if isinstance(best.get("arguments"), list) and all(isinstance(x, str) for x in best["arguments"]):
        args = list(best["arguments"])
    elif isinstance(best.get("command"), str):
        try:
            args = shlex.split(best["command"], posix=True)
        except Exception:

            args = [x for x in best["command"].split(" ") if x]

    def _is_source_like(token: str) -> bool:

        bn = Path(token).name
        ext = Path(bn).suffix.lower()
        return ext in {
            ".c",
            ".cc",
            ".cpp",
            ".cxx",
            ".m",
            ".mm",
            ".ixx",
            ".cppm",
            ".h",
            ".hh",
            ".hpp",
            ".hxx",
        }

    def _looks_like_tool(token: str) -> bool:

        if not token or token.startswith("-") or token.startswith("@"):
            return False
        bn = Path(token).name
        if _is_source_like(bn):
            return False

        known = {
            "cc",
            "c++",
            "gcc",
            "g++",
            "clang",
            "clang++",
            "clang-cl",
            "cl",
            "ccache",
            "sccache",
            "distcc",
        }
        if bn in known:
            return True
        if bn.startswith(("clang", "gcc", "g++")):
            return True
        return False


    # compile_commands often prefixes the real compiler with wrappers (ccache/sccache/etc).
    while args and _looks_like_tool(args[0]):
        args = args[1:]

    q, u, s = _iter_include_dirs_from_args(args, directory)
    meta = {
        "matched": True,
        "selected": sel_abs,
        "matched_entry_file": best_fp_abs,
        "matched_entry_directory": _norm_abs(directory),
        "exact": bool(best_is_exact),
        "score": int(best_score),
        "source": "arguments" if isinstance(best.get("arguments"), list) else ("command" if isinstance(best.get("command"), str) else "unknown"),
    }
    return q, u, s, meta


def find_companion_source_from_compile_commands(
    cc_path: Path,
    header_file: Path,
    *,
    allowed_exts: tuple[str, ...] = (".cpp", ".cc", ".cxx", ".c", ".mm"),
) -> tuple[Path | None, dict[str, Any]]:

    """Try to locate a companion source file for a header using compile_commands.json.

    Strategy:
    - Consider only compile entries whose file stem matches the header stem (case-insensitive),
      and whose extension is in allowed_exts.
    - Rank candidates by path proximity to the header directory (longest common path prefix).
    - Ignore entries that point to non-existent files (stale compile DB).
    """

    cc_path = cc_path.expanduser()
    header_file = header_file.expanduser()
    try:
        header_file = header_file.resolve()
    except Exception:
        pass

    try:
        raw = cc_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as e:
        raise ValueError(f"Failed to read/parse compile_commands.json: {e}") from e

    if not isinstance(data, list):
        raise ValueError("compile_commands.json must be a JSON array.")

    stem_cf = header_file.stem.casefold()
    header_dir = header_file.parent
    allowed_cf = {e.casefold() for e in allowed_exts}

    def _common_prefix_len(a: Path, b: Path) -> int:
        ap = a.parts
        bp = b.parts
        n = 0
        for x, y in zip(ap, bp):
            if str(x).casefold() != str(y).casefold():
                break
            n += 1
        return n

    best: Path | None = None
    best_score = -1
    cand_count = 0

    for ent in data:
        if not isinstance(ent, dict):
            continue
        f = ent.get("file")
        d = ent.get("directory")
        if not isinstance(f, str) or not f.strip():
            continue
        dir_p = Path(d).expanduser() if isinstance(d, str) and d.strip() else header_dir
        fp = dir_p / Path(f).expanduser() if not Path(f).is_absolute() else Path(f).expanduser()
        try:
            fp = fp.resolve()
        except Exception:
            fp = fp.absolute()

        if fp.suffix.casefold() not in allowed_cf:
            continue
        if fp.stem.casefold() != stem_cf:
            continue
        if not (fp.exists() and fp.is_file()):
            continue

        cand_count += 1
        score = _common_prefix_len(header_dir, fp.parent)
        if score > best_score:
            best_score = score
            best = fp

    meta: dict[str, Any]
    if best is None:
        meta = {
            "matched": False,
            "reason": "no matching translation unit found",
            "selected_header": _norm_abs(header_file),
            "allowed_exts": list(allowed_exts),
            "candidates_considered": int(cand_count),
        }
    else:
        meta = {
            "matched": True,
            "selected_header": _norm_abs(header_file),
            "matched_companion": _norm_abs(best),
            "score": int(best_score),
            "allowed_exts": list(allowed_exts),
            "candidates_considered": int(cand_count),
        }
    return best, meta


_RE_INCLUDE_QUOTED = re.compile(r'^\s*#\s*include\s*"([^"]+)"')


def flatten_user_includes(
    root_file: Path,
    extra_include_dirs: list[Path],
    compile_commands_path: Path | None,
    inline_once: bool,
    strip_pragma_once: bool,
    emit_line_directives: bool,
    include_debug_comments: bool,
    *,
    compile_commands_selected_file: Path | None = None,
) -> tuple[str, dict[str, Any]]:

    """Inline quoted includes into a single CE-friendly translation unit (plus stats)."""
    # Inline only quoted includes; system headers can explode into megabytes.

    root_file = root_file.expanduser()
    if not root_file.exists() or not root_file.is_file():
        raise ValueError(f"File not found: {root_file}")

    quote_dirs: list[Path] = []
    user_dirs: list[Path] = []
    sys_dirs: list[Path] = []

    cc_meta: dict[str, Any] = {"matched": False}
    if compile_commands_path is not None:
        cc = compile_commands_path.expanduser()
        selected_for_cc = compile_commands_selected_file.expanduser() if compile_commands_selected_file is not None else root_file
        if cc.exists() and cc.is_file():
            q, u, s, cc_meta2 = _load_compile_commands_include_dirs(cc, selected_for_cc)
            quote_dirs.extend(q)
            user_dirs.extend(u)
            sys_dirs.extend(s)
            cc_meta = cc_meta2
        elif str(cc).strip():
            raise ValueError(f"compile_commands.json not found: {cc}")


    norm_extra: list[Path] = []
    for d in extra_include_dirs:
        try:
            p = d.expanduser()
            if not p.is_absolute():

                p = root_file.parent / p
            p = p.resolve()
        except Exception:
            p = (root_file.parent / d).absolute() if not d.is_absolute() else d.absolute()
        if p.exists() and p.is_dir():
            norm_extra.append(p)


    global_search: list[Path] = []
    for p in quote_dirs + user_dirs + norm_extra:
        if p not in global_search:
            global_search.append(p)
    # sys_dirs are tracked for reporting/debugging, but we intentionally don't inline them.

    inlined: set[str] = set()
    stack: list[str] = []

    stats: dict[str, Any] = {
        "files_inlined": 0,
        "include_lines_seen": 0,
        "include_lines_inlined": 0,
        "include_lines_unresolved": 0,
        "root": str(root_file),
        "compile_commands_used": str(compile_commands_path) if compile_commands_path is not None else "",
        "compile_commands_selected_file": str(compile_commands_selected_file) if compile_commands_selected_file is not None else str(root_file),
        "compile_commands_match": cc_meta,
        "quote_dirs": [str(p) for p in quote_dirs],
        "user_include_dirs": [str(p) for p in user_dirs],
        "system_include_dirs_ignored": [str(p) for p in sys_dirs],
        "extra_include_dirs": [str(p) for p in norm_extra],
        "strip_pragma_once": bool(strip_pragma_once),
        "emit_line_directives": bool(emit_line_directives),
        "include_debug_comments": bool(include_debug_comments),
    }

    def _escape_for_line(p: Path) -> str:


        return str(p).replace("\\", "\\\\").replace('"', '\\"')

    def _resolve_include(name: str, including_file: Path) -> Path | None:


        cand = (including_file.parent / name).expanduser()
        if cand.exists() and cand.is_file():
            return cand
        # Search order for quoted includes: local dir first, then -iquote/-I/extra dirs.
        for d in global_search:
            cand2 = (d / name).expanduser()
            if cand2.exists() and cand2.is_file():
                return cand2
        return None

    def _read_text(p: Path) -> str:

        data = p.read_bytes()
        txt, _enc = decode_text_file_for_editor(data)
        return txt

    def _flatten_file(p: Path) -> str:

        p = p.expanduser()
        p_abs = _norm_abs(p)

        if p_abs in stack:

            if include_debug_comments:
                return f'/* [cetest] include cycle detected: "{_escape_for_line(p)}" */\n'
            return ""

        if inline_once and p_abs in inlined:
            if include_debug_comments:
                return f'/* [cetest] skipped duplicate include: "{_escape_for_line(p)}" */\n'
            return ""

        stack.append(p_abs)
        inlined.add(p_abs)
        stats["files_inlined"] += 1

        src = _read_text(p)
        out: list[str] = []
        if include_debug_comments:
            out.append(f'/* [cetest] begin inlined file: {_escape_for_line(p)} */\n')
        if emit_line_directives:
            # Keep file/line locations sane in CE diagnostics.
            out.append(f'#line 1 "{_escape_for_line(p)}"\n')

        line_no = 0
        for line in src.splitlines(True):
            line_no += 1
            if strip_pragma_once and re.match(r"^\s*#\s*pragma\s+once\b", line):
                continue
            m = _RE_INCLUDE_QUOTED.match(line)
            if not m:
                out.append(line)
                continue

            stats["include_lines_seen"] += 1
            inc_name = m.group(1)
            resolved = _resolve_include(inc_name, p)
            if resolved is None:
                stats["include_lines_unresolved"] += 1
                out.append(line)
                continue

            stats["include_lines_inlined"] += 1
            if include_debug_comments:
                out.append(f'/* [cetest] inlined: "{inc_name}" -> "{_escape_for_line(resolved)}" */\n')
            out.append(_flatten_file(resolved))
            if emit_line_directives:
                out.append(f'#line {line_no + 1} "{_escape_for_line(p)}"\n')

        if include_debug_comments:
            out.append(f'/* [cetest] end inlined file: {_escape_for_line(p)} */\n')
        stack.pop()
        return "".join(out)

    text = _flatten_file(root_file)
    return text, stats


