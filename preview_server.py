"""
Local MJPEG preview proxy — transcodes camera RTMP/RTSP to browser-friendly multipart JPEG.
Uses tools/ffmpeg.exe beside the app, or ffmpeg on PATH.
"""
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREVIEW_PORT = 8765
PREVIEW_WARMUP_S = 30.0
PREVIEW_STALE_S = 20.0
PREVIEW_MAX_AGE_S = 480.0
WATCHDOG_INTERVAL_S = 15.0
STREAM_MAX_AGE_S = 600.0

_ffmpeg_path: str | None = None
_procs: dict[str, subprocess.Popen] = {}
_proc_lock = threading.Lock()
_stream_locks: dict[str, threading.Lock] = {}
_last_frame_at: dict[str, float | None] = {}
_proc_started_at: dict[str, float] = {}
_watchdog_stop = threading.Event()
_watchdog_thread: threading.Thread | None = None


def _stream_lock(cam_key: str) -> threading.Lock:
    with _proc_lock:
        lock = _stream_locks.get(cam_key)
        if lock is None:
            lock = threading.Lock()
            _stream_locks[cam_key] = lock
        return lock


def _terminate_proc(proc: subprocess.Popen | None) -> None:
    if not proc:
        return
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=1.0)
    except Exception:
        pass


def _note_frame(cam_key: str) -> None:
    _last_frame_at[cam_key] = time.monotonic()


def _proc_is_stale(cam_key: str, proc: subprocess.Popen) -> bool:
    if proc.poll() is not None:
        return True
    now = time.monotonic()
    started = _proc_started_at.get(cam_key, 0.0)
    if started and now - started > PREVIEW_MAX_AGE_S:
        return True
    last = _last_frame_at.get(cam_key)
    if last is None:
        # Allow generous warmup before the first JPEG frame arrives.
        return bool(started and now - started > PREVIEW_WARMUP_S)
    return now - last > PREVIEW_STALE_S


def _drop_proc_locked(cam_key: str) -> None:
    proc = _procs.pop(cam_key, None)
    if proc:
        _terminate_proc(proc)
    _last_frame_at.pop(cam_key, None)
    _proc_started_at.pop(cam_key, None)


def _watchdog_loop() -> None:
    while not _watchdog_stop.wait(WATCHDOG_INTERVAL_S):
        with _proc_lock:
            for cam_key, proc in list(_procs.items()):
                if _proc_is_stale(cam_key, proc):
                    _drop_proc_locked(cam_key)


def _ensure_watchdog() -> None:
    global _watchdog_thread
    if _watchdog_thread and _watchdog_thread.is_alive():
        return
    _watchdog_stop.clear()
    _watchdog_thread = threading.Thread(
        target=_watchdog_loop, name="PreviewWatchdog", daemon=True
    )
    _watchdog_thread.start()


def _app_install_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def ffmpeg_available() -> bool:
    global _ffmpeg_path
    if _ffmpeg_path is not None:
        return bool(_ffmpeg_path)
    bundled = os.path.join(_app_install_dir(), "tools", "ffmpeg.exe")
    if os.path.isfile(bundled):
        _ffmpeg_path = bundled
        return True
    _ffmpeg_path = shutil.which("ffmpeg") or ""
    return bool(_ffmpeg_path)


def _start_ffmpeg(cam_key: str, source_url: str) -> subprocess.Popen | None:
    if not ffmpeg_available():
        return None
    cmd = [
        _ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-probesize",
        "32",
        "-analyzeduration",
        "0",
    ]
    url = source_url.lower()
    if url.startswith("rtsp://"):
        cmd.extend(["-rtsp_transport", "tcp"])
    cmd.extend(
        [
            "-i",
            source_url,
            "-an",
            "-f",
            "mjpeg",
            "-q:v",
            "8",
            "-r",
            "10",
            "-vf",
            "scale=960:-2",
            "pipe:1",
        ]
    )
    try:
        popen_kw: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "bufsize": 0,
        }
        if sys.platform == "win32":
            popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(cmd, **popen_kw)
        _proc_started_at[cam_key] = time.monotonic()
        _last_frame_at[cam_key] = None
        return proc
    except Exception:
        return None


