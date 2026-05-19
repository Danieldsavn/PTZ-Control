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
import time
from typing import Any

import requests

DEFAULT_MANIFEST_URL = (
    "https://github.com/Danieldsavn/PTZ-Control/releases/latest/download/update.json"
)
EXE_NAME = "PTZ-Control.exe"
LEGACY_EXE_NAME = "PTZ-CONTROL 3.0.exe"
DOWNLOAD_SUFFIX = ".download"
BAK_SUFFIX = ".bak"
REQUEST_TIMEOUT = 120
APPLY_LOG_NAME = "apply_update.log"


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
    new_version: str,
    version_file: str,
) -> None:
    download_path = canonical_exe + DOWNLOAD_SUFFIX
    bak_path = canonical_exe + BAK_SUFFIX
    log_path = os.path.join(exe_dir, APPLY_LOG_NAME)
    version_json = json.dumps({"version": str(new_version).strip()})
    # Wait for running process to exit, swap exe (retry while locked), relaunch, log steps.
    ps = f"""$logFile = '{log_path.replace("'", "''")}'
function Log($msg) {{
  try {{ Add-Content -LiteralPath $logFile -Value ("[{{0}}] {{1}}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg) }} catch {{}}
}}
Log "Update script started"
$canonical = '{canonical_exe.replace("'", "''")}'
$running = '{running_exe.replace("'", "''")}'
$download = '{download_path.replace("'", "''")}'
$bak = '{bak_path.replace("'", "''")}'
$versionFile = '{version_file.replace("'", "''")}'
$versionJson = '{version_json.replace("'", "''")}'
$workDir = '{exe_dir.replace("'", "''")}'
$procName = [System.IO.Path]::GetFileNameWithoutExtension($running)
Log ("Waiting for process: " + $procName)
$deadline = (Get-Date).AddSeconds(120)
while ((Get-Date) -lt $deadline) {{
  $alive = Get-Process -Name $procName -ErrorAction SilentlyContinue
  if (-not $alive) {{ break }}
  Start-Sleep -Seconds 1
}}
Start-Sleep -Seconds 2
if (-not (Test-Path -LiteralPath $download)) {{
  Log ("Download file missing: " + $download)
  exit 1
}}
$moved = $false
for ($i = 1; $i -le 60; $i++) {{
  try {{
    if (Test-Path -LiteralPath $canonical) {{
      Move-Item -LiteralPath $canonical -Destination $bak -Force -ErrorAction Stop
    }}
    Move-Item -LiteralPath $download -Destination $canonical -Force -ErrorAction Stop
    $moved = $true
    Log "Exe replaced successfully"
    break
  }} catch {{
    Log ("Replace attempt $i failed: " + $_.Exception.Message)
    Start-Sleep -Seconds 1
  }}
}}
if (-not $moved) {{
  Log "ERROR: Could not replace exe after retries"
  exit 1
}}
if ($running -ne $canonical -and (Test-Path -LiteralPath $running)) {{
  try {{
    Remove-Item -LiteralPath $running -Force -ErrorAction SilentlyContinue
    Log "Removed legacy exe"
  }} catch {{}}
}}
try {{
  Set-Content -LiteralPath $versionFile -Value $versionJson -Encoding UTF8
  Log "version.json updated"
}} catch {{
  Log ("version.json write failed: " + $_.Exception.Message)
}}
Log ("Launching: " + $canonical)
Start-Process -FilePath $canonical -WorkingDirectory $workDir | Out-Null
Start-Sleep -Seconds 2
$restarted = Get-Process -Name $procName -ErrorAction SilentlyContinue
if (-not $restarted) {{
  Log "Start-Process did not show app; trying cmd start"
  $arg = '/c start "" "' + $canonical + '"'
  Start-Process -FilePath "cmd.exe" -ArgumentList $arg -WorkingDirectory $workDir | Out-Null
}}
Log "Update script finished"
"""
    with open(script_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(ps)


def _launch_apply_script(script_path: str, exe_dir: str) -> None:
    """Start updater detached so it keeps running after the app exits."""
    script_quoted = f'"{script_path}"'
    if sys.platform == "win32":
        # "start" fully detaches the child on Windows (more reliable than DETACHED_PROCESS alone).
        cmd = (
            f'start "" /MIN powershell.exe -NoProfile -ExecutionPolicy Bypass '
            f"-WindowStyle Hidden -File {script_quoted}"
        )
        subprocess.Popen(cmd, shell=True, cwd=exe_dir)
        return
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
        close_fds=True,
    )


def apply_downloaded_update(
    exe_dir: str,
    new_version: str,
    version_file: str,
    parent_pid: int | None = None,
) -> tuple[bool, str]:
    del parent_pid  # wait by process image name instead (works with legacy exe names)
    canonical = canonical_exe_path(exe_dir)
    running = running_exe_path(exe_dir)
    download_path = canonical + DOWNLOAD_SUFFIX
    if not os.path.isfile(download_path):
        return False, "Downloaded update not found"
    if os.path.getsize(download_path) < 1024 * 1024:
        return False, "Downloaded file is too small to be a valid build"
    script_path = os.path.join(exe_dir, "apply_update.ps1")
    log_path = os.path.join(exe_dir, APPLY_LOG_NAME)
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"Update requested for version {new_version}\n")
        _write_apply_script(
            script_path, exe_dir, canonical, running, new_version, version_file
        )
        _launch_apply_script(script_path, exe_dir)
        time.sleep(1.5)
    except Exception as e:
        return False, f"Could not start updater: {e}"
    return True, "Installing update and restarting…"


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
