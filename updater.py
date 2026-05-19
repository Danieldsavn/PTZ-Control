"""
PTZ-Control GUI updater — downloads new build, swaps exe, restarts main app.
Invoked by main app: PTZ-Control-Updater.exe --job "path\\to\\update_job.json"
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import tkinter as tk
from tkinter import ttk

from update_checker import (
    APPLY_LOG_NAME,
    BAK_SUFFIX,
    DOWNLOAD_SUFFIX,
    EXE_NAME,
    canonical_exe_path,
    download_update,
    write_version_file,
)

PROC_NAME = "PTZ-Control"
WAIT_PROCESS_SEC = 120
SWAP_RETRIES = 60


def _log(exe_dir: str, msg: str) -> None:
    path = os.path.join(exe_dir, APPLY_LOG_NAME)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _process_running(image_name: str) -> bool:
    if sys.platform != "win32":
        return False
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}.exe", "/NH"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return image_name.lower() in (r.stdout or "").lower()
    except Exception:
        return False


def _wait_for_exit(proc_name: str, exe_dir: str, timeout: int) -> None:
    _log(exe_dir, f"Waiting for {proc_name} to exit")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _process_running(proc_name):
            break
        time.sleep(1)
    time.sleep(2)


def _swap_exe(canonical: str, download: str, bak: str, exe_dir: str) -> bool:
    for attempt in range(1, SWAP_RETRIES + 1):
        try:
            if os.path.isfile(canonical):
                if os.path.isfile(bak):
                    os.remove(bak)
                shutil.move(canonical, bak)
            shutil.move(download, canonical)
            _log(exe_dir, "Exe replaced successfully")
            return True
        except Exception as e:
            _log(exe_dir, f"Replace attempt {attempt} failed: {e}")
            time.sleep(1)
    return False


def _launch_app(canonical: str, work_dir: str, exe_dir: str) -> None:
    _log(exe_dir, f"Launching: {canonical}")
    try:
        subprocess.Popen(
            [canonical],
            cwd=work_dir,
            close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    except Exception as e:
        _log(exe_dir, f"Start-Process failed: {e}; trying cmd start")
        subprocess.Popen(
            f'cmd /c start "" "{canonical}"',
            shell=True,
            cwd=work_dir,
        )
    time.sleep(2)
    if not _process_running(PROC_NAME):
        _log(exe_dir, "App process not detected after launch (may still be starting)")


def run_update(job_path: str, ui: "UpdaterUI") -> None:
    with open(job_path, encoding="utf-8") as f:
        job = json.load(f)
    exe_dir = os.path.abspath(str(job.get("install_dir") or "").strip())
    download_url = str(job.get("download_url") or "").strip()
    sha256 = str(job.get("sha256") or "").strip()
    version = str(job.get("version") or "").strip()
    if not exe_dir or not download_url or not version:
        raise ValueError("update_job.json missing install_dir, download_url, or version")

    canonical = canonical_exe_path(exe_dir)
    download_path = canonical + DOWNLOAD_SUFFIX
    bak_path = canonical + BAK_SUFFIX
    version_file = os.path.join(exe_dir, "version.json")

    try:
        if os.path.isfile(download_path):
            os.remove(download_path)
    except Exception:
        pass

    _log(exe_dir, f"GUI updater started for version {version}")

    ui.set_status("Downloading update…")
    ui.set_progress(0)

    def on_progress(done: int, total: int) -> None:
        if total > 0:
            ui.set_progress(int(100 * done / total))

    ok, msg = download_update(download_url, download_path, sha256, on_progress)
    if not ok:
        raise RuntimeError(msg)

    if os.path.getsize(download_path) < 1024 * 1024:
        raise RuntimeError("Downloaded file is too small to be a valid build")

    ui.set_status("Installing update…")
    ui.set_progress(100)

    _wait_for_exit(PROC_NAME, exe_dir, WAIT_PROCESS_SEC)

    if not os.path.isfile(download_path):
        raise RuntimeError("Downloaded update file missing before install")

    if not _swap_exe(canonical, download_path, bak_path, exe_dir):
        raise RuntimeError("Could not replace PTZ-Control.exe (file may still be in use)")

    write_version_file(version_file, version)
    _log(exe_dir, "version.json updated")

    ui.set_status("Restarting PTZ-Control…")
    _launch_app(canonical, exe_dir, exe_dir)
    _log(exe_dir, "Update finished")


class UpdaterUI:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("PTZ-Control Updater")
        self.root.resizable(False, False)
        self.root.geometry("420x140")
        self.status = tk.StringVar(value="Starting…")
        ttk.Label(self.root, textvariable=self.status, wraplength=380).pack(
            padx=16, pady=(16, 8), anchor="w"
        )
        self.bar = ttk.Progressbar(self.root, length=380, mode="determinate", maximum=100)
        self.bar.pack(padx=16, pady=8)
        self.error_label = ttk.Label(self.root, text="", foreground="red", wraplength=380)
        self.error_label.pack(padx=16, pady=(0, 12), anchor="w")
        self._done = False

    def set_status(self, text: str) -> None:
        self.status.set(text)
        self.root.update_idletasks()

    def set_progress(self, pct: int) -> None:
        self.bar["value"] = max(0, min(100, pct))
        self.root.update_idletasks()

    def show_error(self, text: str) -> None:
        self.error_label.config(text=text)
        self.set_status("Update failed")
        self._done = True
        ttk.Button(self.root, text="Close", command=self.root.destroy).pack(pady=4)

    def show_success(self) -> None:
        self.set_status("Update complete — restarting app")
        self._done = True
        self.root.after(1500, self.root.destroy)

    def run_worker(self, job_path: str) -> None:
        def work() -> None:
            try:
                run_update(job_path, self)
                self.root.after(0, self.show_success)
            except Exception as e:
                exe_dir = os.path.dirname(job_path)
                try:
                    with open(job_path, encoding="utf-8") as f:
                        exe_dir = os.path.abspath(
                            str(json.load(f).get("install_dir") or exe_dir)
                        )
                except Exception:
                    pass
                _log(exe_dir, f"ERROR: {e}")
                self.root.after(0, lambda: self.show_error(str(e)))

        import threading

        threading.Thread(target=work, daemon=True).start()

    def mainloop(self) -> None:
        self.root.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True, help="Path to update_job.json")
    args = parser.parse_args()
    job_path = os.path.abspath(args.job)
    if not os.path.isfile(job_path):
        print(f"Job file not found: {job_path}", file=sys.stderr)
        return 1
    ui = UpdaterUI()
    ui.run_worker(job_path)
    ui.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
