"""
Check for app updates via a remote update.json manifest.
In-place updates are applied by PTZ-Control-Updater.exe (GUI), not PowerShell.
User JSON config in the exe directory is never modified.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from typing import Any

import requests

DEFAULT_MANIFEST_URL = (
    "https://github.com/Danieldsavn/PTZ-Control/releases/latest/download/update.json"
)
EXE_NAME = "PTZ-Control.exe"
UPDATER_EXE_NAME = "PTZ-Control-Updater.exe"
UPDATE_JOB_NAME = "update_job.json"
DOWNLOAD_SUFFIX = ".download"
BAK_SUFFIX = ".bak"
REQUEST_TIMEOUT = 120
APPLY_LOG_NAME = "apply_update.log"
STAGING_EXE_NAME = "PTZ-Control.exe.download"


def get_update_work_dir() -> str:
    """Writable folder for update job, log, and download staging (not beside Program Files exe)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "PTZ-Control")
    os.makedirs(path, exist_ok=True)
    return path


def update_job_path() -> str:
    return os.path.join(get_update_work_dir(), UPDATE_JOB_NAME)


def apply_log_path() -> str:
    return os.path.join(get_update_work_dir(), APPLY_LOG_NAME)


def staging_download_path() -> str:
    return os.path.join(get_update_work_dir(), STAGING_EXE_NAME)


def dir_is_writable(dir_path: str) -> bool:
    try:
        os.makedirs(dir_path, exist_ok=True)
        test = os.path.join(dir_path, ".ptz_write_test")
        with open(test, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test)
        return True
    except OSError:
        return False


def canonical_exe_path(exe_dir: str) -> str:
    return os.path.join(exe_dir, EXE_NAME)


def updater_exe_path(exe_dir: str) -> str:
    return os.path.join(exe_dir, UPDATER_EXE_NAME)


def running_exe_path(exe_dir: str) -> str:
    if getattr(sys, "frozen", False) and sys.executable:
        p = os.path.abspath(sys.executable)
        if os.path.normcase(os.path.dirname(p)) == os.path.normcase(
            os.path.abspath(exe_dir)
        ):
            return p
    return canonical_exe_path(exe_dir)


def parse_version(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(version or "").strip().split("."):
        piece = piece.strip()
        if not piece:
            continue
        m = re.match(r"^(\d+)", piece)
        if m:
            parts.append(int(m.group(1)))
    return tuple(parts) if parts else (0,)


def version_greater(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def fetch_manifest(manifest_url: str) -> dict[str, Any]:
    url = (manifest_url or "").strip()
    if not url:
        return {"error": "No update manifest URL configured"}
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "PTZ-Control-Updater"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": f"Could not fetch update info: {e}"}
    if not isinstance(data, dict):
        return {"error": "Invalid update manifest"}
    version = str(data.get("version") or "").strip()
    download_url = str(data.get("download_url") or "").strip()
    if not version or not download_url:
        return {"error": "Update manifest missing version or download_url"}
    return {
        "version": version,
        "download_url": download_url,
        "sha256": str(data.get("sha256") or "").strip().lower(),
        "release_notes": str(data.get("release_notes") or "").strip(),
    }


def check_for_update(
    current_version: str,
    manifest_url: str,
    enabled: bool = True,
) -> dict[str, Any]:
    current = (current_version or "").strip() or "0"
    if not enabled:
        return {
            "update_available": False,
            "current": current,
            "latest": current,
            "notes": "",
            "error": "",
        }
    manifest = fetch_manifest(manifest_url)
    if manifest.get("error"):
        return {
            "update_available": False,
            "current": current,
            "latest": current,
            "notes": "",
            "error": manifest["error"],
        }
    latest = manifest["version"]
    available = version_greater(latest, current)
    return {
        "update_available": available,
        "current": current,
        "latest": latest,
        "notes": manifest.get("release_notes") or "",
        "download_url": manifest.get("download_url") or "",
        "sha256": manifest.get("sha256") or "",
        "error": "",
    }


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_update(
    download_url: str,
    dest_path: str,
    expected_sha256: str = "",
    progress_callback=None,
) -> tuple[bool, str]:
    url = (download_url or "").strip()
    if not url:
        return False, "No download URL"
    try:
        with requests.get(
            url,
            stream=True,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "PTZ-Control-Updater"},
        ) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
            with open(dest_path, "wb") as out:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    out.write(chunk)
                    done += len(chunk)
                    if progress_callback and total > 0:
                        progress_callback(done, total)
    except Exception as e:
        try:
            if os.path.isfile(dest_path):
                os.remove(dest_path)
        except Exception:
            pass
        return False, f"Download failed: {e}"

    if expected_sha256:
        actual = _sha256_file(dest_path)
        if actual.lower() != expected_sha256.lower():
            try:
                os.remove(dest_path)
            except Exception:
                pass
            return False, "Downloaded file failed checksum verification"
    return True, "Download complete"


def write_version_file(version_path: str, version: str) -> None:
    try:
        with open(version_path, "w", encoding="utf-8") as f:
            json.dump({"version": str(version).strip()}, f, indent=2)
            f.write("\n")
    except Exception:
        pass


def write_update_job(job: dict[str, Any]) -> str:
    work_dir = get_update_work_dir()
    path = os.path.join(work_dir, UPDATE_JOB_NAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(job, f, indent=2)
            f.write("\n")
    except PermissionError as e:
        raise PermissionError(
            f"Cannot write update job ({path}): {e}"
        ) from e
    return path


def _spawn_updater_process(updater: str, job_path: str, work_dir: str, elevated: bool) -> bool:
    if sys.platform != "win32":
        return False
    if elevated:
        import ctypes

        params = f'--job "{job_path}"'
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", updater, params, work_dir, 1
        )
        return rc > 32
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [updater, "--job", job_path],
        cwd=work_dir,
        close_fds=True,
        creationflags=flags,
    )
    return True


def launch_gui_updater(exe_dir: str, job_path: str) -> tuple[bool, str]:
    updater = updater_exe_path(exe_dir)
    if not os.path.isfile(updater):
        return (
            False,
            "PTZ-Control-Updater.exe was not found beside the app. "
            "Install the latest release using PTZ-Control-Setup.exe.",
        )
    work_dir = get_update_work_dir()
    log_path = apply_log_path()
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"Update job started: {job_path}\ninstall_dir={exe_dir}\n")
    except Exception:
        pass
    if sys.platform != "win32":
        return False, "Updates are only supported on Windows."
    need_elevation = not dir_is_writable(exe_dir)
    if not _spawn_updater_process(updater, job_path, work_dir, elevated=need_elevation):
        return (
            False,
            "Could not start the updater. Try right-clicking PTZ-Control and Run as administrator.",
        )
    time.sleep(1)
    if need_elevation:
        return (
            True,
            "Updater is installing (admin approval may be required)… the app will restart.",
        )
    return True, "Updater is installing… the app will restart."


def read_apply_log(exe_dir: str = "", tail_lines: int = 20) -> str:
    del exe_dir
    path = apply_log_path()
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-tail_lines:]).strip()
    except Exception:
        return ""