def _get_proc(cam_key: str, source_url: str) -> subprocess.Popen | None:
    with _proc_lock:
        proc = _procs.get(cam_key)
        if proc and proc.poll() is None and not _proc_is_stale(cam_key, proc):
            return proc
        if proc:
            _drop_proc_locked(cam_key)
        proc = _start_ffmpeg(cam_key, source_url)
        if proc:
            _procs[cam_key] = proc
        return proc


def stop_all_previews():
    with _proc_lock:
        for cam_key in list(_procs.keys()):
            _drop_proc_locked(cam_key)


def invalidate_preview(cam_key: str):
    with _proc_lock:
        _drop_proc_locked(cam_key)


def refresh_all_previews() -> None:
    with _proc_lock:
        for cam_key in list(_procs.keys()):
            _drop_proc_locked(cam_key)


class _PreviewHandler(BaseHTTPRequestHandler):
    source_urls: dict[str, str] = {}

    def log_message(self, *_args, **_kwargs):
        pass

    def do_GET(self):
        if self.path.startswith("/preview/"):
            cam = self.path.split("/preview/", 1)[1].split("?", 1)[0].strip("/")
            if cam not in ("cam1", "cam2"):
                self.send_error(404)
                return
            url = self.source_urls.get(cam, "")
            if not url:
                self.send_error(503, "No stream URL")
                return
            if not ffmpeg_available():
                self.send_error(503, "ffmpeg not found")
                return
            proc = _get_proc(cam, url)
            if not proc or not proc.stdout:
                self.send_error(503, "Preview unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            with _stream_lock(cam):
                buf = b""
                stream_start = time.monotonic()
                try:
                    while True:
                        if time.monotonic() - stream_start > STREAM_MAX_AGE_S:
                            break
                        if proc.poll() is not None:
                            break
                        chunk = proc.stdout.read(4096)
                        if not chunk:
                            break
                        buf += chunk
                        while True:
                            start = buf.find(b"\xff\xd8")
                            if start < 0:
                                buf = b""
                                break
                            end = buf.find(b"\xff\xd9", start + 2)
                            if end < 0:
                                buf = buf[start:]
                                break
                            frame = buf[start : end + 2]
                            buf = buf[end + 2 :]
                            try:
                                self.wfile.write(b"--frame\r\n")
                                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                                self.wfile.write(
                                    f"Content-Length: {len(frame)}\r\n\r\n".encode()
                                )
                                self.wfile.write(frame)
                                self.wfile.write(b"\r\n")
                                self.wfile.flush()
                                _note_frame(cam)
                            except Exception:
                                return
                except Exception:
                    return
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_error(404)


_server: ThreadingHTTPServer | None = None
_server_thread: threading.Thread | None = None


def start_preview_server(source_urls: dict[str, str], port: int = PREVIEW_PORT) -> int | None:
    global _server, _server_thread
    _ensure_watchdog()
    if _server:
        _PreviewHandler.source_urls = dict(source_urls)
        return port
    try:
        _PreviewHandler.source_urls = dict(source_urls)
        _server = ThreadingHTTPServer(("127.0.0.1", port), _PreviewHandler)
        _server_thread = threading.Thread(target=_server.serve_forever, daemon=True)
        _server_thread.start()
        return port
    except OSError:
        _server = None
        _server_thread = None
        return None


def shutdown_preview_server() -> None:
    """Stop FFmpeg children and the preview HTTP server."""
    global _server, _server_thread
    _watchdog_stop.set()
    stop_all_previews()
    srv = _server
    if srv:
        try:
            srv.shutdown()
        except Exception:
            pass
    _server = None
    th = _server_thread
    _server_thread = None
    if th and th.is_alive():
        th.join(timeout=2.0)


def preview_url(cam_key: str, port: int = PREVIEW_PORT) -> str:
    return f"http://127.0.0.1:{port}/preview/{cam_key}"
