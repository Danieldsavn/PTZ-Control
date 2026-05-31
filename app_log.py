"""
Session debug log for PTZ-Control troubleshooting.

Writes timestamped lines to logs/ beside the executable (or project root in dev).
Each run creates a new session file; logs/latest.txt points at the current session path.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from typing import Any, Optional

_lock = threading.Lock()
_enabled = True
_verbose_gostream = False
_log_dir: Optional[str] = None
_session_path: Optional[str] = None
_started = False

_LEVELS = {"DEBUG", "INFO", "WARN", "ERROR"}

# Log retention (pruned on each new session)
MAX_LOG_SESSIONS = 5
MAX_LOG_FILE_BYTES = 10 * 1024 * 1024

# High-frequency API polls — log returns at most every N seconds unless state changes
_POLL_API_METHODS = frozenset({"obs_get_status", "switcher_get_status"})
_POLL_API_LOG_INTERVAL_S = 30.0
_last_poll_api_log: dict[str, float] = {}
_last_poll_api_fingerprint: dict[str, str] = {}

_SENSITIVE_KEY_RE = re.compile(
    r"(stream.*key|rtmp|password|secret|token|api_?key)",
    re.IGNORECASE,
)
_RESTREAM_KEY_RE = re.compile(r"re_[A-Za-z0-9]{8,}")


def _default_log_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def log_dir() -> str:
    global _log_dir
    if _log_dir is None:
        _log_dir = os.path.join(_default_log_root(), "logs")
    return _log_dir


def session_path() -> Optional[str]:
    return _session_path


def enabled() -> bool:
    return _enabled


def verbose_gostream() -> bool:
    return _verbose_gostream and _enabled


def configure(
    *,
    enabled: bool = True,
    verbose_gostream: bool = False,
    log_dir_override: Optional[str] = None,
) -> None:
    global _enabled, _verbose_gostream, _log_dir
    _enabled = bool(enabled)
    _verbose_gostream = bool(verbose_gostream)
    if log_dir_override:
        _log_dir = log_dir_override


def redact_sensitive(value: Any) -> Any:
    """Mask stream keys and similar secrets before writing logs."""
    if value is None:
        return None
    if isinstance(value, str):
        if _RESTREAM_KEY_RE.search(value):
            return _RESTREAM_KEY_RE.sub("re_…redacted", value)
        if len(value) > 48 and value.isalnum():
            return value[:4] + "…" + value[-4:]
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if _SENSITIVE_KEY_RE.search(key):
                if isinstance(v, str) and v.strip():
                    s = v.strip()
                    out[key] = (s[:4] + "…" + s[-4:]) if len(s) > 12 else "…"
                else:
                    out[key] = v
            else:
                out[key] = redact_sensitive(v)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(v) for v in value]
    return value


def _format_detail(detail: Any) -> str:
    if detail is None:
        return ""
    try:
        detail = redact_sensitive(detail)
        if isinstance(detail, str):
            text = detail
        else:
            text = json.dumps(detail, ensure_ascii=False, default=str)
    except Exception:
        text = repr(detail)
    if len(text) > 2000:
        text = text[:2000] + "…"
    return text


def _poll_status_fingerprint(result: Any) -> str:
    if not isinstance(result, tuple) or len(result) < 2:
        return ""
    payload = result[1]
    if not isinstance(payload, dict):
        return str(payload)[:120]
    keys = (
        "connected",
        "pgmInput",
        "pvwInput",
        "usk1On",
        "dsk1On",
        "splitviewOn",
        "streamLive",
        "stream2Live",
        "mp1StillIndex",
    )
    return json.dumps({k: payload.get(k) for k in keys}, sort_keys=True, default=str)


def should_log_api_call(method: str, phase: str) -> bool:
    if method in _POLL_API_METHODS:
        return False
    if method == "set_camera_title":
        return False
    return True


def should_log_api_return(method: str, result: Any) -> bool:
    if method not in _POLL_API_METHODS:
        return True
    fp = _poll_status_fingerprint(result)
    now = time.monotonic()
    with _lock:
        last_fp = _last_poll_api_fingerprint.get(method)
        last_ts = _last_poll_api_log.get(method, 0.0)
        if fp != last_fp:
            _last_poll_api_fingerprint[method] = fp
            _last_poll_api_log[method] = now
            return True
        if now - last_ts >= _POLL_API_LOG_INTERVAL_S:
            _last_poll_api_log[method] = now
            return True
    return False


def safe_api_args(args: tuple, kwargs: dict) -> dict:
    out: dict[str, Any] = {}
    if args:
        out["args"] = redact_sensitive([str(a)[:200] for a in args[:6]])
    if kwargs:
        out["kwargs"] = redact_sensitive(
            {k: str(v)[:200] for k, v in list(kwargs.items())[:8]}
        )
    return out


def _prune_old_sessions() -> None:
    """Keep only the newest session logs under size/count limits."""
    try:
        root = log_dir()
        names = [
            n
            for n in os.listdir(root)
            if n.startswith("PTZ-Control_") and n.endswith(".log")
        ]
        paths = [os.path.join(root, n) for n in names]
        paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for path in paths[MAX_LOG_SESSIONS:]:
            try:
                os.remove(path)
            except OSError:
                pass
        for path in paths[:MAX_LOG_SESSIONS]:
            try:
                if os.path.getsize(path) > MAX_LOG_FILE_BYTES:
                    with open(path, "rb") as f:
                        f.seek(-MAX_LOG_FILE_BYTES, os.SEEK_END)
                        tail = f.read()
                    with open(path, "wb") as f:
                        f.write(
                            b"... [log truncated — size cap]\n".encode("utf-8")
                            + tail
                        )
            except OSError:
                pass
    except OSError:
        pass


def write(
    level: str,
    category: str,
    message: str,
    detail: Any = None,
) -> None:
    if not _enabled:
        return
    level = (level or "INFO").upper()
    if level not in _LEVELS:
        level = "INFO"
    category = (category or "APP").upper()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.") + f"{datetime.now().microsecond // 1000:03d}"
    line = f"{ts} | {level:<5} | {category:<8} | {message}"
    extra = _format_detail(detail)
    if extra:
        line += f" | {extra}"
    with _lock:
        try:
            if not _started:
                start_session()
            if _session_path:
                with open(_session_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass


def start_session() -> str:
    """Create logs/ and a new session file; return its path."""
    global _session_path, _started
    with _lock:
        if _started and _session_path:
            return _session_path
        os.makedirs(log_dir(), exist_ok=True)
        _prune_old_sessions()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        _session_path = os.path.join(log_dir(), f"PTZ-Control_{stamp}.log")
        _started = True
        header = [
            "=" * 72,
            f"PTZ-Control debug log session {stamp}",
            f"Python {sys.version.split()[0]} | frozen={getattr(sys, 'frozen', False)}",
            f"Log file: {_session_path}",
            "=" * 72,
        ]
        try:
            with open(_session_path, "w", encoding="utf-8") as f:
                f.write("\n".join(header) + "\n")
            latest_ptr = os.path.join(log_dir(), "latest.txt")
            with open(latest_ptr, "w", encoding="utf-8") as f:
                f.write(_session_path + "\n")
        except Exception:
            pass
    write("INFO", "SYSTEM", "Log session started")
    return _session_path or ""


def debug(category: str, message: str, detail: Any = None) -> None:
    write("DEBUG", category, message, detail)


def info(category: str, message: str, detail: Any = None) -> None:
    write("INFO", category, message, detail)


def warn(category: str, message: str, detail: Any = None) -> None:
    write("WARN", category, message, detail)


def error(category: str, message: str, detail: Any = None) -> None:
    write("ERROR", category, message, detail)


def user(action: str, target: str = "", detail: Any = None) -> None:
    msg = action if not target else f"{action} → {target}"
    write("INFO", "USER", msg, detail)


def api(method: str, phase: str, detail: Any = None) -> None:
    write("DEBUG", "API", f"{method} {phase}", detail)


def switcher(message: str, detail: Any = None) -> None:
    write("INFO", "SWITCH", message, detail)


def midi(message: str, detail: Any = None) -> None:
    write("INFO", "MIDI", message, detail)


def gsp(message: str, detail: Any = None) -> None:
    if not verbose_gostream():
        return
    write("DEBUG", "GSP", message, detail)


def camera(message: str, detail: Any = None) -> None:
    write("DEBUG", "CAMERA", message, detail)


def exception(category: str, message: str, exc: BaseException | None = None) -> None:
    detail: dict[str, Any] = {"error": str(exc) if exc else message}
    if exc is not None:
        detail["traceback"] = traceback.format_exc()
    write("ERROR", category, message, detail)


def summarize_api_result(result: Any) -> Any:
    """Trim API return values for log lines."""
    result = redact_sensitive(result)
    if isinstance(result, tuple) and len(result) >= 2:
        ok, payload = result[0], result[1]
        if isinstance(payload, dict):
            keys = list(payload.keys())[:12]
            slim = {k: payload[k] for k in keys}
            return {"ok": ok, "payload": slim}
        text = str(payload)
        if len(text) > 240:
            text = text[:240] + "…"
        return {"ok": ok, "payload": text}
    text = str(result)
    if len(text) > 400:
        text = text[:400] + "…"
    return text


def open_logs_folder() -> tuple[bool, str]:
    path = log_dir()
    os.makedirs(path, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        import subprocess

        subprocess.Popen(["open", path])
    else:
        import subprocess

        subprocess.Popen(["xdg-open", path])
    return True, path
