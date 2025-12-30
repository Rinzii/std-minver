"""Data models and classification helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CompilerInfo:

    id: str
    name: str
    lang: str
    compiler_type: str
    semver: str | None
    instruction_set: str | None


@dataclass(frozen=True)
class CeLibraryInfo:

    id: str
    name: str
    versions: list[str]


def parse_semver_key(s: str | None) -> tuple[int, int, int, int, str]:



    """Sort key for CE semver strings (handles trunk/nightly-ish values)."""
    # CE "semver" isn't strict; treat things like "trunk"/"nightly" as very new.

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


def normalize_platform(instruction_set: str | None) -> str:


    if not instruction_set:
        return "unknown"
    s = instruction_set.strip()
    if not s:
        return "unknown"
    sl = s.casefold()
    if sl in ("amd64", "x86_64", "x86-64", "x64"):
        return "x86-64"
    if sl in ("arm64",):
        return "aarch64"
    return s


def parse_platform_from_name(name: str | None) -> str | None:

    if not name:
        return None
    s = name.lower()

    if re.search(r"^\s*arm\s+gcc\b", s):
        return "arm"
    if re.search(r"^\s*arm64\s+msvc\b", s):
        return "aarch64"
    if re.search(r"^\s*arm\s+msvc\b", s):
        return "arm"


    patterns = [
        ("armv7-a", r"\barmv7[-\s]?a\b|\barmv7\b"),
        ("aarch64", r"\baarch64\b|\barm64\b"),
        ("x86-64", r"\bx86[-_]?64\b|\bamd64\b|\bx64\b"),
        ("x86", r"\bx86\b|\bi686\b"),
        ("riscv64", r"\briscv64\b"),
        ("ppc64le", r"\bppc64le\b"),
        ("s390x", r"\bs390x\b"),
        ("wasm32", r"\bwasm32\b"),
        ("wasm64", r"\bwasm64\b"),
        ("msp430", r"\bmsp430\b"),
        ("avr", r"\bavr\b"),
        ("6502", r"\b6502\b"),
        ("mips64", r"\bmips64\b"),
        ("mips", r"\bmips\b"),
        ("hexagon", r"\bhexagon\b"),
        ("qnx", r"\bqnx\b"),
    ]
    for canon, rx in patterns:
        if re.search(rx, s):
            return canon
    return None


def parse_platform_from_id(cid: str | None) -> str | None:

    if not cid:
        return None
    s = cid.strip().lower()
    if not s:
        return None
    if s.startswith("arm"):
        return "arm"
    if s.startswith("msp430"):
        return "msp430"
    if s.startswith("avr"):
        return "avr"
    if s.startswith("gcc6502") or "6502" in s:
        return "6502"
    if s.startswith("hexagon"):
        return "hexagon"
    if s.startswith("qnx"):
        return "qnx"
    return None


def platform_label(c: CompilerInfo) -> str:

    a = normalize_platform(c.instruction_set)
    if a != "unknown":
        return a
    p = parse_platform_from_name(c.name)
    if p is not None:
        return p
    pid = parse_platform_from_id(c.id)
    return pid if pid is not None else "unknown"


def series_label(c: CompilerInfo) -> str:

    # Grouping label: strip version-ish suffixes so related compilers bucket together.

    name = (c.name or "").strip()
    if not name:
        return "(unknown series)"

    sem = (c.semver or "").strip()
    if sem and sem in name:

        name2 = name[::-1].replace(sem[::-1], "", 1)[::-1].strip()
        if name2:
            name = name2


    name = re.sub(r"\s*\((?:trunk|head|snapshot|nightly|git)\)\s*$", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r"\s+(?:v)?\d+(?:\.\d+){0,3}([^\w].*)?$", "", name, flags=re.IGNORECASE).strip()
    return name if name else "(unknown series)"


def normalize_family_token(token: str) -> str:

    ct = (token or "").strip().lower()
    if not ct:
        return "unknown"

    if "nvc++" in ct or "nvhpc" in ct:
        return "nvc++"



    if ct in ("clang-intel",):
        return "intel-icx"
    if ct in ("clang-cl",):
        return "clang-cl"
    if ct in ("win32-mingw-gcc",):
        return "mingw-gcc"
    if ct in ("win32-mingw-clang",):
        return "mingw-clang"
    if ct in ("win32-vc",):
        return "msvc"

    if "icpx" in ct:
        return "intel-icx"
    if "icx" in ct:
        return "intel-icx"
    if "icc" in ct:
        return "intel-icc"

    if "gcc" in ct or "g++" in ct or ct in ("gpp", "gxx", "gnu"):
        return "gcc"
    if "clang" in ct:
        return "clang"

    if "win32" in ct and "mingw" in ct and "gcc" in ct:
        return "mingw-gcc"
    if "win32" in ct and "mingw" in ct and "clang" in ct:
        return "mingw-clang"
    if "msvc" in ct or "visual" in ct or ct in ("cl", "vc"):
        return "msvc"

    if "icx" in ct or "icc" in ct or "intel" in ct:
        return "intel"

    return ct


def guess_family(c: CompilerInfo) -> str:


    # compilerType is a good hint, but not reliable across all CE instances.

    fam = normalize_family_token(c.compiler_type)
    hay = f"{c.name} {c.id} {c.compiler_type}".lower()

    if "nvc++" in hay or "nvhpc" in hay:
        return "nvc++"



    if fam in ("clang-cl", "mingw-gcc", "mingw-clang", "msvc") and fam != "unknown":
        return fam


    if re.search(r"\bicpx\b", hay) or "oneapi" in hay or "dpc++" in hay or "dpcpp" in hay:
        return "intel-icx"
    if re.search(r"\bicx\b", hay) or "intel icx" in hay:
        return "intel-icx"
    if re.search(r"\bicc\b", hay) or "intel icc" in hay:
        return "intel-icc"


    if fam != "unknown" and fam != "intel":
        return fam

    if "g++" in hay or re.search(r"\bgcc\b", hay) or "gcc-" in hay or "mingw" in hay:

        if "mingw" in hay:
            return "mingw-gcc"
        return "gcc"
    if "clang" in hay:

        if "mingw" in hay:
            return "mingw-clang"
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

    pri = {
        "gcc": 0,
        "mingw-gcc": 1,
        "clang": 2,
        "mingw-clang": 3,
        "clang-cl": 4,
        "msvc": 5,
        "intel-icx": 6,
        "intel-icc": 7,
        "nvc++": 8,
    }
    return (pri.get(fam, 50), fam.casefold())


