"""Compiler Explorer API client and workers."""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from cetest_core import CODE_ABORTED, CODE_TIMEOUT, CODE_TRANSPORT_ERROR, LOG, CancelledByUser
from cetest_flags import ExtraFlagsConfig
from cetest_models import CeLibraryInfo, CompilerInfo, parse_semver_key
from cetest_prefs import LibraryRule, RateLimiter, _effective_libraries_for_compiler, build_user_args_for_group


class CeClient:
    """Compiler Explorer HTTP client with retry/backoff and a small compile cache."""

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


        # CE can throw 429/5xx bursts; a little backoff keeps the UI from feeling flaky.
        max_retries = 6
        backoff = 0.6

        for attempt in range(max_retries):
            if abort_event is not None and abort_event.is_set():
                raise CancelledByUser()

            # Global throttle so we don't hammer CE (especially during probing).
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
                # Honor server-provided backoff when available (common for 429).
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

    def list_libraries_cpp(self, abort_event: threading.Event | None = None) -> list[CeLibraryInfo]:


        items = self._request_json("GET", "/api/libraries/c++", timeout_s=45.0, abort_event=abort_event)
        if isinstance(items, dict) and "libraries" in items and isinstance(items["libraries"], list):
            items = items["libraries"]
        if not isinstance(items, list):
            return []

        out: list[CeLibraryInfo] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            lid = str(it.get("id") or "")
            name = str(it.get("name") or lid)
            versions_raw = it.get("versions")
            versions: list[str] = []
            if isinstance(versions_raw, list):
                versions = [str(v.get("id") or v.get("version") or v) for v in versions_raw if v is not None]
            elif isinstance(versions_raw, dict):

                versions = [str(k) for k in versions_raw.keys()]
            if not lid or not versions:
                continue

            versions = [v.strip() for v in versions if str(v).strip()]
            versions.sort(key=lambda s: parse_semver_key(s), reverse=True)
            out.append(CeLibraryInfo(id=lid, name=name, versions=versions))
        out.sort(key=lambda x: x.name.casefold())
        return out

    def compile_cached(
        self,
        compiler_id: str,
        source: str,
        user_arguments: str,
        libraries: list[dict[str, str]] | None = None,
        abort_event: threading.Event | None = None,
        timeout_s: float = 45.0,
    ) -> dict[str, Any]:



        h = hashlib.sha1(source.encode("utf-8")).hexdigest()
        libs_key: tuple[tuple[str, str], ...] = ()
        if isinstance(libraries, list):
            pairs: list[tuple[str, str]] = []
            for it in libraries:
                if not isinstance(it, dict):
                    continue
                lid = _normalize_ce_library_id(str(it.get("id") or ""))
                ver = _normalize_ce_library_version(str(it.get("version") or ""))
                if lid and ver:
                    pairs.append((lid, ver))
            libs_key = tuple(sorted(set(pairs)))

        key = (compiler_id, user_arguments, libs_key, h)
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
                "libraries": (libraries if isinstance(libraries, list) else []),
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



    platform: str
    series: str
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



    platform: str
    series: str
    compiler_type: str
    highest_supported: CeAttempt | None
    lowest_supported: CeAttempt | None
    first_failure: CeAttempt | None
    attempts: list[CeAttempt]
    inconclusive_reason: str | None = None


