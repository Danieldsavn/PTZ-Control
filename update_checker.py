"""
Check for app updates via a remote update.json manifest and apply in-place exe swap.
User JSON config (switcher.json, presets.json, etc.) in the exe directory is never modified.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from typing import Any

import requests

DEFAULT_MANIFEST_URL = (
    "https://github.com/Danieldsavn/PTZ-Control/releases/latest/download/update.json"
)
EXE_NAME = "PTZ-Control.exe"
LEGACY_EXE_NAME = "PTZ-CONTROL 3.0.exe"
DOWNLOAD_SUFFIX = ".download"
BAK_SUFFIX = ".bak"
REQUEST_TIMEOUT = 30


def canonical_exe_path(exe_dir: str) -> str:
    return os.path.join(exe_dir, EXE_NAME)


def running_exe_path(exe_dir: str) -> str:
    """Path of the exe the user launched (supports legacy filename)."""
    if getattr(sys, "frozen", False) and sys.executable:
        p = os.path.abspath(sys.executable)
        if os.path.normcase(os.path.dirname(p)) == os.path.normcase(os.path.abspath(exe_dir)):
            return p
    canonical = canonical_exe_path(exe_dir)
    if os.path.isfile(canonical):
        return canonical
    legacy = os.path.join(exe_dir, LEGACY_EXE_NAME)
    if os.path.isfile(legacy):
        return legacy
    return canonical


def parse_version(version: str) -> tuple[int, ...]:
    """Parse '3.0' / '3.1.2' into comparable integer tuple."""
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


def _write_apply_script(
    script_path: str,
    exe_dir: str,
    canonical_exe: str,
    running_exe: str,
    parent_pid: int,
    new_version: str,
    version_file: str,
) -> None:
    download_path = canonical_exe + DOWNLOAD_SUFFIX
    bak_path = canonical_exe + BAK_SUFFIX
    version_json = json.dumps({"version": str(new_version).strip()})
    # PowerShell: wait for app exit, swap exe, write version.json, relaunch app
    ps = f"""$ErrorActionPreference = 'Stop'
$parentPid = {int(parent_pid)}
$canonical = '{canonical_exe.replace("'", "''")}'
$running = '{running_exe.replace("'", "''")}'
$download = '{download_path.replace("'", "''")}'
$bak = '{bak_path.replace("'", "''")}'
$versionFile = '{version_file.replace("'", "''")}'
$versionJson = '{version_json.replace("'", "''")}'
$workDir = '{exe_dir.replace("'", "''")}'
try {{
  Wait-Process -Id $parentPid -ErrorAction SilentlyContinue
}} catch {{}}
Start-Sleep -Seconds 2
if (Test-Path $download) {{
  if (Test-Path $canonical) {{ Move-Item -LiteralPath $canonical -Destination $bak -Force }}
  Move-Item -LiteralPath $download -Destination $canonical -Force
  if ($running -ne $canonical -and (Test-Path $running)) {{
    Remove-Item -LiteralPath $running -Force -ErrorAction SilentlyContinue
  }}
  Set-Content -LiteralPath $versionFile -Value $versionJson -Encoding UTF8
  Start-Process -FilePath $canonical -WorkingDirectory $workDir
  Start-Sleep -Milliseconds 500
  if (-not (Get-Process -Name ([System.IO.Path]::GetFileNameWithoutExtension($canonical)) -ErrorAction SilentlyContinue)) {{
    Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", "start", "", $canonical) -WorkingDirectory $workDir
  }}
}}
Remove-Item -LiteralPath '{script_path.replace("'", "''")}' -Force -ErrorAction SilentlyContinue
"""
    with open(script_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(ps)


def apply_downloaded_update(
    exe_dir: str,
    new_version: str,
    version_file: str,
    parent_pid: int | None = None,
) -> tuple[bool, str]:
    canonical = canonical_exe_path(exe_dir)
    running = running_exe_path(exe_dir)
    download_path = canonical + DOWNLOAD_SUFFIX
    if not os.path.isfile(download_path):
        return False, "Downloaded update not found"
    if os.path.getsize(download_path) < 1024 * 1024:
        return False, "Downloaded file is too small to be a valid build"
    script_path = os.path.join(exe_dir, "apply_update.ps1")
    pid = parent_pid if parent_pid is not None else os.getpid()
    try:
        _write_apply_script(
            script_path, exe_dir, canonical, running, pid, new_version, version_file
        )
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script_path,
            ],
            cwd=exe_dir,
            creationflags=flags,
            close_fds=True,
        )
    except Exception as e:
        return False, f"Could not start updater: {e}"
    return True, "Restarting to apply update…"
