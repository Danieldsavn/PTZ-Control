"""
Local MJPEG preview proxy — transcodes camera RTMP/RTSP to browser-friendly multipart JPEG.
Requires ffmpeg on PATH.
"""
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREVIEW_PORT = 8765
_ffmpeg_path = None
_procs: dict[str, subprocess.Popen] = {}
_proc_lock = threading.Lock()


def ffmpeg_available() -> bool:
    global _ffmpeg_path
    if _ffmpeg_path is not None:
        return bool(_ffmpeg_path)
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
        return subprocess.Popen(cmd, **popen_kw)
    except Exception:
        return None


def _get_proc(cam_key: str, source_url: str) -> subprocess.Popen | None:
    with _proc_lock:
        proc = _procs.get(cam_key)
        if proc and proc.poll() is None:
            return proc
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        proc = _start_ffmpeg(cam_key, source_url)
        if proc:
            _procs[cam_key] = proc
        return proc


def stop_all_previews():
    with _proc_lock:
        for proc in list(_procs.values()):
            try:
                proc.kill()
            except Exception:
                pass
        _procs.clear()


def invalidate_preview(cam_key: str):
    with _proc_lock:
        proc = _procs.pop(cam_key, None)
        if proc:
            try:
                proc.kill()
            except Exception:
                pass


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
            buf = b""
            try:
                while True:
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
                            self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                            self.wfile.write(frame)
                            self.wfile.write(b"\r\n")
                            self.wfile.flush()
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


def start_preview_server(source_urls: dict[str, str], port: int = PREVIEW_PORT) -> int | None:
    global _server
    if _server:
        _PreviewHandler.source_urls = dict(source_urls)
        return port
    try:
        _PreviewHandler.source_urls = dict(source_urls)
        _server = ThreadingHTTPServer(("127.0.0.1", port), _PreviewHandler)
        t = threading.Thread(target=_server.serve_forever, daemon=True)
        t.start()
        return port
    except OSError:
        return None


def preview_url(cam_key: str, port: int = PREVIEW_PORT) -> str:
    return f"http://127.0.0.1:{port}/preview/{cam_key}"