def stderr_text_from_resp(resp: dict[str, Any]) -> str:


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



    group_done = pyqtSignal(object)
    finished = pyqtSignal()
    failed = pyqtSignal(str)
    aborted = pyqtSignal()

    def __init__(
        self,
        ce: CeClient,
        jobs: list[tuple[str, str, str, list[CompilerInfo]]],
        source: str,
        cpp_std: str,
        extra_flags: "ExtraFlagsConfig",
        library_rules: list[LibraryRule],
        abort_event: threading.Event,
    ):

        super().__init__()
        self._ce = ce
        self._jobs = jobs
        self._source = source
        self._cpp_std = cpp_std
        self._extra_flags = extra_flags
        self._library_rules = list(library_rules or [])
        self._abort = abort_event

    def _cancelled(self) -> bool:

        if self._abort.is_set():
            return True
        t = QThread.currentThread()
        return bool(t and t.isInterruptionRequested())

    def run(self):

        try:
            for platform, series, compiler_type, compilers in self._jobs:
                if self._cancelled():
                    raise CancelledByUser()

                try:
                    LOG.debug(
                        "Probe start group fam=%s platform=%s series=%s candidates=%d",
                        compiler_type,
                        platform,
                        series,
                        len(compilers),
                    )
                    summary = self._probe_group_binary(platform, series, compiler_type, compilers)
                    self.group_done.emit(summary)
                except CancelledByUser:
                    raise
                except Exception as e:
                    LOG.exception("Group probe failed fam=%s platform=%s series=%s", compiler_type, platform, series)
                    summary = CeGroupSummary(
                        platform=platform,
                        series=series,
                        compiler_type=compiler_type,
                        highest_supported=None,
                        lowest_supported=None,
                        first_failure=CeAttempt(
                            platform=platform,
                            series=series,
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

    def _probe_group_binary(self, platform: str, series: str, fam: str, compilers: list[CompilerInfo]) -> CeGroupSummary:
        """Find the oldest passing compiler in a group using a boundary binary-search."""

        # compilers is newest->oldest; we look for the OK->FAIL boundary with a binary search.

        n = len(compilers)
        attempts_by_idx: dict[int, CeAttempt] = {}
        user_args = build_user_args_for_group(fam, platform, series, self._cpp_std, self._extra_flags)
        inconclusive_reason: str | None = None

        def test(i: int) -> CeAttempt:

            nonlocal inconclusive_reason
            if self._cancelled():
                raise CancelledByUser()

            # Cache by index; binary search tends to revisit the same midpoints.
            hit = attempts_by_idx.get(i)
            if hit is not None:
                return hit

            ci = compilers[i]
            try:
                libs = _effective_libraries_for_compiler(self._library_rules, fam, ci.id)
                resp = self._ce.compile_cached(
                    ci.id,
                    self._source,
                    user_args,
                    libraries=libs,
                    abort_event=self._abort,
                    timeout_s=45.0,
                )
                code = int(resp.get("code", -1))
                att = CeAttempt(
                    platform=platform,
                    series=series,
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
                    platform=platform,
                    series=series,
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
                    platform=platform,
                    series=series,
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
                platform=platform,
                series=series,
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
                platform=platform,
                series=series,
                compiler_type=fam,
                highest_supported=None,
                lowest_supported=None,
                first_failure=newest,
                attempts=tested,
                inconclusive_reason=inconclusive_reason,
            )

        if not newest.ok():
            # Newest fails => the feature never worked for this group (at least in this list).
            return CeGroupSummary(
                platform=platform,
                series=series,
                compiler_type=fam,
                highest_supported=None,
                lowest_supported=None,
                first_failure=newest,
                attempts=[newest],
                inconclusive_reason=None,
            )

        if n == 1:
            return CeGroupSummary(
                platform=platform,
                series=series,
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
                platform=platform,
                series=series,
                compiler_type=fam,
                highest_supported=newest,
                lowest_supported=None,
                first_failure=oldest,
                attempts=tested,
                inconclusive_reason=inconclusive_reason,
            )

        if oldest.ok():
            # Oldest passes => everything in this group passes; boundary doesn't exist.
            tested = [attempts_by_idx[i] for i in sorted(attempts_by_idx.keys())]
            return CeGroupSummary(
                platform=platform,
                series=series,
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
                    platform=platform,
                    series=series,
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
                platform=platform,
                series=series,
                compiler_type=fam,
                highest_supported=newest,
                lowest_supported=None,
                first_failure=first_fail,
                attempts=tested,
                inconclusive_reason=inconclusive_reason,
            )

        return CeGroupSummary(
            platform=platform,
            series=series,
            compiler_type=fam,
            highest_supported=newest,
            lowest_supported=lowest_ok if lowest_ok.ok() else None,
            first_failure=first_fail if not first_fail.ok() else None,
            attempts=tested,
            inconclusive_reason=None,
        )


