import os
import re
import shutil
import subprocess
import sys
from typing import Any, Callable

# PyWebView uses pythonnet/Edge; configure before webview import (critical for frozen restart).
if sys.platform == "win32":
    os.environ.setdefault("PYTHONNET_RUNTIME", "netfx")
    os.environ.setdefault("PYWEBVIEW_GUI", "edgechromium")

_SINGLE_INSTANCE_MUTEX = None


def _acquire_single_instance() -> bool:
    """Only one PTZ-Control process at a time. Returns False if another is already running."""
    global _SINGLE_INSTANCE_MUTEX
    if sys.platform == "win32":
        import ctypes

        ERROR_ALREADY_EXISTS = 183
        # Released automatically when this process exits (restart/update safe).
        name = "Local\\PTZ-Control-SingleInstance"
        handle = ctypes.windll.kernel32.CreateMutexW(None, True, name)
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
            ctypes.windll.user32.MessageBoxW(
                None,
                "PTZ-Control is already running.\n\n"
                "Only one copy can be open at a time. Close the other window first, "
                "or use Restart in settings if you need to reload the app.",
                "PTZ-Control",
                0x30,
            )
            return False
        _SINGLE_INSTANCE_MUTEX = handle
        return True

    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 47823))
        sock.listen(1)
    except OSError:
        print("PTZ-Control is already running.", file=sys.stderr)
        return False
    _SINGLE_INSTANCE_MUTEX = sock
    return True


import json
import socket
import threading
import time
import requests
import webview
from gostream_client import (
    GoStreamClient,
    DEFAULT_PORT as GOSTREAM_DEFAULT_PORT,
    SOURCE_IN1,
    SOURCE_IN2,
    SOURCE_IN3,
    SOURCE_IN4,
    STREAM_1_ID,
    STREAM_2_ID,
    USK_1_KEY_ID,
    DSK_1_KEY_ID,
    DSK_FILL_STILL_1,
    SOURCE_MP,
    SOURCE_MULTI,
    MULTISOURCE_WINDOW_1_ID,
    MULTISOURCE_WINDOW_2_ID,
)
from preview_server import (
    PREVIEW_PORT,
    ffmpeg_available,
    invalidate_preview,
    preview_url,
    refresh_all_previews,
    shutdown_preview_server,
    start_preview_server,
    stop_all_previews,
)
import app_log
from midi_manager import (
    MidiManager,
    SCENES as MIDI_SCENES,
    list_input_devices,
    midi_available,
    normalize_midi_config,
)
from update_checker import (
    DEFAULT_MANIFEST_URL,
    check_for_update,
    get_update_work_dir,
    launch_gui_updater,
    launch_restart,
    normalize_manifest_url,
    running_exe_path,
    write_update_job,
)

# ---- Camera config ----
CAMERAS = {
    "cam1": {"ip": "192.168.2.91", "visca_port": 52381},
    "cam2": {"ip": "192.168.2.92", "visca_port": 52381},
}

# If your camera requires auth for the HTTP CGI tracking endpoint:
AUTH_USER = None
AUTH_PASS = None

HTTP_TIMEOUT = 2.5
VISCA_TIMEOUT = 1.5  # UDP send won't block much, but keep for socket timeout

VALID_PRESET_COUNTS = (2, 4, 6, 8, 10, 12)
DEFAULT_PRESET_COUNT = 6
SIMPLE_PRESETS = (13, 14, 15, 16, 17, 18)  # 6 presets always used for simple mode
DEFAULT_SPEED_PCT = 60  # 0-100 slider percent, persisted


# -------- paths (important for PyInstaller) --------
def _exe_dir():
    # Where we want to WRITE files (next to the EXE)
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _app_data_dir():
    # Persistent writable folder that survives app updates/reinstalls.
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or _exe_dir()
    else:
        base = os.path.expanduser("~")
    path = os.path.join(base, "PTZ-Control")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        return _exe_dir()
    return path


def _persist_path(filename: str) -> str:
    """
    Return persistent path for runtime data and migrate legacy files written
    next to the EXE on first launch after upgrade.
    """
    target = os.path.join(_app_data_dir(), filename)
    if os.path.exists(target):
        return target
    legacy = os.path.join(_exe_dir(), filename)
    if legacy != target and os.path.exists(legacy):
        try:
            shutil.copy2(legacy, target)
        except Exception:
            pass
    return target


def _ui_base_dir():
    # Where we want to READ bundled UI assets from
    if hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


PRESETS_FILE = _persist_path("presets.json")
SWITCHER_CONFIG_FILE = _persist_path("switcher.json")
OBS_CONFIG_FILE = _persist_path("obs.json")  # legacy; migrated to switcher.json
WINDOW_CONFIG_FILE = _persist_path("window.json")
VERSION_FILE = os.path.join(_exe_dir(), "version.json")
DEFAULT_VERSION = "3.0"

_app_window = None

# Runtime camera IPs (loaded from presets.json, used by _base_url and _visca_send_udp)
_camera_ips = {}


def _get_camera_ip(cam_key: str) -> str:
    return _camera_ips.get(cam_key) or CAMERAS.get(cam_key, {}).get("ip", "")


def _auth():
    if AUTH_USER and AUTH_PASS:
        return (AUTH_USER, AUTH_PASS)
    return None


def _base_url(cam_key: str) -> str:
    ip = _get_camera_ip(cam_key)
    return f"http://{ip}"


def _post(cam_key: str, endpoint: str, data: dict):
    """
    Sends a POST to the camera and returns (ok, text_or_json).
    """
    url = _base_url(cam_key) + endpoint
    r = requests.post(
        url,
        data=data,
        auth=_auth(),
        verify=False,
        timeout=HTTP_TIMEOUT,
        headers={"Accept": "application/json, text/plain, */*"},
    )
    try:
        return True, r.json()
    except Exception:
        return True, r.text


def _get(cam_key: str, endpoint: str, params: dict | None = None, timeout: float | None = None):
    """
    Lightweight GET helper (used for camera_ping).
    """
    url = _base_url(cam_key) + endpoint
    r = requests.get(
        url,
        params=params,
        auth=_auth(),
        verify=False,
        timeout=timeout or HTTP_TIMEOUT,
        headers={"Accept": "application/json, text/plain, */*"},
    )
    try:
        return True, r.json()
    except Exception:
        return True, r.text


# ---------------- VISCA over IP (UDP only) ----------------
_visca_lock = threading.Lock()
_visca_seq = {k: 1 for k in CAMERAS.keys()}


def _visca_wrap(cam_key: str, payload: bytes) -> bytes:
    """
    Sony-style VISCA over IP header (8 bytes) + VISCA payload.
    Header:
      01 00 <len_hi> <len_lo> <seq_32bit>
    """
    global _visca_seq
    with _visca_lock:
        seq = _visca_seq.get(cam_key, 1)
        _visca_seq[cam_key] = (seq + 1) & 0xFFFFFFFF

    length = len(payload)
    header = bytes([
        0x01, 0x00,
        (length >> 8) & 0xFF,
        (length >> 0) & 0xFF,
        (seq >> 24) & 0xFF,
        (seq >> 16) & 0xFF,
        (seq >> 8) & 0xFF,
        (seq >> 0) & 0xFF,
    ])
    return header + payload


def _visca_send_udp(cam_key: str, payload: bytes):
    ip = _get_camera_ip(cam_key)
    port = CAMERAS.get(cam_key, {}).get("visca_port", 52381)
    packet = _visca_wrap(cam_key, payload)

    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(VISCA_TIMEOUT)
        s.sendto(packet, (ip, port))
        return True, "UDP: sent"
    except Exception as e:
        return False, f"VISCA UDP error: {e}"
    finally:
        if s:
            try:
                s.close()
            except OSError:
                pass


# ---------------- VISCA payload builders ----------------
def _clamp(n, lo, hi):
    return max(lo, min(int(n), hi))


def _visca_ptz_drive(direction: str, speed: int):
    """
    VISCA Pan/Tilt Drive:
      81 01 06 01 VV WW PP TT FF
    VV pan speed 0x01-0x18
    WW tilt speed 0x01-0x14
    PP: 01 left, 02 right, 03 stop
    TT: 01 up,   02 down,  03 stop
    """
    speed = _clamp(speed, 1, 10)
    pan = int(round((speed / 10) * 0x18)) or 0x01
    tilt = int(round((speed / 10) * 0x14)) or 0x01
    pan = _clamp(pan, 0x01, 0x18)
    tilt = _clamp(tilt, 0x01, 0x14)

    pan_dir = 0x03
    tilt_dir = 0x03

    if direction == "up":
        pan_dir, tilt_dir = 0x03, 0x01
    elif direction == "down":
        pan_dir, tilt_dir = 0x03, 0x02
    elif direction == "left":
        pan_dir, tilt_dir = 0x01, 0x03
    elif direction == "right":
        pan_dir, tilt_dir = 0x02, 0x03
    elif direction == "stop":
        pan_dir, tilt_dir = 0x03, 0x03
    else:
        raise ValueError(f"Unknown PT direction: {direction}")

    return bytes([0x81, 0x01, 0x06, 0x01, pan, tilt, pan_dir, tilt_dir, 0xFF])


def _visca_zoom(direction: str, speed: int):
    """
    VISCA Zoom:
      81 01 04 07 2p FF  (tele)
      81 01 04 07 3p FF  (wide)
      81 01 04 07 00 FF  (stop)
    p = 0..7
    """
    speed = _clamp(speed, 1, 10)
    p = int(round((speed / 10) * 7)) or 1
    p = _clamp(p, 0, 7)

    if direction == "zoom_in":
        z = 0x20 | p
    elif direction == "zoom_out":
        z = 0x30 | p
    elif direction in ("stop", "zoom_stop"):
        z = 0x00
    else:
        raise ValueError(f"Unknown zoom direction: {direction}")

    return bytes([0x81, 0x01, 0x04, 0x07, z, 0xFF])


def _visca_focus(direction: str, speed: int):
    """
    VISCA Focus (manual):
      81 01 04 08 2p FF  (far / out)
      81 01 04 08 3p FF  (near / in)
      81 01 04 08 00 FF  (stop)
    p = 0..7
    """
    speed = _clamp(speed, 1, 10)
    p = int(round((speed / 10) * 7)) or 1
    p = _clamp(p, 0, 7)

    if direction == "focus_in":
        z = 0x30 | p
    elif direction == "focus_out":
        z = 0x20 | p
    elif direction in ("stop", "focus_stop"):
        z = 0x00
    else:
        raise ValueError(f"Unknown focus direction: {direction}")

    return bytes([0x81, 0x01, 0x04, 0x08, z, 0xFF])


def _visca_autofocus(on: bool) -> bytes:
    """VISCA AF mode: 04 38 02 = auto on, 04 38 03 = manual."""
    mode = 0x02 if on else 0x03
    return bytes([0x81, 0x01, 0x04, 0x38, mode, 0xFF])


def _visca_preset_recall(preset: int):
    """
    Preset recall:
      81 01 04 3F 02 pp FF
    """
    preset = _clamp(preset, 0, 127)
    return bytes([0x81, 0x01, 0x04, 0x3F, 0x02, preset & 0xFF, 0xFF])


def _visca_preset_set(preset: int):
    """
    Preset set (save):
      81 01 04 3F 01 pp FF
    """
    preset = _clamp(preset, 0, 127)
    return bytes([0x81, 0x01, 0x04, 0x3F, 0x01, preset & 0xFF, 0xFF])


# ---------------- Presets + label + speed persistence (one file) ----------------
def _normalize_preset_count(data):
    """Return valid preset_count (2,4,6,8,10,12) from state/data."""
    n = int(data.get("preset_count", DEFAULT_PRESET_COUNT))
    return n if n in VALID_PRESET_COUNTS else DEFAULT_PRESET_COUNT


