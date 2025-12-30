"""Compiler flag/argument helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass


def std_flags_for_family(fam: str, std: str) -> str:

    f = fam.strip().lower()
    s = std.strip().lower()


    if f in ("msvc", "clang-cl"):
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


def _flag_style_for_family(fam: str) -> str:


    f = (fam or "").strip().lower()
    return "msvc" if f in ("msvc", "clang-cl") else "gnu"


def _normalize_user_flags_text(s: str) -> str:


    return re.sub(r"\s+", " ", str(s or "")).strip()


@dataclass(frozen=True)
class ExtraFlagsConfig:

    extra_gnu: str
    extra_msvc: str
    extra_by_group: dict[str, str]

    def extra_for_group(self, fam: str, platform: str, series: str) -> str:

        key = f"{fam}|{platform}|{series}"
        return str(self.extra_by_group.get(key, "") or "")


