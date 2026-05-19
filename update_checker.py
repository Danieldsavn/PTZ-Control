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


def write_update_job(exe_dir: str, job: dict[str, Any]) -> str:
    path = os.path.join(exe_dir, UPDATE_JOB_NAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2)
        f.write("\n")
    return path


def launch_gui_updater(exe_dir: str, job_path: str) -> tuple[bool, str]:
    updater = updater_exe_path(exe_dir)
    if not os.path.isfile(updater):
        return (
            False,
            "PTZ-Control-Updater.exe was not found beside the app. "
            "Install the latest release using PTZ-Control-Setup.exe.",
        )
    log_path = os.path.join(exe_dir, APPLY_LOG_NAME)
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"Update job started: {job_path}\n")
    except Exception:
        pass
    if sys.platform == "win32":
        cmd = f'start "" "{updater}" --job "{job_path}"'
        subprocess.Popen(cmd, shell=True, cwd=exe_dir)
        time.sleep(1)
        return True, "Updater is installing… the app will restart."
    return False, "Updates are only supported on Windows."


def read_apply_log(exe_dir: str, tail_lines: int = 20) -> str:
    path = os.path.join(exe_dir, APPLY_LOG_NAME)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-tail_lines:]).strip()
    except Exception:
        return ""