def _default_state(preset_count=None):
    if preset_count is None or preset_count not in VALID_PRESET_COUNTS:
        preset_count = DEFAULT_PRESET_COUNT
    data = {"preset_count": preset_count, "labels": {}, "simple_labels": {}, "speed_pct": {}, "camera_titles": {}, "camera_ips": {}}
    for cam in CAMERAS.keys():
        data["labels"][cam] = {str(i): f"Preset {i}" for i in range(1, preset_count + 1)}
        data["simple_labels"][cam] = {str(i): f"Preset {i}" for i in SIMPLE_PRESETS}
        data["speed_pct"][cam] = DEFAULT_SPEED_PCT
        data["camera_titles"][cam] = ""
        data["camera_ips"][cam] = CAMERAS[cam]["ip"]
    return data


def _save_state(data):
    try:
        with open(PRESETS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _load_state():
    """
    Supports:
      New format:
        {"labels": {"cam1": {...}, "cam2": {...}}, "speed_pct": {"cam1": 60, "cam2": 60}}
      Old format (labels only):
        {"cam1": {...}, "cam2": {...}}
    """
    try:
        if os.path.exists(PRESETS_FILE):
            with open(PRESETS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            base = _default_state()

            # Migrate old format (labels only)
            if isinstance(data, dict) and "labels" not in data:
                migrated = _default_state()
                for cam in CAMERAS.keys():
                    if cam in data and isinstance(data[cam], dict):
                        # overwrite defaults with user labels
                        migrated["labels"][cam].update({
                            str(k): str(v) for k, v in data[cam].items()
                            if str(k).isdigit()
                        })
                data = migrated

            # Ensure required keys
            if not isinstance(data, dict):
                data = _default_state()

            if "labels" not in data or not isinstance(data["labels"], dict):
                data["labels"] = base["labels"]

            if "speed_pct" not in data or not isinstance(data["speed_pct"], dict):
                data["speed_pct"] = base["speed_pct"]

            if "camera_titles" not in data or not isinstance(data["camera_titles"], dict):
                data["camera_titles"] = base["camera_titles"]
            for cam in CAMERAS.keys():
                if cam not in data["camera_titles"] or not isinstance(data["camera_titles"][cam], str):
                    data["camera_titles"][cam] = base["camera_titles"][cam]

            if "camera_ips" not in data or not isinstance(data["camera_ips"], dict):
                data["camera_ips"] = base["camera_ips"]
            for cam in CAMERAS.keys():
                if cam not in data["camera_ips"] or not isinstance(data["camera_ips"].get(cam), str) or not data["camera_ips"][cam].strip():
                    data["camera_ips"][cam] = base["camera_ips"][cam]

            if "simple_labels" not in data or not isinstance(data["simple_labels"], dict):
                data["simple_labels"] = base["simple_labels"]
            for cam in CAMERAS.keys():
                if cam not in data["simple_labels"] or not isinstance(data["simple_labels"][cam], dict):
                    data["simple_labels"][cam] = {str(i): f"Preset {i}" for i in SIMPLE_PRESETS}
                sl = data["simple_labels"][cam]
                data["simple_labels"][cam] = {str(i): sl.get(str(i), f"Preset {i}") for i in SIMPLE_PRESETS}

            data["preset_count"] = _normalize_preset_count(data)
            preset_count = data["preset_count"]

            # Validate per cam
            for cam in CAMERAS.keys():
                # labels
                if cam not in data["labels"] or not isinstance(data["labels"][cam], dict):
                    data["labels"][cam] = {str(i): f"Preset {i}" for i in range(1, preset_count + 1)}
                base_labels = {str(i): f"Preset {i}" for i in range(1, preset_count + 1)}
                for i in range(1, preset_count + 1):
                    k = str(i)
                    if k not in data["labels"][cam] or not isinstance(data["labels"][cam].get(k), str):
                        data["labels"][cam][k] = base_labels.get(k, f"Preset {i}")
                data["labels"][cam] = {k: v for k, v in data["labels"][cam].items() if k.isdigit() and 1 <= int(k) <= preset_count}

                # speed pct
                if cam not in data["speed_pct"]:
                    data["speed_pct"][cam] = base["speed_pct"][cam]
                try:
                    v = int(data["speed_pct"][cam])
                    data["speed_pct"][cam] = max(0, min(100, v))
                except Exception:
                    data["speed_pct"][cam] = base["speed_pct"][cam]

            _save_state(data)
            return data
    except Exception:
        pass

    data = _default_state()
    # Only create file if it doesn't exist; never overwrite on load failure
    if not os.path.exists(PRESETS_FILE):
        _save_state(data)
    return data


# ---------------- GoStream switcher config (OSEE GoStream Duet) ----------------
def _default_switcher_config():
    return {
        "host": "192.168.1.80",
        "port": GOSTREAM_DEFAULT_PORT,
        "sdi_inputs": {
            "cam1": SOURCE_IN1,
            "cam2": SOURCE_IN2,
        },
        "auto_reconnect": True,
        "switching_hold_seconds": 0,
        "simple_mode": False,
        "stream1_key": "",
        "settings_last_tab": "stream",
        "keyboard_hints_dismissed": False,
        "usk1_fill_source": 3,
        "usk1_key_source": 4,
        "dsk1_fill_source": DSK_FILL_STILL_1,
        "keys_use_mix_fade": True,
        "key_mix_rate_seconds": 1.0,
        "camera_mix_rate_seconds": 1.0,
        "splitview_mix_rate_seconds": 1.0,
        "midi": normalize_midi_config({}),
        "update_check_enabled": True,
        "update_manifest_url": DEFAULT_MANIFEST_URL,
        "debug_log_enabled": True,
        "debug_log_verbose_gostream": False,
    }


def _apply_log_settings_from_config(cfg: dict | None) -> None:
    data = cfg if isinstance(cfg, dict) else {}
    app_log.configure(
        enabled=bool(data.get("debug_log_enabled", True)),
        verbose_gostream=bool(data.get("debug_log_verbose_gostream", False)),
    )


def _migrate_legacy_obs_config(data: dict) -> dict:
    """Convert old obs.json fields into switcher.json format."""
    base = _default_switcher_config()
    out = dict(base)
    if isinstance(data, dict):
        if data.get("host"):
            out["host"] = str(data["host"]).strip()
        try:
            p = int(data.get("port", GOSTREAM_DEFAULT_PORT))
            out["port"] = GOSTREAM_DEFAULT_PORT if p == 4455 else p
        except Exception:
            pass
        if "auto_reconnect" in data:
            out["auto_reconnect"] = bool(data["auto_reconnect"])
        if "switching_hold_seconds" in data:
            try:
                out["switching_hold_seconds"] = max(0, min(5, int(data["switching_hold_seconds"])))
            except Exception:
                pass
        if "simple_mode" in data:
            out["simple_mode"] = bool(data["simple_mode"])
    return out


def _load_switcher_config():
    base = _default_switcher_config()
    try:
        if os.path.exists(SWITCHER_CONFIG_FILE):
            with open(SWITCHER_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key in base:
                    if key not in data:
                        data[key] = base[key]
                if not isinstance(data.get("sdi_inputs"), dict):
                    data["sdi_inputs"] = dict(base["sdi_inputs"])
                for cam in CAMERAS:
                    if cam not in data["sdi_inputs"]:
                        data["sdi_inputs"][cam] = base["sdi_inputs"][cam]
                try:
                    data["port"] = int(data.get("port", GOSTREAM_DEFAULT_PORT))
                except Exception:
                    data["port"] = GOSTREAM_DEFAULT_PORT
                try:
                    v = int(data["switching_hold_seconds"])
                    data["switching_hold_seconds"] = max(0, min(5, v))
                except Exception:
                    data["switching_hold_seconds"] = 0
                data["auto_reconnect"] = True
                tab = data.get("settings_last_tab", base["settings_last_tab"])
                if tab not in ("switcher", "stream", "general", "midi"):
                    tab = base["settings_last_tab"]
                data["settings_last_tab"] = tab
                data["midi"] = normalize_midi_config(data.get("midi"))
                data["keyboard_hints_dismissed"] = bool(
                    data.get("keyboard_hints_dismissed", False)
                )
                if "update_check_enabled" not in data:
                    data["update_check_enabled"] = base["update_check_enabled"]
                data["update_manifest_url"] = normalize_manifest_url(
                    (data.get("update_manifest_url") or "").strip()
                    or base["update_manifest_url"]
                )
                if "debug_log_enabled" not in data:
                    data["debug_log_enabled"] = base["debug_log_enabled"]
                if "debug_log_verbose_gostream" not in data:
                    data["debug_log_verbose_gostream"] = base[
                        "debug_log_verbose_gostream"
                    ]
                if "camera_mix_rate_seconds" not in data:
                    legacy_rate = float(data.get("key_mix_rate_seconds", 1.0))
                    half = max(0.5, legacy_rate / 2.0)
                    data["camera_mix_rate_seconds"] = half
                    data["splitview_mix_rate_seconds"] = half
                    if legacy_rate == 2.0:
                        data["key_mix_rate_seconds"] = 1.0
                return data
        if os.path.exists(OBS_CONFIG_FILE):
            with open(OBS_CONFIG_FILE, "r", encoding="utf-8") as f:
                legacy = json.load(f)
            data = _migrate_legacy_obs_config(legacy if isinstance(legacy, dict) else {})
            _save_switcher_config(data)
            return data
    except Exception:
        pass
    data = _default_switcher_config()
    _save_switcher_config(data)
    return data


def _save_switcher_config(data):
    try:
        with open(SWITCHER_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _sdi_label(source_id: int) -> str:
    if source_id == SOURCE_IN1:
        return "SDI 1"
    if source_id == SOURCE_IN2:
        return "SDI 2"
    return f"Input {source_id}"


# ---------------- Camera stream URLs (RTSP from camera IP) ----------------
CAMERA_RTSP_PATH = "/live/av0"  # e.g. rtsp://192.168.2.91:554/live/av0


def _discover_stream_urls_from_ip(ip: str) -> dict:
    """Build preview source URLs from camera IP; try HTTP APIs when available."""
    ip = (ip or "").strip()
    rtmp = f"rtmp://{ip}:1935/live"
    rtsp = f"rtsp://{ip}:554{CAMERA_RTSP_PATH}"
    if not ip:
        return {"rtmp": "", "rtsp": "", "source": ""}
    api_paths = (
        "/api/v2/streaming",
        "/api/v2/stream-info",
        "/api/stream",
    )
    for path in api_paths:
        try:
            r = requests.get(f"http://{ip}{path}", timeout=1.2, verify=False)
            if not r.ok:
                continue
            data = r.json()
            if not isinstance(data, dict):
                continue
            for key in ("rtmp", "rtmpUrl", "rtmp_url", "RTMP"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    rtmp = val.strip()
                    break
            for key in ("rtsp", "rtspUrl", "rtsp_url", "RTSP"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    rtsp = val.strip()
                    break
            enc = data.get("encode") or data.get("streaming")
            if isinstance(enc, dict):
                for key in ("rtmp", "rtmpUrl", "url"):
                    val = enc.get(key)
                    if isinstance(val, str) and "rtmp" in val.lower():
                        rtmp = val.strip()
                        break
                for key in ("rtsp", "rtspUrl"):
                    val = enc.get(key)
                    if isinstance(val, str) and "rtsp" in val.lower():
                        rtsp = val.strip()
                        break
        except Exception:
            continue
    source = rtsp or rtmp
    return {"rtmp": rtmp, "rtsp": rtsp, "source": source}


def _camera_preview_source(cam_key: str) -> str:
    urls = _discover_stream_urls_from_ip(_get_camera_ip(cam_key))
    return urls.get("source") or ""


def _preview_sources_for_all() -> dict:
    return {cam: _camera_preview_source(cam) for cam in CAMERAS.keys()}


def _refresh_preview_server():
    sources = _preview_sources_for_all()
    start_preview_server(sources, PREVIEW_PORT)
    return sources


# ---------------- Window Config (position/size) ----------------
def _load_window_config():
    try:
        if os.path.exists(WINDOW_CONFIG_FILE):
            with open(WINDOW_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "x" in data and "y" in data and "width" in data and "height" in data:
                return data
    except Exception:
        pass
    return None


def _save_window_config(x, y, width, height):
    try:
        data = {"x": int(x), "y": int(y), "width": int(width), "height": int(height)}
        with open(WINDOW_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ---------------- Version (shown in Settings) ----------------
def _load_version():
    try:
        if os.path.exists(VERSION_FILE):
            with open(VERSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("version"):
                return str(data["version"]).strip() or DEFAULT_VERSION
    except Exception:
        pass
    return DEFAULT_VERSION


# Switcher reconnect: mission-critical — always retry while host is configured
SWITCHER_RECONNECT_MIN_S = 1.0
SWITCHER_RECONNECT_MAX_S = 5.0
SWITCHER_POLL_FAIL_DISCONNECT = 3
GSP_RECV_STALE_S = 4.0
# Re-check upstream key 1 (Luma, HDMI 3 fill, HDMI 4 key) on this interval
USK_VERIFY_INTERVAL_S = 8.0
GOD_BLESS_STILL_INDEX = 5
TITLE_STILL_INDEX = 1

# ---------------- GoStream physical switcher manager ----------------
# Switcher commands run on the API thread (v3.22 model). A background worker queue
# (v3.23–3.27) serialized CUT/lyrics and fought the poll/connect locks.
class SwitcherManager:
    """OSEE GoStream Duet (and compatible) over GSP TCP protocol."""

    def __init__(self):
        self._config = _load_switcher_config()
        self._client = GoStreamClient()
        self._connected = False
        self._status_cache = {
            "connected": False,
            "deviceName": "GoStream",
            "pgmInput": None,
            "pvwInput": None,
            "cams": {
                "cam1": {"program": False, "preview": False},
                "cam2": {"program": False, "preview": False},
            },
            "streamLive": False,
            "usk1On": False,
            "dsk1On": False,
            "stream1Key": "",
            "stream2Live": False,
            "splitviewOn": False,
            "fullScreenSlideOn": False,
            "mp1StillIndex": None,
        }
        self._lock = threading.Lock()
        self._conn_lock = threading.RLock()
        self._poll_thread = None
        self._stop_polling = False
        self._last_reconnect_attempt = 0.0
        self._reconnect_backoff = SWITCHER_RECONNECT_MIN_S
        self._poll_failures = 0
        self._last_live_cam = "cam1"
        self._last_usk_verify = 0.0
        self._god_bless_active = False
        self._god_bless_return_still_index: int | None = None
        self._test_stream_active = False
        self._test_stream_owns_live = False
        self._test_stream_saved_stream1_enable: bool | None = None
        self._connect_setup_done = False
        self._connect_setup_at = 0.0
        self._ensure_polling()
        # v3.22: block until first TCP connect attempt finishes (do not rely on poll alone)
        self._try_connect()

    def _host_configured(self) -> bool:
        return bool((self._config.get("host") or "").strip())

    def _ensure_polling(self):
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._stop_polling = False
        self._poll_thread = threading.Thread(target=self._poll_status, daemon=True)
        self._poll_thread.start()

    def _mark_disconnected(self) -> None:
        app_log.switcher("Disconnected from switcher")
        with self._lock:
            self._connected = False
            self._status_cache["connected"] = False
            self._connect_setup_done = False
        with self._conn_lock:
            self._client.disconnect()

    def _kick_reconnect(self) -> None:
        """Ask the poll thread to reconnect soon (non-blocking)."""
        self._last_reconnect_attempt = 0.0
        self._reconnect_backoff = SWITCHER_RECONNECT_MIN_S

    def _schedule_reconnect(self) -> None:
        if not self._host_configured():
            return
        now = time.monotonic()
        if now - self._last_reconnect_attempt < self._reconnect_backoff:
            return
        self._last_reconnect_attempt = now
        if self._try_connect():
            self._reconnect_backoff = SWITCHER_RECONNECT_MIN_S
            self._poll_failures = 0
        else:
            self._reconnect_backoff = min(
                SWITCHER_RECONNECT_MAX_S,
                self._reconnect_backoff * 1.5,
            )

    def _sdi_for_cam(self, cam_key: str) -> int:
        sdi = self._config.get("sdi_inputs", {})
        try:
            return int(sdi.get(cam_key, SOURCE_IN1 if cam_key == "cam1" else SOURCE_IN2))
        except Exception:
            return SOURCE_IN1 if cam_key == "cam1" else SOURCE_IN2

    def _refresh_status_cache(self):
        pgm = self._client.pgm_src
        pvw = self._client.pvw_src
        splitview_on = pgm == SOURCE_MULTI
        splitview_live_src = None
        if splitview_on:
            splitview_live_src = self._client.multisource_window2_src
            if splitview_live_src is None:
                splitview_live_src = self._sdi_for_cam(self._last_live_cam or "cam1")
        cam_status = {}
        for cam_key in ("cam1", "cam2"):
            sid = self._sdi_for_cam(cam_key)
            if splitview_on:
                cam_status[cam_key] = {
                    "program": splitview_live_src is not None and sid == splitview_live_src,
                    "preview": pvw is not None and pvw == sid and sid != splitview_live_src,
                }
            else:
                cam_status[cam_key] = {
                    "program": pgm is not None and pgm == sid,
                    "preview": pvw is not None and pvw == sid,
                }
        with self._lock:
            self._status_cache.update({
                "connected": self._connected and self._client.connected,
                "pgmInput": pgm,
                "pvwInput": pvw,
                "cams": cam_status,
                "streamLive": self._client.stream_live_on,
                "usk1On": self._client.usk1_on,
                "dsk1On": self._client.dsk1_on,
                "stream1Key": self._client.stream1_rtmp_key or self._config.get("stream1_key", ""),
                "stream2Live": self._client.stream2_output_live,
                "splitviewOn": self._client.pgm_src == SOURCE_MULTI,
                "fullScreenSlideOn": (
                    self._client.pgm_src == SOURCE_IN3
                    and self._client.pgm_src != SOURCE_MULTI
                ),
                "mp1StillIndex": self._client.mp1_still_index,
            })

    def _try_connect(self) -> bool:
        if not self._host_configured():
            return False
        host = (self._config.get("host") or "").strip()
        try:
            port = int(self._config.get("port", GOSTREAM_DEFAULT_PORT))
        except (TypeError, ValueError):
            return False
        with self._conn_lock:
            if self._connected and self._client.connected:
                return True
            try:
                with self._lock:
                    self._connected = False
                    self._status_cache["connected"] = False
                self._client.disconnect()
                self._client.connect(host, port, timeout=2.0)
                with self._lock:
                    self._connected = True
                    self._status_cache["connected"] = True
            except (ConnectionRefusedError, OSError, socket.timeout, Exception) as e:
                app_log.switcher(
                    "Connect failed",
                    {"host": host, "error": str(e)},
                )
                with self._lock:
                    self._connected = False
                    self._status_cache["connected"] = False
                self._client.disconnect()
                return False
        self._refresh_status_cache()
        # v3.22: initial bus setup on the connect path (not via command worker)
        try:
            self.configure_multisource_layout()
        except Exception:
            pass
        try:
            self.apply_usk1_defaults()
        except Exception:
            pass
        try:
            self.apply_dsk1_defaults()
        except Exception:
            pass
        with self._lock:
            self._connect_setup_done = True
            self._connect_setup_at = time.monotonic()
        app_log.switcher("Connected", {"host": host, "port": port})
        return True

    def startup_setup_is_fresh(self) -> bool:
        """True when connect path already ran USK/multisource/DSK (splash can skip worker)."""
        with self._lock:
            if not self._connect_setup_done:
                return False
            return (time.monotonic() - self._connect_setup_at) < 30.0

    def _reconnect(self):
        self._last_reconnect_attempt = 0.0
        self._reconnect_backoff = SWITCHER_RECONNECT_MIN_S
        self._try_connect()

    def update_config(self, config):
        with self._lock:
            if isinstance(config, dict):
                self._config.update(config)
            base = _default_switcher_config()
            for key, val in base.items():
                if key not in self._config:
                    self._config[key] = val
            if not isinstance(self._config.get("sdi_inputs"), dict):
                self._config["sdi_inputs"] = dict(base["sdi_inputs"])
            # Mission-critical: always keep auto-reconnect enabled
            self._config["auto_reconnect"] = True
            _save_switcher_config(self._config)
        self._ensure_polling()
        self._reconnect()

    def _usk1_target_sources(self) -> tuple[int, int]:
        fill = int(self._config.get("usk1_fill_source", SOURCE_IN3))
        key_src = int(self._config.get("usk1_key_source", SOURCE_IN4))
        return fill, key_src

    def apply_usk1_defaults(self) -> tuple[bool, str]:
        if not self._ensure_connected():
            return False, "Switcher not connected"
        try:
            fill, key_src = self._usk1_target_sources()
            if self._client.configure_usk_luma(USK_1_KEY_ID, fill, key_src):
                return True, "Upstream key configured"
            return False, "Failed to configure upstream key"
        except Exception as e:
            return False, str(e)

    def apply_dsk1_defaults(self) -> tuple[bool, str]:
        if not self._ensure_connected():
            return False, "Switcher not connected"
        try:
            fill = int(self._config.get("dsk1_fill_source", DSK_FILL_STILL_1))
            if self._client.configure_dsk1_still1(DSK_1_KEY_ID, fill, still_index=1):
                return True, "Downstream key set to Still 1"
            return False, "Failed to configure downstream key"
        except Exception as e:
            return False, str(e)

    def run_startup_setup(self) -> dict[str, Any]:
        """Splash: USK, multisource, DSK, stream UI. Skips heavy work if connect already configured."""
        if self.startup_setup_is_fresh():
            stream_ok, stream_st = self.refresh_stream_ui_state()
            return {
                "usk": True,
                "multisource": True,
                "dsk": True,
                "streamui": bool(stream_ok),
                "status": stream_st if stream_ok else {},
            }
        usk_ok, _ = self.apply_usk1_defaults()
        multi_ok, _ = self.configure_multisource_layout()
        dsk_ok, _ = self.apply_dsk1_defaults()
        stream_ok, stream_st = self.refresh_stream_ui_state()
        return {
            "usk": bool(usk_ok),
            "multisource": bool(multi_ok),
            "dsk": bool(dsk_ok),
            "streamui": bool(stream_ok),
            "status": stream_st if stream_ok else {},
        }

    def _verify_usk1_defaults(self) -> None:
        """Poll USK1 state and re-apply Luma / HDMI 3 / HDMI 4 when drifted."""
        if not self._connected or not self._client.connected:
            return
        now = time.monotonic()
        if now - self._last_usk_verify < USK_VERIFY_INTERVAL_S:
            return
        self._last_usk_verify = now
        try:
            self._client.request_usk1_luma_state()
            time.sleep(0.1)
            fill, key_src = self._usk1_target_sources()
            if not self._client.usk1_luma_matches(fill, key_src):
                self.apply_usk1_defaults()
        except Exception:
            pass

    def wait_for_connected(self, timeout_s: float = 3.0) -> bool:
        if self._connected and self._client.connected:
            return True
        self._reconnect()
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        while time.monotonic() < deadline:
            if self._connected and self._client.connected:
                return True
            if not self._host_configured():
                return False
            self._schedule_reconnect()
            time.sleep(0.25)
        return bool(self._connected and self._client.connected)

    def set_simple_mode(self, value: bool):
        with self._lock:
            self._config["simple_mode"] = bool(value)
            _save_switcher_config(self._config)

    def set_ui_prefs(self, prefs: dict):
        if not isinstance(prefs, dict):
            return
        with self._lock:
            if "keyboard_hints_dismissed" in prefs:
                self._config["keyboard_hints_dismissed"] = bool(
                    prefs["keyboard_hints_dismissed"]
                )
            if "settings_last_tab" in prefs:
                tab = prefs["settings_last_tab"]
                if tab in ("switcher", "stream", "general", "midi"):
                    self._config["settings_last_tab"] = tab
            if "update_check_enabled" in prefs:
                self._config["update_check_enabled"] = bool(prefs["update_check_enabled"])
            if "debug_log_enabled" in prefs:
                self._config["debug_log_enabled"] = bool(prefs["debug_log_enabled"])
            if "debug_log_verbose_gostream" in prefs:
                self._config["debug_log_verbose_gostream"] = bool(
                    prefs["debug_log_verbose_gostream"]
                )
            _save_switcher_config(self._config)
        _apply_log_settings_from_config(self._config)

    def _poll_status(self):
        while not self._stop_polling:
            try:
                if not self._host_configured():
                    time.sleep(1.0)
                    continue

                if not self._connected or not self._client.connected:
                    self._schedule_reconnect()
                    time.sleep(0.5)
                    continue

                ok_pgm = self._client.send_get("pgmIndex")
                ok_pvw = self._client.send_get("pvwIndex")
                ok_live = self._client.send_get("live")
                self._client.send_get(
                    "liveStreamOutputEnable", [STREAM_1_ID]
                )
                self._client.send_get(
                    "liveStreamOutputEnable", [STREAM_2_ID]
                )
                self._client.send_get(
                    "liveStreamOutputStatus", [STREAM_1_ID]
                )
                self._client.send_get(
                    "liveStreamOutputStatus", [STREAM_2_ID]
                )
                self._client.request_stream_ui_state()
                if (
                    self._client.pgm_src == SOURCE_MULTI
                    or self._client.multisource_enabled
                ):
                    self._client.send_get(
                        "multiSourceWindowSource", [MULTISOURCE_WINDOW_1_ID]
                    )
                    self._client.send_get(
                        "multiSourceWindowSource", [MULTISOURCE_WINDOW_2_ID]
                    )
                # v3.22 health: failed sends only (v3.23 stale-recv disconnect broke connect)
                if not ok_pgm or not ok_pvw or not ok_live:
                    self._poll_failures += 1
                    if self._poll_failures >= SWITCHER_POLL_FAIL_DISCONNECT:
                        self._mark_disconnected()
                        self._poll_failures = 0
                else:
                    self._poll_failures = 0
                    time.sleep(0.06)
                    self._refresh_status_cache()
                    self._verify_usk1_defaults()
            except Exception as e:
                try:
                    app_log.switcher("Poll error", {"error": str(e)})
                except Exception:
                    pass
                self._mark_disconnected()
                self._poll_failures = 0
            time.sleep(0.25)

    def get_status(self):
        with self._lock:
            if self._connected and not self._client.connected:
                self._connected = False
                self._status_cache["connected"] = False
            return self._status_cache.copy()

    def refresh_stream_ui_state(self) -> tuple[bool, dict]:
        """Poll lyrics/title on-air and still selection; refresh status cache."""
        if not self._ensure_connected():
            return False, {}
        try:
            self._client.request_stream_ui_state()
            time.sleep(0.12)
            self._refresh_status_cache()
            return True, self.get_status()
        except Exception:
            return False, {}

    def _ensure_connected(self) -> bool:
        """Fast check only — reconnect runs on the poll thread."""
        if self._connected and self._client.connected:
            return True
        if self._connected and not self._client.connected:
            with self._lock:
                self._connected = False
                self._status_cache["connected"] = False
        self._kick_reconnect()
        return False

    def _current_mp1_still_index(self) -> int:
        idx = self._client.mp1_still_index
        if idx is not None and 1 <= int(idx) <= 127:
            return int(idx)
        with self._lock:
            cached = self._status_cache.get("mp1StillIndex")
        if cached is not None and 1 <= int(cached) <= 127:
            return int(cached)
        return TITLE_STILL_INDEX

    def _apply_mp1_still(self, still_index: int) -> bool:
        return self._client.activate_still(int(still_index))

    def _restore_from_god_bless(self, adopt_still: int | None = None) -> bool:
        """Leave God Bless on program: restore MP1 still and fade back to camera."""
        if not self._god_bless_active:
            return True
        restore_idx = int(
            adopt_still
            if adopt_still is not None
            else (self._god_bless_return_still_index or TITLE_STILL_INDEX)
        )
        self._god_bless_active = False
        self._god_bless_return_still_index = restore_idx
        if not self._apply_mp1_still(restore_idx):
            return False
        time.sleep(0.08)
        target = self._background_camera_sdi()
        rate = float(self._config.get("camera_mix_rate_seconds", 1.0))
        if not self._client.fade_to_source(target, rate_seconds=rate):
            return False
        time.sleep(max(0.06, rate * 0.12))
        self._client.send_get("pgmIndex")
        self._client.send_get("pvwIndex")
        self._client.request_stream_ui_state()
        self._refresh_status_cache()
        return True

    def exit_god_bless_if_active(self, adopt_still: int | None = None) -> None:
        if self._god_bless_active:
            try:
                self._restore_from_god_bless(adopt_still=adopt_still)
            except Exception:
                self._god_bless_active = False

    def god_bless_screen(self) -> tuple[bool, str]:
        if not self._ensure_connected():
            return False, "Switcher not connected (reconnecting…)"
        try:
            if self._god_bless_active:
                if not self._restore_from_god_bless():
                    return False, "Failed to leave God Bless Screen"
                return True, "God Bless Screen off"
            saved = self._current_mp1_still_index()
            self._god_bless_return_still_index = saved
            self._god_bless_active = True
            if not self._apply_mp1_still(GOD_BLESS_STILL_INDEX):
                self._god_bless_active = False
                return False, "Failed to select God Bless still on media player 1"
            time.sleep(0.08)
            rate = float(self._config.get("camera_mix_rate_seconds", 1.0))
            if not self._client.fade_to_source(SOURCE_MP, rate_seconds=rate):
                self._god_bless_active = False
                self._apply_mp1_still(saved)
                return False, "Failed to fade to God Bless Screen"
            time.sleep(max(0.08, rate * 0.12))
            self._client.send_get("pgmIndex")
            self._client.send_get("pvwIndex")
            self._client.request_stream_ui_state()
            self._refresh_status_cache()
            return True, "God Bless Screen on"
        except Exception as e:
            self._god_bless_active = False
            return False, f"God Bless Screen error: {e}"

    def _end_test_stream_session(self, *, restore_stream1_enable: bool) -> None:
        """Stop a test-stream session started by test_stream_start (owns global live)."""
        if not self._test_stream_owns_live:
            return
        self._client.set_live(False)
        time.sleep(0.12)
        self._client.set_stream_output_enable(STREAM_2_ID, False)
        time.sleep(0.06)
        if restore_stream1_enable and self._test_stream_saved_stream1_enable:
            self._client.set_stream_output_enable(STREAM_1_ID, True)
            time.sleep(0.06)
        self._test_stream_active = False
        self._test_stream_owns_live = False
        self._test_stream_saved_stream1_enable = None

    def stream_go_live(self):
        if not self._ensure_connected():
            return False, "Switcher not connected (reconnecting…)"
        try:
            if self._test_stream_owns_live:
                saved = self._test_stream_saved_stream1_enable
                self._end_test_stream_session(restore_stream1_enable=False)
                time.sleep(0.1)
                if saved:
                    self._client.set_stream_output_enable(STREAM_1_ID, True)
                    time.sleep(0.08)
            elif self._test_stream_active:
                self._client.set_stream_output_enable(STREAM_2_ID, False)
                time.sleep(0.06)
                self._test_stream_active = False
            if not self._client.set_live(True):
                return False, "Failed to start live stream"
            time.sleep(0.2)
            self._client.send_get("live")
            self._client.send_get("liveStreamOutputStatus", [STREAM_1_ID])
            time.sleep(0.08)
            self._refresh_status_cache()
            return True, "Go Live"
        except Exception as e:
            return False, f"Go Live error: {e}"

    def stream_stop(self):
        if not self._ensure_connected():
            return False, "Switcher not connected (reconnecting…)"
        try:
            if self._test_stream_owns_live:
                self._end_test_stream_session(restore_stream1_enable=True)
            else:
                if not self._client.set_live(False):
                    return False, "Failed to stop live stream"
                if self._test_stream_active:
                    self._client.set_stream_output_enable(STREAM_2_ID, False)
                    self._test_stream_active = False
            time.sleep(0.2)
            self._client.send_get("live")
            self._client.send_get("liveStreamOutputStatus", [STREAM_1_ID])
            self._client.send_get("liveStreamOutputStatus", [STREAM_2_ID])
            time.sleep(0.08)
            self._refresh_status_cache()
            return True, "Stream stopped"
        except Exception as e:
            return False, f"Stop error: {e}"

    def test_stream_start(self):
        """Start Stream 2 (arms output + starts encoder when main is offline)."""
        if not self._ensure_connected():
            return False, "Switcher not connected (reconnecting…)"
        try:
            if self._test_stream_active and self._client.stream2_output_live:
                return True, "Test stream already live (Stream 2)"

            main_live = self._client.stream_live_on
            if main_live:
                # Main encode session already running — add Stream 2 to it.
                if not self._client.set_stream_output_enable(STREAM_2_ID, True):
                    return False, "Failed to start test stream (Stream 2)"
                self._test_stream_active = True
                self._test_stream_owns_live = False
            else:
                s1_enabled = self._client.stream1_output_enabled
                if s1_enabled is None:
                    self._client.send_get("liveStreamOutputEnable", [STREAM_1_ID])
                    time.sleep(0.12)
                    s1_enabled = self._client.stream1_output_enabled
                self._test_stream_saved_stream1_enable = (
                    True if s1_enabled is None else bool(s1_enabled)
                )
                if self._test_stream_saved_stream1_enable:
                    if not self._client.set_stream_output_enable(STREAM_1_ID, False):
                        return False, "Failed to prepare test stream (Stream 1)"
                    time.sleep(0.08)
                if not self._client.set_stream_output_enable(STREAM_2_ID, True):
                    if self._test_stream_saved_stream1_enable:
                        self._client.set_stream_output_enable(STREAM_1_ID, True)
                    return False, "Failed to start test stream (Stream 2)"
                time.sleep(0.08)
                if not self._client.set_live(True):
                    self._client.set_stream_output_enable(STREAM_2_ID, False)
                    if self._test_stream_saved_stream1_enable:
                        self._client.set_stream_output_enable(STREAM_1_ID, True)
                    self._test_stream_saved_stream1_enable = None
                    return False, "Failed to start encoder for test stream"
                self._test_stream_active = True
                self._test_stream_owns_live = True

            time.sleep(0.35)
            self._client.send_get("live")
            self._client.send_get("liveStreamOutputStatus", [STREAM_2_ID])
            time.sleep(0.1)
            self._refresh_status_cache()
            if not self._client.stream2_output_live:
                return (
                    False,
                    "Stream 2 did not go live — check Stream 2 RTMP URL/key on the switcher",
                )
            return True, "Test stream started (Stream 2)"
        except Exception as e:
            self._test_stream_active = False
            self._test_stream_owns_live = False
            self._test_stream_saved_stream1_enable = None
            return False, f"Test stream start error: {e}"

    def test_stream_stop(self):
        """Stop Stream 2 only; restore Stream 1 arm state if we started the encoder."""
        if not self._ensure_connected():
            return False, "Switcher not connected (reconnecting…)"
        try:
            if self._test_stream_owns_live:
                self._end_test_stream_session(restore_stream1_enable=True)
            elif self._client.stream_live_on and self._test_stream_active:
                if not self._client.set_stream_output_enable(STREAM_2_ID, False):
                    return False, "Failed to stop test stream (Stream 2)"
                self._test_stream_active = False
            else:
                if not self._client.set_stream_output_enable(STREAM_2_ID, False):
                    return False, "Failed to stop test stream (Stream 2)"
                self._test_stream_active = False
            time.sleep(0.2)
            self._client.send_get("liveStreamOutputStatus", [STREAM_2_ID])
            time.sleep(0.08)
            self._refresh_status_cache()
            return True, "Test stream stopped (Stream 2)"
        except Exception as e:
            return False, f"Test stream stop error: {e}"

    def usk1_set(self, on: bool):
        if not self._ensure_connected():
            return False, "Switcher not connected (reconnecting…)"
        try:
            if self._god_bless_active:
                self.exit_god_bless_if_active()
                time.sleep(0.12)
            use_fade = bool(self._config.get("keys_use_mix_fade", True))
            rate = float(self._config.get("key_mix_rate_seconds", 1.0))
            if use_fade:
                ok = self._client.set_key_on_air_faded(
                    USK_1_KEY_ID, on, dsk=False, rate_seconds=rate
                )
            else:
                ok = self._client.set_usk_on_air(USK_1_KEY_ID, on)
            if not ok:
                return False, "Failed to set upstream key 1"
            time.sleep(0.1)
            self._client.send_get("keyOnAir")
            self._refresh_status_cache()
            return True, "Lyrics " + ("On" if on else "Off")
        except Exception as e:
            return False, f"Lyrics error: {e}"

    def dsk1_set(self, on: bool):
        if not self._ensure_connected():
            return False, "Switcher not connected (reconnecting…)"
        try:
            if self._god_bless_active:
                adopt = TITLE_STILL_INDEX if on else None
                if not self._restore_from_god_bless(adopt_still=adopt):
                    return False, "Failed to leave God Bless Screen"
                time.sleep(0.12)
            use_fade = bool(self._config.get("keys_use_mix_fade", True))
            rate = float(self._config.get("key_mix_rate_seconds", 1.0))
            if use_fade:
                ok = self._client.set_key_on_air_faded(
                    DSK_1_KEY_ID, on, dsk=True, rate_seconds=rate
                )
            else:
                ok = self._client.set_dsk_on_air(DSK_1_KEY_ID, on)
            if not ok:
                return False, "Failed to set downstream key 1"
            time.sleep(0.1)
            self._client.send_get("dskOnAir")
            self._refresh_status_cache()
            return True, "Title " + ("On" if on else "Off")
        except Exception as e:
            return False, f"Title error: {e}"

    def activate_still(self, still_index: int):
        if not self._ensure_connected():
            return False, "Switcher not connected (reconnecting…)"
        try:
            still_index = int(still_index)
            if self._god_bless_active:
                if not self._restore_from_god_bless(adopt_still=still_index):
                    return False, "Failed to leave God Bless Screen"
                return True, f"Still {still_index} activated"
            if not self._apply_mp1_still(still_index):
                return False, f"Failed to activate still {still_index}"
            time.sleep(0.06)
            self._client.request_stream_ui_state()
            self._refresh_status_cache()
            return True, f"Still {still_index} activated"
        except Exception as e:
            return False, f"Still error: {e}"

    def set_stream1_key(self, key_text: str):
        key_text = str(key_text or "")
        with self._lock:
            self._config["stream1_key"] = key_text
            _save_switcher_config(self._config)
        if not self._ensure_connected():
            return True, "Stream key saved locally (switcher not connected)"
        try:
            if not self._client.set_stream_output_key(STREAM_1_ID, key_text):
                return False, "Failed to send stream key to switcher"
            time.sleep(0.1)
            self._client.send_get("liveStreamOutputKey")
            self._refresh_status_cache()
            return True, "Stream key 1 saved"
        except Exception as e:
            return False, f"Stream key error: {e}"

    def get_stream1_key(self):
        with self._lock:
            cached = self._client.stream1_rtmp_key if self._connected else ""
            key = cached or self._config.get("stream1_key", "")
        return True, key

    def _live_camera_sdi(self) -> int:
        with self._lock:
            cams = self._status_cache.get("cams", {})
            if cams.get("cam1", {}).get("program"):
                return self._sdi_for_cam("cam1")
            if cams.get("cam2", {}).get("program"):
                return self._sdi_for_cam("cam2")
            return self._sdi_for_cam(self._last_live_cam or "cam1")

    def configure_multisource_layout(self, cam_key: str | None = None):
        if not self._ensure_connected():
            return False, "Switcher not connected (reconnecting…)"
        cam = cam_key or self._last_live_cam or "cam1"
        window2 = self._sdi_for_cam(cam)
        try:
            if not self._client.configure_multisource_layout(window2):
                return False, "Failed to configure multisource layout"
            time.sleep(0.1)
            self._client.send_get(
                "multiSourceWindowSource", [MULTISOURCE_WINDOW_1_ID]
            )
            self._client.send_get(
                "multiSourceWindowSource", [MULTISOURCE_WINDOW_2_ID]
            )
            return True, "Multisource layout configured"
        except Exception as e:
            return False, f"Multisource layout error: {e}"

    def splitview_on(self):
        if not self._ensure_connected():
            return False, "Switcher not connected (reconnecting…)"
        try:
            self.exit_god_bless_if_active()
            window2 = self._sdi_for_cam(self._last_live_cam or "cam1")
            sv_rate = float(self._config.get("splitview_mix_rate_seconds", 1.0))
            if not self._client.splitview_on(window2, rate_seconds=sv_rate):
                return False, "Failed to fade to Multisource"
            time.sleep(max(0.08, sv_rate * 0.12))
            self._client.send_get("pgmIndex")
            self._client.send_get("pvwIndex")
            self._client.send_get(
                "multiSourceWindowSource", [MULTISOURCE_WINDOW_1_ID]
            )
            self._client.send_get(
                "multiSourceWindowSource", [MULTISOURCE_WINDOW_2_ID]
            )
            time.sleep(0.06)
            self._refresh_status_cache()
            return True, "Splitview On"
        except Exception as e:
            return False, f"Splitview On error: {e}"

    def _background_camera_sdi(self) -> int:
        """SDI input for the camera layer when leaving splitview (not Multisource on PGM)."""
        with self._lock:
            if self._client.pgm_src == SOURCE_MULTI:
                return self._sdi_for_cam(self._last_live_cam or "cam1")
            return self._live_camera_sdi()

    def splitview_off(self):
        if not self._ensure_connected():
            return False, "Switcher not connected (reconnecting…)"
        try:
            self.exit_god_bless_if_active()
            target = self._background_camera_sdi()
            sv_rate = float(self._config.get("splitview_mix_rate_seconds", 1.0))
            if not self._client.splitview_off(target, rate_seconds=sv_rate):
                return False, "Failed to fade back to camera"
            time.sleep(max(0.08, sv_rate * 0.12))
            self._client.send_get("pgmIndex")
            self._client.send_get("pvwIndex")
            self._refresh_status_cache()
            return True, "Splitview Off"
        except Exception as e:
            return False, f"Splitview Off error: {e}"

    def _full_screen_slide_source(self) -> int:
        return int(self._config.get("full_screen_slide_source", SOURCE_IN3))

    def full_screen_slide_on(self) -> tuple[bool, str]:
        """AUTO mix to Input 3 (slide); splitview off; neither camera on program."""
        if not self._ensure_connected():
            return False, "Switcher not connected (reconnecting…)"
        slide_src = self._full_screen_slide_source()
        auto_rate = float(self._config.get("camera_mix_rate_seconds", 1.0))
        sv_rate = float(self._config.get("splitview_mix_rate_seconds", 1.0))
        try:
            self.exit_god_bless_if_active()
            pgm = self._client.pgm_src
            if pgm == slide_src and pgm != SOURCE_MULTI:
                return True, "Full Screen Slide already on (Input 3)"
            if pgm == SOURCE_MULTI or self._client.multisource_enabled:
                if not self._client.splitview_off(slide_src, rate_seconds=sv_rate):
                    return False, "Failed to turn off splitview for Full Screen Slide"
                time.sleep(max(0.08, sv_rate * 0.12))
            elif pgm != slide_src:
                if not self._client.fade_to_source(slide_src, rate_seconds=auto_rate):
                    return False, "Failed AUTO transition to Full Screen Slide (Input 3)"
                time.sleep(max(0.08, auto_rate * 0.12))
            self._client.send_get("pgmIndex")
            self._client.send_get("pvwIndex")
            self._refresh_status_cache()
            return True, "Full Screen Slide on (Input 3)"
        except Exception as e:
            return False, f"Full Screen Slide error: {e}"

    def full_screen_slide_off(self) -> tuple[bool, str]:
        """AUTO back to the last live camera."""
        if not self._ensure_connected():
            return False, "Switcher not connected (reconnecting…)"
        slide_src = self._full_screen_slide_source()
        auto_rate = float(self._config.get("camera_mix_rate_seconds", 1.0))
        try:
            self.exit_god_bless_if_active()
            pgm = self._client.pgm_src
            if pgm != slide_src or pgm == SOURCE_MULTI:
                return True, "Full Screen Slide already off"
            cam = self._last_live_cam or "cam1"
            target = self._sdi_for_cam(cam)
            if not self._client.fade_to_source(target, rate_seconds=auto_rate):
                return False, "Failed AUTO transition from Full Screen Slide"
            time.sleep(max(0.08, auto_rate * 0.12))
            self._client.send_get("pgmIndex")
            self._client.send_get("pvwIndex")
            self._refresh_status_cache()
            return True, "Full Screen Slide off"
        except Exception as e:
            return False, f"Full Screen Slide error: {e}"

    def full_screen_slide(self) -> tuple[bool, str]:
        """UI button behavior: only go to slide (no toggle-off on repeat press)."""
        return self.full_screen_slide_on()

    def cut_camera(self, cam_key):
        if not self._ensure_connected():
            return False, "Switcher not connected (reconnecting…)"

        source_id = self._sdi_for_cam(cam_key)
        try:
            self.exit_god_bless_if_active()
            self._last_live_cam = cam_key
            ok_layout, _ = self.configure_multisource_layout(cam_key)
            if not ok_layout:
                return False, "Failed to update multisource layout"
            if self._client.pgm_src == SOURCE_MULTI:
                time.sleep(0.08)
                self._client.send_get(
                    "multiSourceWindowSource", [MULTISOURCE_WINDOW_1_ID]
                )
                self._client.send_get(
                    "multiSourceWindowSource", [MULTISOURCE_WINDOW_2_ID]
                )
            else:
                if not self._client.cut_to_input(source_id):
                    return False, "Failed to send CUT to switcher"
                time.sleep(0.12)
                self._client.send_get("pgmIndex")
                self._client.send_get("pvwIndex")
            self._refresh_status_cache()
            return True, "Cut successful"
        except Exception as e:
            return False, f"Cut error: {e}"

    def fade_camera(self, cam_key):
        if not self._ensure_connected():
            return False, "Switcher not connected (reconnecting…)"

        source_id = self._sdi_for_cam(cam_key)
        rate = float(self._config.get("camera_mix_rate_seconds", 1.0))
        try:
            self.exit_god_bless_if_active()
            self._last_live_cam = cam_key
            ok_layout, _ = self.configure_multisource_layout(cam_key)
            if not ok_layout:
                return False, "Failed to update multisource layout"
            if not self._client.fade_to_source(source_id, rate_seconds=rate):
                return False, "Failed to fade to camera"
            time.sleep(max(0.08, rate * 0.12))
            self._client.send_get("pgmIndex")
            self._client.send_get("pvwIndex")
            if self._client.pgm_src == SOURCE_MULTI:
                self._client.send_get(
                    "multiSourceWindowSource", [MULTISOURCE_WINDOW_1_ID]
                )
                self._client.send_get(
                    "multiSourceWindowSource", [MULTISOURCE_WINDOW_2_ID]
                )
            self._refresh_status_cache()
            return True, "Fade successful"
        except Exception as e:
            return False, f"Fade error: {e}"

    def shutdown(self):
        self._stop_polling = True
        if self._poll_thread:
            self._poll_thread.join(timeout=1.0)
            self._poll_thread = None
        with self._conn_lock:
            self._client.disconnect()


_switcher_manager = None


def _get_switcher_manager():
    global _switcher_manager
    if _switcher_manager is None:
        _switcher_manager = SwitcherManager()
    return _switcher_manager


_midi_manager = None
_midi_ui_event_lock = threading.Lock()
_midi_ui_event: dict[str, Any] | None = None


def _save_midi_config_section(midi_cfg: dict) -> None:
    """Persist MIDI mappings only — MidiManager already holds the in-memory state."""
    data = _load_switcher_config()
    data["midi"] = normalize_midi_config(midi_cfg)
    _save_switcher_config(data)


def _notify_midi_propresenter_ui(cue_id: str) -> None:
    """Queue cue for UI thread to consume on next poll."""
    global _midi_ui_event
    with _midi_ui_event_lock:
        _midi_ui_event = {
            "cue": str(cue_id),
            "at": time.time(),
        }


def _pop_midi_ui_event() -> dict[str, Any] | None:
    global _midi_ui_event
    with _midi_ui_event_lock:
        evt = _midi_ui_event
        _midi_ui_event = None
        return evt


def _midi_non_live_cam(sm: "SwitcherManager") -> str:
    """Pick the camera that is currently not on Program."""
    try:
        st = sm.get_status()
        cams = st.get("cams", {}) if isinstance(st, dict) else {}
        cam1_live = bool(cams.get("cam1", {}).get("program"))
        cam2_live = bool(cams.get("cam2", {}).get("program"))
        if cam1_live and not cam2_live:
            return "cam2"
        if cam2_live and not cam1_live:
            return "cam1"
        # Fallback: opposite of the last known live camera.
        return "cam2" if (sm._last_live_cam or "cam1") == "cam1" else "cam1"
    except Exception:
        return "cam2" if (sm._last_live_cam or "cam1") == "cam1" else "cam1"


def _midi_tracking_set(cam: str, on: bool) -> tuple[bool, str]:
    return _post(
        cam,
        "/cgi-bin/param.cgi?postfulltrack",
        {
            "path": "%2Fdata%2Ftrack.conf",
            "common.track": "1" if on else "0",
        },
    )


def _midi_recall_preset(cam: str, preset: int) -> tuple[bool, str]:
    return _visca_send_udp(cam, _visca_preset_recall(int(preset)))


def _midi_trigger_scene(scene_id: str) -> None:
    meta = MIDI_SCENES.get(scene_id, {})
    label = meta.get("label", scene_id)
    app_log.midi("Trigger cue", {"cue": scene_id, "label": label})
    sm = _get_switcher_manager()
    st = sm.get_status() if hasattr(sm, "get_status") else {}
    usk_on = bool(st.get("usk1On")) if isinstance(st, dict) else False
    dsk_on = bool(st.get("dsk1On")) if isinstance(st, dict) else False
    split_on = bool(st.get("splitviewOn")) if isinstance(st, dict) else False
    slide_on = bool(st.get("fullScreenSlideOn")) if isinstance(st, dict) else False

    def worship_transition() -> tuple[bool, str]:
        cam = _midi_non_live_cam(sm)
        _midi_tracking_set(cam, False)
        _midi_recall_preset(cam, 11)
        ok, msg = sm.fade_camera(cam)
        if not ok:
            return ok, msg
        time.sleep(1.0)
        return sm.usk1_set(True)

    def sermon_transition() -> tuple[bool, str]:
        cam = _midi_non_live_cam(sm)
        sm.usk1_set(False)
        _midi_tracking_set(cam, False)
        _midi_recall_preset(cam, 1)
        ok, msg = sm.cut_camera(cam)
        if not ok:
            return ok, msg
        time.sleep(5.0)
        _midi_tracking_set(cam, True)
        return True, "Sermon transition"

    def end_service_transition() -> tuple[bool, str]:
        cam = _midi_non_live_cam(sm)
        sm.usk1_set(False)
        _midi_tracking_set(cam, False)
        _midi_recall_preset(cam, 12)
        return sm.fade_camera(cam)

    handlers: dict[str, Callable[[], Any]] = {
        "lyrics_on": (lambda: (True, "Lyrics already On") if usk_on else sm.usk1_set(True)),
        "lyrics_off": (lambda: (True, "Lyrics already Off") if not usk_on else sm.usk1_set(False)),
        "title_on": (lambda: (True, "Title already On") if dsk_on else sm.dsk1_set(True)),
        "title_off": (lambda: (True, "Title already Off") if not dsk_on else sm.dsk1_set(False)),
        "splitview_on": (lambda: (True, "Splitview already On") if split_on else sm.splitview_on()),
        "splitview_off": (lambda: (True, "Splitview already Off") if not split_on else sm.splitview_off()),
        "cam1_cut": lambda: sm.cut_camera("cam1"),
        "cam2_cut": lambda: sm.cut_camera("cam2"),
        "still_sid": lambda: sm.activate_still(1),
        "still_nate": lambda: sm.activate_still(2),
        "still_cecil": lambda: sm.activate_still(3),
        "full_screen_slide_auto": (lambda: (True, "Full Screen Slide already On") if slide_on else sm.full_screen_slide_on()),
        "worship_transition": worship_transition,
        "sermon_transition": sermon_transition,
        "end_service_transition": end_service_transition,
    }
    handler = handlers.get(scene_id)
    if not handler:
        app_log.midi("Unknown MIDI cue", {"cue": scene_id})
        return
    try:
        handler()
        _notify_midi_propresenter_ui(scene_id)
    except Exception as e:
        app_log.midi("Trigger failed", {"cue": scene_id, "error": str(e)})


def _get_midi_manager() -> MidiManager:
    global _midi_manager
    if _midi_manager is None:
        _midi_manager = MidiManager(
            load_config=_load_switcher_config,
            save_midi_config=_save_midi_config_section,
            trigger_scene=_midi_trigger_scene,
        )
    return _midi_manager


class Api:
    """
    Exposed to JS as window.pywebview.api
    """
    def __init__(self):
        self._state = _load_state()
        global _camera_ips
        ips = self._state.get("camera_ips") or {}
        for cam in CAMERAS.keys():
            _camera_ips[cam] = (ips.get(cam) or "").strip() or CAMERAS[cam]["ip"]

    def log_user_event(self, action: str, target: str = "", detail=None):
        try:
            app_log.user(str(action or "event"), str(target or ""), detail)
            return True, "logged"
        except Exception as e:
            return False, f"log_user_event error: {e}"

    def log_get_info(self):
        try:
            return True, {
                "enabled": app_log.enabled(),
                "verbose_gostream": app_log.verbose_gostream(),
                "log_dir": app_log.log_dir(),
                "session_path": app_log.session_path() or "",
            }
        except Exception as e:
            return False, f"log_get_info error: {e}"

    def open_logs_folder(self):
        try:
            return app_log.open_logs_folder()
        except Exception as e:
            return False, f"open_logs_folder error: {e}"

    # --------- Tracking toggles ---------
    def tracking_on(self, cam: str):
        return _post(cam, "/cgi-bin/param.cgi?postfulltrack", {
            "path": "%2Fdata%2Ftrack.conf",
            "common.track": "1"
        })

    def tracking_off(self, cam: str):
        return _post(cam, "/cgi-bin/param.cgi?postfulltrack", {
            "path": "%2Fdata%2Ftrack.conf",
            "common.track": "0"
        })

    def get_tracking_status(self, cam: str):
        """
        Returns (ok, value) where value is True (tracking on), False (tracking off), or None (unknown).
        Tries GET param.cgi to read common.track from track.conf.
        """
        try:
            ok, resp = _get(cam, "/cgi-bin/param.cgi", params={
                "getfulltrack": "1",
                "path": "/data/track.conf"
            }, timeout=2.0)
            if not ok or resp is None:
                return True, None
            if isinstance(resp, dict):
                v = resp.get("common.track") or resp.get("common", {}).get("track")
                if v is not None:
                    return True, v in (1, "1", True, "on", "true")
            text = str(resp) if not isinstance(resp, str) else resp
            if "common.track=1" in text or "common.track= 1" in text:
                return True, True
            if "common.track=0" in text or "common.track= 0" in text:
                return True, False
            return True, None
        except Exception:
            return True, None

    def _camera_focus_http(self, cam: str, direction: str, speed: int = 5) -> tuple[bool, str]:
        """Manual focus via camera HTTP (Brickcom / PTZOptics-style param.cgi)."""
        spd = max(1, min(7, int(round(speed / 10.0 * 7)) or 1))
        if direction == "focus_in":
            ptz_cmds = [f"FOCUSIN{spd}_mfocus", "FOCUSIN_mfocus", "near"]
            fz_move = "near"
        elif direction == "focus_out":
            ptz_cmds = [f"FOCUSOUT{spd}_mfocus", "FOCUSOUT_mfocus", "far"]
            fz_move = "far"
        else:
            ptz_cmds = ["FOCUSSTOP_mfocus", "stop"]
            fz_move = None

        for cmd in ptz_cmds:
            try:
                ok, _ = _get(
                    cam,
                    "/cgi-bin/param.cgi",
                    params={"ptzcmd": cmd},
                    timeout=HTTP_TIMEOUT,
                )
                if ok:
                    return True, cmd
            except Exception:
                continue

        if fz_move:
            try:
                ok, _ = _get(
                    cam,
                    "/cgi-bin/focuszoom.cgi",
                    params={"FzMove": fz_move},
                    timeout=HTTP_TIMEOUT,
                )
                if ok:
                    return True, f"FzMove={fz_move}"
            except Exception:
                pass
        elif direction == "focus_stop":
            for params in (
                {"FzMove": "stop", "FzTarget": "focus"},
                {"FzMove": "stop"},
            ):
                try:
                    ok, _ = _get(
                        cam,
                        "/cgi-bin/focuszoom.cgi",
                        params=params,
                        timeout=HTTP_TIMEOUT,
                    )
                    if ok:
                        return True, "FzMove=stop"
                except Exception:
                    continue
        return False, "No focus command accepted"

    def focus_manual(self, cam: str, direction: str, speed: int = 5):
        """Manual focus in/out/stop (locks auto focus, then HTTP; VISCA fallback)."""
        try:
            speed = _clamp(speed, 1, 10)
            if direction in ("focus_in", "focus_out"):
                self.focus_off(cam)
            ok_http, detail = self._camera_focus_http(cam, direction, speed)
            if ok_http:
                return True, detail
            if direction in ("focus_in", "focus_out", "focus_stop"):
                return _visca_send_udp(cam, _visca_focus(direction, speed))
            return False, f"Unknown focus direction: {direction}"
        except Exception as e:
            return False, f"focus_manual error: {e}"

    def _focus_mode_http(self, cam: str, manual: bool) -> bool:
        cmd = "LOCK_mfocus" if manual else "UNLOCK_mfocus"
        try:
            ok, resp = _get(
                cam,
                "/cgi-bin/param.cgi",
                params={"ptzcmd": cmd},
                timeout=HTTP_TIMEOUT,
            )
            if not ok or resp is None:
                return False
            text = str(resp).upper()
            if "ERROR" in text or "FAIL" in text or "INVALID" in text:
                return False
            return True
        except Exception:
            return False

    # --------- Focus (auto focus on / off) ---------
    def focus_on(self, cam: str):
        """Auto focus on (UNLOCK). Sony/Brickcom: param.cgi?ptzcmd=UNLOCK_mfocus"""
        try:
            if self._focus_mode_http(cam, manual=False):
                return True, "Focus auto"
            ok, msg = _visca_send_udp(cam, _visca_autofocus(True))
            return (ok, "Focus auto" if ok else msg)
        except Exception as e:
            return False, str(e)

    def focus_off(self, cam: str):
        """Auto focus off / manual (LOCK). Sony/Brickcom: param.cgi?ptzcmd=LOCK_mfocus"""
        try:
            if self._focus_mode_http(cam, manual=True):
                return True, "Focus manual"
            ok, msg = _visca_send_udp(cam, _visca_autofocus(False))
            return (ok, "Focus manual" if ok else msg)
        except Exception as e:
            return False, str(e)

    def get_focus_status(self, cam: str):
        """Returns (ok, value) where value is True (auto), False (manual), or None (unknown)."""
        try:
            ok, resp = _get(cam, "/cgi-bin/param.cgi", params={"inquiry": "mfocus"}, timeout=2.0)
            if not ok or resp is None:
                return True, None
            text = str(resp) if not isinstance(resp, str) else resp
            if isinstance(resp, dict):
                v = resp.get("mfocus") or resp.get("focus")
                if v is not None:
                    vs = str(v).strip().upper()
                    if vs in ("UNLOCK", "AUTO", "1", "ON", "TRUE"):
                        return True, True
                    if vs in ("LOCK", "MANUAL", "0", "OFF", "FALSE"):
                        return True, False
            upper = text.upper()
            m = re.search(r"MFOCUS\s*[=:]\s*(\w+)", upper)
            if m:
                val = m.group(1)
                if val in ("UNLOCK", "AUTO", "1", "ON"):
                    return True, True
                if val in ("LOCK", "MANUAL", "0", "OFF"):
                    return True, False
            if "UNLOCK" in upper or re.search(r"\bAUTO\b", upper):
                return True, True
            if re.search(r"\bLOCK\b", upper) or re.search(r"\bMANUAL\b", upper):
                return True, False
            return True, None
        except Exception:
            return True, None

    # --------- Online check (for dot indicator) ---------
    def camera_ping(self, cam: str):
        """
        Fast liveness check over HTTP (doesn't depend on VISCA).
        Returns (ok, details)
        """
        try:
            _ok, _resp = _get(cam, "/", timeout=1.0)
            return True, "online"
        except Exception as e:
            return False, str(e)

    # --------- Presets (VISCA UDP) ---------
    def preset_recall(self, cam: str, preset: int):
        try:
            payload = _visca_preset_recall(int(preset))
            return _visca_send_udp(cam, payload)
        except Exception as e:
            return False, f"preset_recall error: {e}"

    def preset_save(self, cam: str, preset: int):
        try:
            payload = _visca_preset_set(int(preset))
            return _visca_send_udp(cam, payload)
        except Exception as e:
            return False, f"preset_save error: {e}"

    # --------- Preset count (2,4,6,8,10,12) ---------
    def get_preset_count(self):
        try:
            return True, _normalize_preset_count(self._state)
        except Exception as e:
            return False, f"get_preset_count error: {e}"

    def set_preset_count(self, n: int):
        try:
            n = int(n)
            if n not in VALID_PRESET_COUNTS:
                return False, f"Preset count must be one of {VALID_PRESET_COUNTS}"
            self._state["preset_count"] = n
            for cam in CAMERAS.keys():
                labels = self._state.get("labels", {}).get(cam, {})
                base = {str(i): f"Preset {i}" for i in range(1, n + 1)}
                for i in range(1, n + 1):
                    k = str(i)
                    base[k] = labels.get(k, base[k])
                if "labels" not in self._state:
                    self._state["labels"] = {}
                self._state["labels"][cam] = base
            _save_state(self._state)
            return True, n
        except Exception as e:
            return False, f"set_preset_count error: {e}"

    # --------- Preset labels (stored next to EXE) ---------
    def get_preset_labels(self, cam: str):
        try:
            preset_count = _normalize_preset_count(self._state)
            labels = self._state.get("labels", {})
            if cam not in labels:
                labels[cam] = {str(i): f"Preset {i}" for i in range(1, preset_count + 1)}
                self._state["labels"] = labels
                _save_state(self._state)
            cam_labels = labels[cam]
            out = {str(i): cam_labels.get(str(i), f"Preset {i}") for i in range(1, preset_count + 1)}
            return True, out
        except Exception as e:
            return False, f"get_preset_labels error: {e}"

    def set_preset_label(self, cam: str, preset: int, label: str):
        try:
            preset = int(preset)
            label = (label or "").strip()
            if not label:
                label = f"Preset {preset}"

            if preset in SIMPLE_PRESETS:
                sl = self._state.get("simple_labels", {})
                if cam not in sl or not isinstance(sl.get(cam), dict):
                    sl = dict(sl)
                    sl[cam] = {str(i): f"Preset {i}" for i in SIMPLE_PRESETS}
                sl[cam][str(preset)] = label
                self._state["simple_labels"] = sl
                _save_state(self._state)
                return True, {str(i): sl[cam].get(str(i), f"Preset {i}") for i in SIMPLE_PRESETS}

            preset_count = _normalize_preset_count(self._state)
            if preset < 1 or preset > preset_count:
                return False, "Preset out of range"

            labels = self._state.get("labels", {})
            if cam not in labels:
                labels[cam] = {str(i): f"Preset {i}" for i in range(1, preset_count + 1)}
            labels[cam][str(preset)] = label
            self._state["labels"] = labels
            _save_state(self._state)
            out = {str(i): labels[cam].get(str(i), f"Preset {i}") for i in range(1, preset_count + 1)}
            return True, out
        except Exception as e:
            return False, f"set_preset_label error: {e}"

    def get_simple_preset_labels(self, cam: str):
        try:
            sl = self._state.get("simple_labels", {}) or {}
            if cam not in sl or not isinstance(sl.get(cam), dict):
                sl = dict(sl)
                sl[cam] = {str(i): f"Preset {i}" for i in SIMPLE_PRESETS}
                self._state["simple_labels"] = sl
                _save_state(self._state)
            out = {str(i): sl[cam].get(str(i), f"Preset {i}") for i in SIMPLE_PRESETS}
            return True, out
        except Exception as e:
            return False, f"get_simple_preset_labels error: {e}"

    # --------- Camera display titles (for panel header and OBS Settings labels) ---------
    def get_camera_titles(self):
        try:
            titles = self._state.get("camera_titles", {})
            if not titles:
                titles = _default_state()["camera_titles"]
                self._state["camera_titles"] = titles
                _save_state(self._state)
            out = {}
            for cam in CAMERAS.keys():
                out[cam] = (titles.get(cam) or "").strip()
            return True, out
        except Exception as e:
            return False, f"get_camera_titles error: {e}"

    def set_camera_title(self, cam: str, title: str):
        try:
            if cam not in CAMERAS:
                return False, "Unknown camera"
            titles = self._state.get("camera_titles", {})
            if not titles:
                titles = _default_state()["camera_titles"]
            titles[cam] = (title or "").strip()
            self._state["camera_titles"] = titles
            _save_state(self._state)
            return True, titles
        except Exception as e:
            return False, f"set_camera_title error: {e}"

    # --------- Speed persistence (0-100 slider) ---------
    def get_speed_pct(self, cam: str):
        try:
            sp = self._state.get("speed_pct", {})
            if cam not in sp:
                sp[cam] = DEFAULT_SPEED_PCT
                self._state["speed_pct"] = sp
                _save_state(self._state)
            return True, int(sp[cam])
        except Exception as e:
            return False, f"get_speed_pct error: {e}"

    def set_speed_pct(self, cam: str, pct: int):
        try:
            pct = int(pct)
            pct = max(0, min(100, pct))
            sp = self._state.get("speed_pct", {})
            sp[cam] = pct
            self._state["speed_pct"] = sp
            _save_state(self._state)
            return True, pct
        except Exception as e:
            return False, f"set_speed_pct error: {e}"

    # --------- PTZ Move (VISCA UDP) ---------
    def ptz_move(self, cam: str, direction: str, speed: int = 5):
        """
        direction: up/down/left/right/stop/zoom_in/zoom_out/zoom_stop/focus_in/focus_out
        stop stops pan/tilt, zoom, and focus
        """
        try:
            speed = _clamp(speed, 1, 10)

            if direction == "stop":
                ok1, r1 = _visca_send_udp(cam, _visca_ptz_drive("stop", speed))
                ok2, r2 = _visca_send_udp(cam, _visca_zoom("zoom_stop", speed))
                ok3, r3 = self.focus_manual(cam, "focus_stop", speed)
                ok = ok1 and ok2 and ok3[0]
                return ok, ("stopped" if ok else f"{r1} | {r2} | {r3[1]}")

            if direction in ("up", "down", "left", "right"):
                return _visca_send_udp(cam, _visca_ptz_drive(direction, speed))

            if direction in ("zoom_in", "zoom_out", "zoom_stop"):
                return _visca_send_udp(cam, _visca_zoom(direction, speed))

            if direction in ("focus_in", "focus_out", "focus_stop"):
                return self.focus_manual(cam, direction, speed)

            return False, f"Unknown direction: {direction}"
        except Exception as e:
            return False, f"ptz_move error: {e}"

    # --------- Version (Settings footer) ---------
    def get_version(self):
        try:
            return True, _load_version()
        except Exception as e:
            return False, str(e)

    # --------- Utility ---------
    def get_camera_ips(self):
        ips = self._state.get("camera_ips", {})
        out = {}
        for cam in CAMERAS.keys():
            out[cam] = (ips.get(cam) or "").strip() or CAMERAS[cam]["ip"]
        return out

    def set_camera_ip(self, cam: str, ip: str):
        if cam not in CAMERAS:
            return False, "Unknown camera"
        ip = (ip or "").strip() or CAMERAS[cam]["ip"]
        if "camera_ips" not in self._state or not isinstance(self._state["camera_ips"], dict):
            self._state["camera_ips"] = {c: CAMERAS[c]["ip"] for c in CAMERAS.keys()}
        self._state["camera_ips"][cam] = ip
        _save_state(self._state)
        global _camera_ips
        _camera_ips[cam] = ip
        invalidate_preview(cam)
        try:
            _refresh_preview_server()
        except Exception:
            pass
        return True, ip

    # --------- GoStream switcher (physical) ---------
    def get_switcher_config(self):
        try:
            manager = _get_switcher_manager()
            return True, manager._config.copy()
        except Exception as e:
            return False, f"get_switcher_config error: {e}"

    def set_switcher_config(self, config):
        try:
            manager = _get_switcher_manager()
            manager.update_config(config)
            return True, "Switcher settings saved"
        except Exception as e:
            return False, f"set_switcher_config error: {e}"

    def set_switcher_simple_mode(self, value: bool):
        try:
            manager = _get_switcher_manager()
            manager.set_simple_mode(value)
            return True
        except Exception as e:
            return False, str(e)

    def switcher_get_status(self):
        try:
            manager = _get_switcher_manager()
            return True, manager.get_status()
        except Exception as e:
            return False, f"switcher_get_status error: {e}"

    def switcher_get_info(self):
        try:
            manager = _get_switcher_manager()
            sdi = manager._config.get("sdi_inputs", {})
            cam1 = int(sdi.get("cam1", SOURCE_IN1))
            cam2 = int(sdi.get("cam2", SOURCE_IN2))
            st = manager.get_status()
            return True, {
                "host": manager._config.get("host", ""),
                "port": manager._config.get("port", GOSTREAM_DEFAULT_PORT),
                "connected": st.get("connected", False),
                "pgmInput": st.get("pgmInput"),
                "pvwInput": st.get("pvwInput"),
                "cam1Sdi": cam1,
                "cam1SdiLabel": _sdi_label(cam1),
                "cam2Sdi": cam2,
                "cam2SdiLabel": _sdi_label(cam2),
            }
        except Exception as e:
            return False, f"switcher_get_info error: {e}"

    def switcher_cut(self, cam: str):
        try:
            return _get_switcher_manager().cut_camera(cam)
        except Exception as e:
            return False, f"switcher_cut error: {e}"

    def switcher_fade(self, cam: str):
        try:
            return _get_switcher_manager().fade_camera(cam)
        except Exception as e:
            return False, f"switcher_fade error: {e}"

    def switcher_stream_go_live(self):
        try:
            return _get_switcher_manager().stream_go_live()
        except Exception as e:
            return False, f"switcher_stream_go_live error: {e}"

    def switcher_stream_stop(self):
        try:
            return _get_switcher_manager().stream_stop()
        except Exception as e:
            return False, f"switcher_stream_stop error: {e}"

    def switcher_test_stream_start(self):
        try:
            return _get_switcher_manager().test_stream_start()
        except Exception as e:
            return False, f"switcher_test_stream_start error: {e}"

    def switcher_test_stream_stop(self):
        try:
            return _get_switcher_manager().test_stream_stop()
        except Exception as e:
            return False, f"switcher_test_stream_stop error: {e}"

    def switcher_usk1_on(self):
        try:
            return _get_switcher_manager().usk1_set(True)
        except Exception as e:
            return False, f"switcher_usk1_on error: {e}"

    def switcher_usk1_off(self):
        try:
            return _get_switcher_manager().usk1_set(False)
        except Exception as e:
            return False, f"switcher_usk1_off error: {e}"

    def switcher_dsk1_on(self):
        try:
            return _get_switcher_manager().dsk1_set(True)
        except Exception as e:
            return False, f"switcher_dsk1_on error: {e}"

    def switcher_dsk1_off(self):
        try:
            return _get_switcher_manager().dsk1_set(False)
        except Exception as e:
            return False, f"switcher_dsk1_off error: {e}"

    def switcher_get_stream1_key(self):
        try:
            return _get_switcher_manager().get_stream1_key()
        except Exception as e:
            return False, f"switcher_get_stream1_key error: {e}"

    def switcher_set_stream1_key(self, key_text: str):
        try:
            return _get_switcher_manager().set_stream1_key(key_text)
        except Exception as e:
            return False, f"switcher_set_stream1_key error: {e}"

    def switcher_activate_still(self, still_index: int):
        try:
            return _get_switcher_manager().activate_still(still_index)
        except Exception as e:
            return False, f"switcher_activate_still error: {e}"

    def switcher_splitview_on(self):
        try:
            return _get_switcher_manager().splitview_on()
        except Exception as e:
            return False, f"switcher_splitview_on error: {e}"

    def switcher_splitview_off(self):
        try:
            return _get_switcher_manager().splitview_off()
        except Exception as e:
            return False, f"switcher_splitview_off error: {e}"

    def switcher_full_screen_slide(self):
        try:
            return _get_switcher_manager().full_screen_slide()
        except Exception as e:
            return False, f"switcher_full_screen_slide error: {e}"

    def configure_multisource_layout(self):
        try:
            return _get_switcher_manager().configure_multisource_layout()
        except Exception as e:
            return False, f"configure_multisource_layout error: {e}"

    def switcher_refresh_stream_ui(self):
        try:
            return _get_switcher_manager().refresh_stream_ui_state()
        except Exception as e:
            return False, f"switcher_refresh_stream_ui error: {e}"

    # Legacy API names (older UI / scripts)
    def get_obs_config(self):
        return self.get_switcher_config()

    def set_obs_config(self, config):
        return self.set_switcher_config(config)

    def set_obs_simple_mode(self, value: bool):
        return self.set_switcher_simple_mode(value)

    def set_switcher_ui_prefs(self, prefs):
        try:
            _get_switcher_manager().set_ui_prefs(prefs)
            return True, "OK"
        except Exception as e:
            return False, f"set_switcher_ui_prefs error: {e}"

    def set_obs_ui_prefs(self, prefs):
        return self.set_switcher_ui_prefs(prefs)

    def obs_get_status(self):
        return self.switcher_get_status()

    def switcher_startup_setup(self):
        try:
            return True, _get_switcher_manager().run_startup_setup()
        except Exception as e:
            return False, f"switcher_startup_setup error: {e}"

    def apply_usk1_defaults(self):
        try:
            return _get_switcher_manager().apply_usk1_defaults()
        except Exception as e:
            return False, f"apply_usk1_defaults error: {e}"

    def apply_dsk1_defaults(self):
        try:
            return _get_switcher_manager().apply_dsk1_defaults()
        except Exception as e:
            return False, f"apply_dsk1_defaults error: {e}"

    def startup_wait_switcher(self, timeout_s=3.0):
        try:
            ok = _get_switcher_manager().wait_for_connected(float(timeout_s))
            return True, ok
        except Exception as e:
            return False, f"startup_wait_switcher error: {e}"

    def obs_get_scene_sources(self):
        return self.switcher_get_info()


    def get_camera_preview_urls(self):
        try:
            streams = {
                cam: _discover_stream_urls_from_ip(_get_camera_ip(cam))
                for cam in CAMERAS
            }
            sources = {
                cam: (streams[cam].get("source") or "") for cam in CAMERAS
            }
            start_preview_server(sources, PREVIEW_PORT)
            urls = {cam: preview_url(cam, PREVIEW_PORT) for cam in CAMERAS}
            return True, {
                "urls": urls,
                "sources": sources,
                "streams": streams,
                "ffmpeg": ffmpeg_available(),
            }
        except Exception as e:
            return False, f"get_camera_preview_urls error: {e}"

    def refresh_camera_preview(self, cam=""):
        """Restart FFmpeg preview for one camera (or both) and return fresh URLs."""
        try:
            key = str(cam or "").strip()
            if key in CAMERAS:
                invalidate_preview(key)
            else:
                refresh_all_previews()
            streams = {
                c: _discover_stream_urls_from_ip(_get_camera_ip(c)) for c in CAMERAS
            }
            sources = {c: (streams[c].get("source") or "") for c in CAMERAS}
            start_preview_server(sources, PREVIEW_PORT)
            urls = {c: preview_url(c, PREVIEW_PORT) for c in CAMERAS}
            return True, {"urls": urls, "ffmpeg": ffmpeg_available()}
        except Exception as e:
            return False, f"refresh_camera_preview error: {e}"

    def window_minimize(self):
        try:
            if _app_window:
                _app_window.minimize()
            return True, ""
        except Exception as e:
            return False, str(e)

    def window_toggle_fullscreen(self):
        try:
            if _app_window:
                _app_window.toggle_fullscreen()
            return True, bool(getattr(_app_window, "fullscreen", False))
        except Exception as e:
            return False, str(e)

    def window_close(self):
        try:
            _shutdown_app()
            if _app_window:
                _app_window.destroy()
            return True, ""
        except Exception as e:
            return False, str(e)

    def restart_app(self):
        """Relaunch PTZ-Control (used when switcher was off at startup)."""
        try:
            if getattr(sys, "frozen", False):
                exe = running_exe_path(_exe_dir())
                if not launch_restart(exe):
                    return False, "Could not restart PTZ-Control."
            else:
                subprocess.Popen(
                    [sys.executable, os.path.abspath(__file__)],
                    cwd=_exe_dir(),
                    close_fds=True,
                )
            global _app_window
            if _app_window:
                try:
                    _app_window.destroy()
                except Exception:
                    pass
            time.sleep(0.3)
            os._exit(0)
        except Exception as e:
            return False, f"restart_app error: {e}"

    def obs_cut(self, cam: str):
        return self.switcher_cut(cam)

    def obs_fade(self, cam: str):
        return self.switcher_fade(cam)

    def midi_list_devices(self):
        try:
            return True, {
                "available": midi_available(),
                "devices": list_input_devices(),
            }
        except Exception as e:
            return False, f"midi_list_devices error: {e}"

    def midi_get_config(self):
        try:
            return True, _get_midi_manager().get_config()
        except Exception as e:
            return False, f"midi_get_config error: {e}"

    def midi_set_device(self, device_name: str):
        try:
            return _get_midi_manager().set_device(device_name or "")
        except Exception as e:
            return False, f"midi_set_device error: {e}"

    def midi_set_enabled(self, enabled: bool):
        try:
            return _get_midi_manager().set_enabled(bool(enabled))
        except Exception as e:
            return False, f"midi_set_enabled error: {e}"

    def midi_start_learn(self, scene_id: str):
        try:
            return _get_midi_manager().start_learn(str(scene_id))
        except Exception as e:
            return False, f"midi_start_learn error: {e}"

    def midi_cancel_learn(self):
        try:
            return _get_midi_manager().cancel_learn()
        except Exception as e:
            return False, f"midi_cancel_learn error: {e}"

    def midi_clear_note(self, scene_id: str):
        try:
            return _get_midi_manager().clear_note(str(scene_id))
        except Exception as e:
            return False, f"midi_clear_note error: {e}"

    def midi_pop_ui_event(self):
        try:
            return True, _pop_midi_ui_event()
        except Exception as e:
            return False, f"midi_pop_ui_event error: {e}"

  # --------- App update ---------
    def check_for_update(self, force=False):
        try:
            cfg = _load_switcher_config()
            enabled = True if force else bool(cfg.get("update_check_enabled", True))
            url = (cfg.get("update_manifest_url") or DEFAULT_MANIFEST_URL).strip()
            st = check_for_update(_load_version(), url, enabled=enabled)
            return True, st
        except Exception as e:
            return False, f"check_for_update error: {e}"

    def download_and_apply_update(self):
        try:
            cfg = _load_switcher_config()
            url = (cfg.get("update_manifest_url") or DEFAULT_MANIFEST_URL).strip()
            st = check_for_update(_load_version(), url, enabled=True)
            if not st.get("update_available"):
                return False, st.get("error") or "No update available"
            exe_dir = _exe_dir()
            work_dir = get_update_work_dir()
            job = {
                "download_url": st.get("download_url") or "",
                "sha256": st.get("sha256") or "",
                "version": st.get("latest") or "",
                "install_dir": exe_dir,
                "work_dir": work_dir,
            }
            try:
                job_path = write_update_job(job)
            except PermissionError as e:
                return False, str(e)
            ok, msg = launch_gui_updater(exe_dir, job_path)
            if not ok:
                return False, msg
            global _app_window
            if _app_window:
                try:
                    _app_window.destroy()
                except Exception:
                    pass
            time.sleep(1)
            os._exit(0)
        except Exception as e:
            return False, f"download_and_apply_update error: {e}"


def _safe_log_args(args: tuple, kwargs: dict) -> dict:
    return app_log.safe_api_args(args, kwargs)


def _instrument_api_methods() -> None:
    """Wrap pywebview API methods so calls and results are written to the debug log."""
    import functools

    skip = {
        "log_user_event",
        "log_get_info",
        "open_logs_folder",
        "obs_get_status",
        "get_camera_preview_urls",
        "get_speed_pct",
        "camera_ping",
        "get_tracking_status",
        "midi_get_config",
        "switcher_get_info",
    }
    prefixes = (
        "switcher_",
        "obs_",
        "midi_",
        "tracking_",
        "focus_",
        "zoom_",
        "preset_",
        "cut_",
        "save_",
        "set_",
        "get_",
        "apply_",
        "configure_",
        "startup_",
        "download_",
        "check_for_update",
        "manual_check",
        "camera_",
        "stream_",
        "refresh_",
        "load_",
        "recall_",
        "store_",
        "move_",
        "toggle_",
        "activate_",
        "deactivate_",
    )

    for name in list(vars(Api).keys()):
        if name.startswith("_") or name in skip:
            continue
        fn = getattr(Api, name, None)
        if not callable(fn):
            continue
        if not (name in prefixes or any(name.startswith(p) for p in prefixes)):
            continue

        def make_wrapper(method_name: str, impl):
            @functools.wraps(impl)
            def wrapped(*args, **kwargs):
                if app_log.should_log_api_call(method_name, "call"):
                    app_log.api(method_name, "call", _safe_log_args(args, kwargs))
                try:
                    result = impl(*args, **kwargs)
                    if app_log.should_log_api_return(method_name, result):
                        app_log.api(
                            method_name,
                            "return",
                            app_log.summarize_api_result(result),
                        )
                    return result
                except Exception as e:
                    app_log.api(method_name, "error", {"error": str(e)})
                    raise

            return wrapped

        setattr(Api, name, make_wrapper(name, fn))


_shutdown_done = False


def _shutdown_app():
    global _shutdown_done, _switcher_manager, _midi_manager
    if _shutdown_done:
        return
    _shutdown_done = True
    app_log.info("SYSTEM", "Application closing")
    try:
        shutdown_preview_server()
    except Exception:
        stop_all_previews()
    if _midi_manager:
        _midi_manager.shutdown()
        _midi_manager = None
    if _switcher_manager:
        _switcher_manager.shutdown()


def main():
    if not _acquire_single_instance():
        sys.exit(0)

    switcher_cfg = _load_switcher_config()
    _apply_log_settings_from_config(switcher_cfg)
    app_log.start_session()
    app_log.info(
        "SYSTEM",
        "Application starting",
        {"version": _load_version(), "frozen": getattr(sys, "frozen", False)},
    )

    ui_base = _ui_base_dir()
    html_path = os.path.join(ui_base, "ui", "ptz-top-half.html")

    _instrument_api_methods()
    api = Api()
    _get_switcher_manager()
    _get_midi_manager()
    try:
        _refresh_preview_server()
    except Exception:
        pass

    width, height = 1920, 1080
    x, y = 0, 0
    saved_config = _load_window_config()
    if saved_config:
        x = int(saved_config.get("x", 0))
        y = int(saved_config.get("y", 0))

    global _app_window
    _app_window = webview.create_window(
        "PTZ-Control",
        url=html_path,
        width=width,
        height=height,
        x=x,
        y=y,
        resizable=False,
        frameless=True,
        easy_drag=False,
        on_top=True,
        maximized=True,
        js_api=api,
    )

    try:
        _app_window.events.closed += _shutdown_app
    except Exception:
        pass

    try:
        webview.start(gui="edgechromium", http_server=True, debug=False)
    finally:
        _shutdown_app()


if __name__ == "__main__":
    main()
